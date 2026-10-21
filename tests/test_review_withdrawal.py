"""需求 R363 (Issue #67): AI 建议的错报不因 "确认" 漏记; 意见决定前列出 AI 建议为错报但未认定的判断; FIN-01 通过前
可撤回复核决定重新选择.

复现 ENG-2026-005 (Xero 演示, 演示配置含 misstatement 与 planning): 只经 HTTP 接口, 照数据管理页导入两阶段演示资料,
过 ACC-01, 数据已齐, PLN-01/03 与总体审计计划 (先退回再批准, 同 R317) 后复核. AI 建议为错报的只有 GEN-02 的 XJ-499
(办公设备高估 750.00) 与 CSH-02 的 BA-091 (利息收入低估 48.53): 它们的复核选项没有 confirm, "同意" 以 misstatement
认定 (草稿取 AI 的), "不同意" 以 override 给出不是错报的结论; 其余判断确认. 错报闭环里 FIN-03 分类都确认, FIN-08
登记管理层回复 (补造的第 1 轮沟通记录), 等更正时停止等待 (错报记未更正), 到 FIN-01 闸门. 整体重要性 324.31, 明显
微小 16.22.
"""

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from backend.contracts.models import (
    Misstatement,
    MisstatementStatus,
    ReviewWithdrawal,
    TrailKind,
)
from backend.orchestration.runner import GraphRunner
from backend.services.data_service import DataService
from tests.fakes import FakeTrail, wait_idle
from tests.fixture_sets import XERO
from tests.xero_fakes import PLAN_DECISIONS, XERO17_MATERIALITY
from tests.xero_flow import API, READY_REASON, demo_offline, drain, fin08_record
from tests.xero_harness import LayeredMockLLMClient

pytestmark = pytest.mark.external
XJ499_TASK = "T-J-GEN-02-1-XJ-499-v1"
XJ499 = "J-GEN-02-1-XJ-499"
NOT_MISSTATEMENT = "The fixed asset register lists the office equipment; the AUD 750.00 asset is supported."
MAX_STEPS = 40


@dataclass
class Demo:
    """一个离线演示项目的接口客户端, runner 与数据服务, 及项目编号."""

    client: httpx.AsyncClient
    runner: GraphRunner
    service: DataService
    eid: str = ""

    @property
    def base(self) -> str:
        return f"{API}/engagements/{self.eid}"

    async def settle(self) -> dict[str, Any]:
        """等后台作业与图都停下, 返回总览 (后台没有错误)."""
        await drain(self.service)
        await wait_idle(self.runner, self.eid)
        overview: dict[str, Any] = (await self.client.get(self.base)).json()
        assert overview["run_error"] is None, overview["run_error"]
        return overview

    async def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post(f"{self.base}/{path}", json=body)

    async def submit(self, path: str, body: dict[str, Any]) -> None:
        posted = await self.post(path, body)
        assert posted.status_code == 202, posted.text

    async def decide(self, decision: str, **fields: Any) -> dict[str, Any]:
        """对当前闸门的材料提交决定, 返回提交前 GET /gate 的视图."""
        view: dict[str, Any] = (await self.client.get(f"{self.base}/gate")).json()
        assert view is not None, "不在闸门"
        body = {k: view[k] for k in ("gate", "target_id", "target_version")}
        await self.submit("gate-decisions", {**body, "decision": decision, **fields})
        return view

    async def tasks(self, status: str) -> list[dict[str, Any]]:
        got = await self.client.get(f"{self.base}/review-tasks", params={"status": status})
        return list(got.json())


async def to_reviews(d: Demo) -> None:
    """建项目, 每个停点按数据管理页的建议导入演示资料, 过 ACC-01, 数据已齐, PLN-01/03 与计划 (先退回再批准), 停在
    执行阶段的复核."""
    clients = (await d.client.get(f"{API}/clients")).json()
    [dataset] = [x for x in clients[0]["datasets"] if x["default"]]
    body = {k: dataset[k] for k in ("dataset", "period_start", "period_end")}
    created = await d.client.post(f"{API}/engagements", json=body)
    assert created.status_code == 201, created.text
    d.eid = created.json()["engagement"]["id"]

    async def stop() -> None:
        await d.settle()
        suggested = (await d.client.get(f"{d.base}/data/summary")).json()["demo"]["suggested"]
        if suggested is not None:
            await d.submit("data/demo-import", {"stage": suggested})
            await d.settle()

    await stop()
    await d.decide("approve")
    await stop()
    await d.submit("data/ready", {"reason": READY_REASON})
    await stop()
    await d.decide("approve", materiality=XERO17_MATERIALITY.model_dump())
    for plan_decision in PLAN_DECISIONS:
        await stop()
        await d.decide(**plan_decision)
    assert (await d.settle())["awaiting_review"]


def review(view: dict[str, Any], xj499: str) -> dict[str, Any]:
    """一条复核任务的决定: AI 建议为错报的 (没有 confirm 可选) 同意即认定 (草稿取 AI 的); XJ-499 按 xj499 为 agree 或
    disagree (不同意, 推翻为不是错报); 其余确认."""
    task, target = view["task"], view["target"]
    body: dict[str, Any] = {"task_id": task["id"], "target_version": task["target_version"]}
    if "confirm" in view["allowed_decisions"]:
        return body | {"decision": "confirm"}
    assert view["allowed_decisions"] == ["override", "misstatement"], view["allowed_decisions"]
    if task["id"] == XJ499_TASK and xj499 == "disagree":
        return body | {"decision": "override", "override_conclusion": NOT_MISSTATEMENT}
    return body | {"decision": "misstatement", "misstatement_draft": target["misstatement_draft"]}


async def to_fin01(d: Demo, xj499: str) -> dict[str, Any]:
    """从复核往下走到 FIN-01 闸门: 复核任务按 review 决定, FIN-08 登记管理层回复, 等更正时停止等待. 返回闸门的视图."""
    for _ in range(MAX_STEPS):
        overview = await d.settle()
        if overview["current_gate"] == "FIN-01":
            view: dict[str, Any] = (await d.client.get(f"{d.base}/gate")).json()
            return view
        pending = [v for v in await d.tasks("pending") if v["task"]["kind"] != "manual"]  # 人工任务另走 FIN-08
        manual = [v for v in (await d.client.get(f"{d.base}/manual-tasks")).json() if v["task"]["status"] == "pending"]
        if pending:
            await d.submit("review-records", review(pending[0], xj499))
        elif manual:
            await d.submit("review-records", fin08_record(manual[0]))
        elif overview["awaiting_correction"]:
            ids = overview["awaiting_correction_ids"]
            await d.submit("correction-notices", {"kind": "stop_waiting", "misstatement_ids": ids})
        else:
            pytest.fail(f"停在了意料之外的位置: {overview['current_gate']}, {overview['progress']}")
    pytest.fail(f"{MAX_STEPS} 步内没有到 FIN-01")


async def withdraw(d: Demo, task_id: str) -> httpx.Response:
    """按复核队列给的可撤回决定撤回该任务上的决定 (前端的撤回入口)."""
    [view] = [v for v in await d.tasks("decided") if v["task"]["id"] == task_id]
    assert view["withdrawable_record_id"] is not None, view
    return await d.post(f"review-records/{view['withdrawable_record_id']}/withdraw", {"reason": None})


def schedule_of(gate: dict[str, Any]) -> tuple[list[tuple[str, int]], int, str]:
    """FIN-01 材料的汇总表: (来源判断, 累计额) 各行, 合计, 候选意见."""
    material = gate["material"]
    sources = {m["id"]: m["judgment_id"] for m in material["misstatements"]}
    schedule = material["schedule"]
    rows = sorted((sources[i["misstatement_id"]], i["amount_cents"]) for i in schedule["items"])
    return rows, schedule["total_cents"], schedule["candidate_opinion"]


def demo() -> Any:
    """离线的演示项目 (tests.xero_flow.demo_offline), 大模型读只读运行副本 (同 R287 的走通)."""
    XERO.ready()
    return demo_offline(LayeredMockLLMClient([XERO.runtime]))


BOTH = [("J-CSH-02-1-BA-091", 4853), (XJ499, 75000)]  # 两条错报的 (来源判断, 累计额)


async def test_agreeing_records_both_and_withdrawing_one_updates_the_schedule() -> None:
    """验收 1, 3, 4: XJ-499 与 BA-091 的复核选项只有认定 (同意) 与推翻 (不同意), 确认报 422 DECISION_NOT_ALLOWED,
    也不是可批量确认的档位; 都 "同意" 后 FIN-01 材料的汇总表有这两条, 未更正合计 798.53, 候选保留意见, 没有未认定的
    建议. FIN-01 之前撤回 XJ-499 的认定: 错报作废 (withdrawn), 判断回到待复核 (可撤回的入口随之消失), 图回到执行
    阶段等复核; 改选不同意后走到 FIN-01, 合计只剩 48.53, 候选无保留, XJ-499 列在 AI 建议为错报但未认定的一节 (750.00
    不低于整体重要性, 计入后候选为保留). 原决定与撤回各一条 decision 留痕."""
    async with demo() as (runner, service, client):
        d = Demo(client, runner, service)
        await to_reviews(d)
        [view] = [v for v in await d.tasks("pending") if v["task"]["id"] == XJ499_TASK]
        assert view["tier"] != "batch_eligible" and view["target"]["proposed_action"] == "misstatement"
        confirm = await d.post("review-records", {**review(view, "agree"), "decision": "confirm"})
        assert (confirm.status_code, confirm.json()["detail"]["code"]) == (422, "DECISION_NOT_ALLOWED")

        gate = await to_fin01(d, "agree")
        assert schedule_of(gate) == (BOTH, 79853, "qualified")
        assert gate["material"]["schedule"]["unrecognised"] == []
        overview = await d.settle()
        assert overview["can_withdraw"] is True
        [decided] = [v for v in await d.tasks("decided") if v["task"]["id"] == XJ499_TASK]
        record_id = decided["withdrawable_record_id"]

        withdrawn = await withdraw(d, XJ499_TASK)
        assert withdrawn.status_code == 202, withdrawn.text
        overview = await d.settle()
        assert (overview["awaiting_review"], overview["current_gate"], overview["engagement"]["phase"]) == (
            True,
            None,
            "P2_Executing",
        )
        [again] = [v for v in await d.tasks("pending") if v["task"]["id"] == XJ499_TASK]
        assert (again["allowed_decisions"], again["target"]["review_status"]) == (
            ["override", "misstatement"],
            "pending",
        )
        assert again["task"]["target_version"] == again["target"]["version"]
        voided = await runner.deps.output_store.get(d.eid, Misstatement, f"M-{record_id}")
        assert voided is not None and voided.status is MisstatementStatus.withdrawn

        gate = await to_fin01(d, "disagree")
        assert schedule_of(gate) == (BOTH[:1], 4853, "unqualified")
        [suggestion] = gate["material"]["schedule"]["unrecognised"]
        assert {k: suggestion[k] for k in ("judgment_id", "amount_cents", "direction", "decision", "level")} == {
            "judgment_id": XJ499,
            "amount_cents": 75000,
            "direction": "overstated",
            "decision": "override",
            "level": "material",
        }
        assert suggestion["would_qualify"] is True and suggestion["reviewed_by"] == overview["engagement"]["owner_id"]
        trail = runner.deps.trail
        assert isinstance(trail, FakeTrail)
        keys = [e.idempotency_key for e in trail.entries if e.kind is TrailKind.decision]
        [withdrawal] = await runner.deps.output_store.latest(d.eid, ReviewWithdrawal)
        assert (withdrawal.record_id, withdrawal.reason) == (record_id, None)
        assert keys.count(record_id) == 1 and keys.count(withdrawal.id) == 1


async def test_disagreeing_is_listed_and_withdrawal_is_refused_after_fin01() -> None:
    """验收 2, 3, 4: XJ-499 "不同意, 不是错报" 时 FIN-01 材料的汇总表只有 BA-091 (48.53), XJ-499 列在未认定一节 (金额,
    方向, 复核人与时间, 750.00 不低于整体重要性); 撤回该推翻, 改为同意后累计随之更新为 798.53, 未认定一节为空. FIN-01
    通过后不能再撤回 (409 NOT_WITHDRAWABLE), 总览 can_withdraw 为假, 复核队列也不给撤回入口."""
    async with demo() as (runner, service, client):
        d = Demo(client, runner, service)
        await to_reviews(d)
        gate = await to_fin01(d, "disagree")
        assert schedule_of(gate) == (BOTH[:1], 4853, "unqualified")
        [suggestion] = gate["material"]["schedule"]["unrecognised"]
        assert (suggestion["judgment_id"], suggestion["level"], suggestion["decided_at"] is not None) == (
            XJ499,
            "material",
            True,
        )

        assert (await withdraw(d, XJ499_TASK)).status_code == 202
        gate = await to_fin01(d, "agree")
        assert schedule_of(gate) == (BOTH, 79853, "qualified")
        assert gate["material"]["schedule"]["unrecognised"] == []

        await d.decide("approve", disposition=gate["material"]["schedule"]["candidate_opinion"])
        overview = await d.settle()
        assert overview["can_withdraw"] is False and overview["current_gate"] == "RPT-01"
        assert all(v["withdrawable_record_id"] is None for v in await d.tasks("decided"))
        misstatements = await runner.deps.output_store.latest(d.eid, Misstatement)
        [ba091] = [m.review_record_id for m in misstatements if m.judgment_id == "J-CSH-02-1-BA-091"]
        refused = await d.post(f"review-records/{ba091}/withdraw", {})
        assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "NOT_WITHDRAWABLE")
