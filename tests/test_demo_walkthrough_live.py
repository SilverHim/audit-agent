"""需求 R311: 演示走通用例 (R287, tests/test_demo_walkthrough.py) 的真实大模型变体. 同样只经 HTTP 接口照页面的做法走:
新建项目, 每个停点按概览建议导入演示资料, 点 "数据已齐", 过各道闸门 (总体审计计划先退回一次再批准, 需求 R317),
复核逐条决定, RPT-01 先退回再批准, 签字归档;
全部大模型程序走真实模型 (组装根 LLM_MODE live), 功能集为演示配置, 存储为真实 Postgres (同 R287 的连库变体).

真实模型的输出不固定, 所以驱动 (drive) 不按停点的固定顺序走, 每次停下按当时的状态办: 有待决任务先逐条决定, 概览建议
导入就导入, 等数据就绪就确认数据已齐, 停在闸门就按闸门决定. 审计师的决定以演示决定表为准, 按决定表期望的结论下,
不看模型的建议动作 (decision_table): BA-091 与 XJ-499 认定错报 (草稿同 R287), XJ-575 推翻为接受, 其余确认; 争议任务能
维持原判断就维持, 否则 "两者都不采用" 给出期望的结论; 无解释的例外按同一张表认定错报或推翻; failed 任务重跑, 每个对象
至多一次. FIN-01 按汇总表的候选意见决定 (错报闭环接入前为保留意见), RPT-03 由模型按所选意见起草.

断言分两级. 硬性: 走到归档, 后台 run_error 始终为空; 每个判断对象有模型自己的结论 (一次通过, 或 failed 后重跑一次;
带规则例外的对象判断失败时复核队列建的是 unattributed 任务, 不能重跑, 驱动按决定表推翻后照常走完, 但该对象缺模型
结论, 硬性检查不过); 全部证据指针经存储解析通过; 签字的报告意见类型与 FIN-01 决定一致, 意见段标题与之相符.
软性 (只告警): 判断的建议动作与固定输出 (演示运行副本) 不同的逐条列出. 汇总写 var/eval/demo-live-<UTC>.json.

配置经 tests.external.live_llm_settings 取 (环境变量, LLM_ENV_FILE 或仓库 .env), 不写进任何文件; 没配时跳过. 不联网的
自测用同一驱动与检查走演示运行副本的固定输出: 离线假存储一遍 (另有一对象先失败再重跑, 与带规则例外的对象失败两种),
连库一遍 (同真实模型用例的路径, 只把大模型换成组装根的 mock); 决定的请求体另有合成数据的单元用例.
"""

import json
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from backend.adapters.llm import SwitchableLLMClient
from backend.adapters.registry_mock import DatasetRegistrySource
from backend.api.schemas import ReviewRecordIn
from backend.contracts.models import (
    AuditPlan,
    AuditReportVersion,
    Confirmation,
    Judgment,
    OpinionType,
    ReportStatus,
    TrailEntry,
    TrailKind,
)
from backend.orchestration.runner import GraphRunner
from backend.services.data_service import DataService
from backend.services.judgment_engine import REPORT_HEADINGS, PointerResolver
from tests.external import LLM_KEYS, live_llm_settings
from tests.fakes import NUMBAT_AUDITOR, PLAN_CONFIRMATION, FakeRuleEngine, FakeTrail, api_client, wait_idle
from tests.fixture_sets import XERO
from tests.test_decisions import PLAN_REJECT_REASON, Started
from tests.test_demo_default import CreatingEngagementStore
from tests.test_demo_walkthrough import API, xero_link
from tests.test_e2e_mock import evidence_pointers, real_storage
from tests.test_e2e_xero17 import XERO_PERIOD
from tests.tools.live_budget import EVAL_DIR, Budget, install_budget, summary, write_summary
from tests.xero_fakes import (
    DEMO_FEATURES,
    ORC1026,
    XERO17_ANNOTATION,
    XERO17_BA091_DRAFT,
    XERO17_INVOICES,
    XERO17_MATERIALITY,
    XERO_DATASET,
    XERO_RULES,
    new_xero_deps,
    xero_journal_reviews,
)
from tests.xero_flow import READY_REASON, demo_source, drain, offline_runner
from tests.xero_harness import FlakyLLM, LayeredMockLLMClient

# 用量上限: 固定输出走一遍 47 次 (含总体审计计划两个修订, 需求 R317; 另有 A7 两轮复核, 随模型输出触发), 留出校验
# 重试与重跑的余量
MAX_CALLS = 152
MAX_STEPS = 200  # 驱动的步数上限 (固定输出走一遍约 40 步)
JUDGING = ("REV-03", "REV-02", "CSH-02", "GEN-02")  # 演示配置下出判断的大模型程序, 各对象的固定输出在运行副本里
GATES = ["ACC-01", "PLN-01/03", "PLN-15", "PLN-15", "FIN-01", "RPT-01", "RPT-01"]  # 依次决定的闸门
RERUN_REASON = "The model output did not pass validation; rerun."
CONFIRM_CONCLUSION = "The original conclusion stands: no misstatement is identified for this item."
BA091_REASON = "Accrued interest of AUD 48.53 confirmed by the bank was not recorded."


@dataclass(frozen=True)
class Expected:
    """演示决定表中一个对象期望的结论: 认定错报 (带草稿), 推翻 (给出结论) 或确认."""

    decision: str  # misstatement, override 或 confirm
    reason: str | None = None
    draft: dict[str, Any] | None = None  # 认定错报的草稿 (JSON)
    conclusion: str | None = None  # 推翻或 "两者都不采用" 时的结论


def decision_table(owner: str) -> dict[str, Expected]:
    """演示决定表 (同 R287 的离线走法), 按判断对象: BA-091 认定错报; GEN-02 七笔同 R146 (XJ-499 认定错报, XJ-575 推翻
    为接受, 其余确认). 表外的对象一律确认."""
    table = {"BA-091": Expected("misstatement", BA091_REASON, XERO17_BA091_DRAFT.model_dump(exclude_none=True))}
    for record in xero_journal_reviews(owner):
        voucher = record.task_id.removeprefix("T-J-GEN-02-1-").removesuffix("-v1")
        draft = record.misstatement_draft.model_dump(exclude_none=True) if record.misstatement_draft else None
        table[voucher] = Expected(record.decision.value, record.reason, draft, record.override_conclusion)
    return table


FinPolicy = Callable[[dict[str, Any]], str]  # FIN-01 闸门的材料 (GET /gate 的 material) → 选定的意见类型


def candidate_opinion(material: dict[str, Any]) -> str:
    """按汇总表的候选意见决定 (需求 R311)."""
    return cast(str, material["schedule"]["candidate_opinion"])


@dataclass
class LiveWalk:
    """一次走法的记录, 驱动边走边写 (中途失败时汇总照样能写出已走到的部分)."""

    eid: str | None = None
    owner: str | None = None
    gates: list[str] = field(default_factory=list)  # 依次决定过的闸门
    fin: str | None = None  # FIN-01 选定的意见类型
    candidate: str | None = None  # 汇总表的候选意见
    failures: Counter[tuple[str, str]] = field(default_factory=Counter)  # (程序, 对象) → 出过的 failed 任务数
    troubled: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # 任务 ID → (程序, 对象, 类型), 非 normal
    decisions: list[tuple[str, str]] = field(default_factory=list)  # (任务 ID, 决定)


def subject_of(view: dict[str, Any]) -> str:
    """任务所指的对象: 判断的 subject_id; 例外取 detail 的凭证号, 否则首个来源 (如回函应计利息的银行账户);
    failed 任务取任务的 subject_id (整条程序失败为空串)."""
    task, target = view["task"], view["target"]
    if task["kind"] == "failed":
        return cast(str, task.get("subject_id") or "")
    if target.get("subject_id"):
        return cast(str, target["subject_id"])
    detail = target.get("detail") or {}
    return cast(str, detail.get("entry_no") or (target.get("source_ids") or [""])[0])


def review_body(view: dict[str, Any], table: dict[str, Expected], walk: LiveWalk) -> dict[str, Any]:
    """对一条待决任务的决定 (请求体), 按演示决定表期望的结论, 不看模型的建议动作 (见模块说明)."""
    task = view["task"]
    kind, pid, subject = task["kind"], task["procedure_id"], subject_of(view)
    if kind == "failed" and pid == "PLN-15":  # 整条失败没有对象, 按修订计: 每个修订各至多重跑一次 (需求 R317)
        subject = f"r{walk.gates.count('PLN-15') + 1}"
    body: dict[str, Any] = {"task_id": task["id"], "target_version": task["target_version"]}
    if kind != "normal":
        walk.troubled[task["id"]] = (pid, subject, kind)
    expected = table.get(subject, Expected("confirm"))
    if kind == "failed":
        walk.failures[pid, subject] += 1
        if walk.failures[pid, subject] > 1:
            pytest.fail(f"{pid} {subject or '(整条)'} 重跑后又失败: {task['id']}")
        return body | {"decision": "rerun", "reason": RERUN_REASON}
    if kind in ("normal", "unattributed"):  # 无解释的例外: 认定错报或推翻, 不能确认
        if expected.decision == "misstatement":
            return body | {"decision": "misstatement", "reason": expected.reason, "misstatement_draft": expected.draft}
        if expected.decision == "override" or kind == "unattributed":
            conclusion = expected.conclusion or CONFIRM_CONCLUSION
            return body | {
                "decision": "override",
                "reason": expected.reason or conclusion,
                "override_conclusion": conclusion,
            }
        return body | {"decision": "confirm"}
    if kind == "dispute":
        return body | dispute_fields(view, expected)
    pytest.fail(f"没有预期的任务类型 {kind}: {task['id']}")


def dispute_fields(view: dict[str, Any], expected: Expected) -> dict[str, Any]:
    """争议任务的仲裁: 原判断 (A6) 与期望一致时维持原判断 (期望错报时须原判断也建议错报, 带草稿; 期望不认定错报时
    原判断不建议错报); 否则 "两者都不采用" (neither), 给出期望的结论, 期望错报时附草稿. 期望为推翻的对象一律 neither."""
    a6 = view["draft_rules"].get("a6_upheld")
    want = expected.decision == "misstatement"
    upheld = {"decision": "dispute_resolution", "dispute_resolution": "a6_upheld"}
    if "a6_upheld" in view["allowed_resolutions"] and expected.decision != "override":
        if want and a6 == "required":
            return upheld | {"reason": expected.reason, "misstatement_draft": expected.draft}
        if not want and a6 != "required":
            return upheld | {"reason": CONFIRM_CONCLUSION}
    conclusion = expected.conclusion or (expected.reason if want else CONFIRM_CONCLUSION)
    fields: dict[str, Any] = {
        "decision": "dispute_resolution",
        "dispute_resolution": "neither",
        "neither_action": "human_conclusion",
        "override_conclusion": conclusion,
        "reason": expected.reason or conclusion,
    }
    return fields | ({"misstatement_draft": expected.draft} if want else {})


async def drive(
    client: httpx.AsyncClient,
    runner: GraphRunner,
    service: DataService,
    walk: LiveWalk,
    *,
    fin: FinPolicy = candidate_opinion,
    budget: Budget | None = None,
) -> LiveWalk:
    """从新建项目走到归档 (见模块说明), 进度记在 walk. 每步之后等后台作业与图都停下, 后台始终没有错误; 每次提交前查
    预算 (已超限即失败, 不再提交重跑)."""
    walk.owner = (await client.get(f"{API}/users/me")).json()["id"]
    table = decision_table(walk.owner)
    clients = (await client.get(f"{API}/clients")).json()
    [dataset] = [d for d in clients[0]["datasets"] if d["default"]]
    created = await client.post(
        f"{API}/engagements", json={k: dataset[k] for k in ("dataset", "period_start", "period_end")}
    )
    assert created.status_code == 201, created.text
    eid = walk.eid = created.json()["engagement"]["id"]
    base = f"{API}/engagements/{eid}"
    ready_posted = False

    async def post(path: str, body: dict[str, Any]) -> None:
        if budget is not None:
            budget.check()
        posted = await client.post(f"{base}/{path}", json=body)
        assert posted.status_code in (201, 202), (path, posted.text)

    for _ in range(MAX_STEPS):
        await drain(service)
        await wait_idle(runner, eid)
        overview = (await client.get(base)).json()
        assert overview["run_error"] is None, overview["run_error"]
        if overview["engagement"]["phase"] == "Archived":
            return walk
        pending = (await client.get(f"{base}/review-tasks", params={"status": "pending"})).json()
        if pending:
            body = review_body(pending[0], table, walk)
            walk.decisions.append((body["task_id"], body["decision"]))
            await post("review-records", body)
            continue
        suggested = (await client.get(f"{base}/data/summary")).json()["demo"]["suggested"]
        if suggested is not None:
            await post("data/demo-import", {"stage": suggested})
            continue
        if overview["awaiting_data"]:
            assert not ready_posted, f"确认数据已齐之后仍在等数据: {overview['missing_tables']}"
            ready_posted = True
            await post("data/ready", {"reason": READY_REASON})
            continue
        gate = overview["current_gate"]
        assert gate is not None, f"没有待决任务, 不等数据也不在闸门, 流程停住: {overview['procedures']}"
        got = await client.get(f"{base}/gate")
        assert got.status_code == 200, got.text
        view = got.json()
        body = {k: view[k] for k in ("gate", "target_id", "target_version")}
        if gate == "PLN-01/03":
            body |= {"decision": "approve", "materiality": XERO17_MATERIALITY.model_dump()}
        elif gate == "FIN-01":
            walk.candidate = view["material"]["schedule"]["candidate_opinion"]
            walk.fin = fin(view["material"])
            reason = f"Opinion follows the schedule of uncorrected misstatements (candidate {walk.candidate})."
            body |= {"decision": "approve", "disposition": walk.fin, "reason": reason}
        elif gate == "PLN-15" and "PLN-15" not in walk.gates:  # 计划先退回一次, 同 R317 的离线走法
            body |= {"decision": "reject", "reason": PLAN_REJECT_REASON}
        elif gate == "PLN-15":
            body |= {"decision": "approve", "plan_confirmation": PLAN_CONFIRMATION.model_dump()}
        elif gate == "RPT-01" and "RPT-01" not in walk.gates:  # 先退回一次, 同 R287
            reason = "The basis for opinion paragraph must name the financial reporting framework."
            body |= {"decision": "reject", "annotations": [XERO17_ANNOTATION.model_dump()], "reason": reason}
        else:
            body |= {"decision": "approve"}
        walk.gates.append(gate)
        await post("gate-decisions", body)
    pytest.fail(f"{MAX_STEPS} 步内未到归档")


def latest_model_judgments(judgments: list[Judgment]) -> dict[tuple[str, str], Judgment]:
    """模型自己给出的判断 (人工判断 -H 不算), 每个 (程序, 对象) 取最后一次执行的."""
    latest: dict[tuple[str, str], Judgment] = {}
    for j in sorted(judgments, key=lambda j: (j.run_id, j.version)):
        if "-H" not in j.id:
            latest[j.procedure_id, j.subject_id] = j
    return latest


def fixture_action(procedure_id: str, subject_id: str) -> str | None:
    """演示运行副本里该对象固定输出的建议动作; 没有固定输出为空."""
    path = XERO.runtime / procedure_id / f"{subject_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))["proposed_action"] if path.is_file() else None


async def evaluate(runner: GraphRunner, walk: LiveWalk, budget: Budget) -> dict[str, Any]:
    """汇总 (需求 R311 验收 4) 与两级检查的材料: 用量 (调用与 token 取预算, 尝试与失败级别取留痕), 各判断的建议动作与
    固定输出的对照, 签字报告的意见类型, 解析不了的证据指针, 缺模型结论的判断对象, 重跑与非 normal 的任务."""
    deps, eid = runner.deps, walk.eid
    out: dict[str, Any] = {"engagement_id": eid, "gates": walk.gates, "fin_disposition": walk.fin}
    out["candidate_opinion"] = walk.candidate
    out["reruns"] = {f"{pid} {subject}": n for (pid, subject), n in sorted(walk.failures.items())}
    out["troubled_tasks"] = [list(v) for _, v in sorted(walk.troubled.items())]
    out["decisions"] = dict(sorted(Counter(decision for _, decision in walk.decisions).items()))
    entries = await deps.trail.list(eid, TrailKind.llm_call, limit=10_000) if eid else []
    out["usage"] = summary(entries, budget)
    if eid is None:
        return out
    store = deps.output_store
    judged = latest_model_judgments(await store.latest(eid, Judgment))
    rows = []
    for (pid, subject), j in sorted(judged.items()):
        expected = fixture_action(pid, subject)
        rows.append({"procedure_id": pid, "subject_id": subject, "proposed_action": j.proposed_action.value})
        rows[-1] |= {"fixture": expected, "same": expected == j.proposed_action.value}
    out["judgments"] = rows
    out["differences"] = [r for r in rows if r["fixture"] is not None and not r["same"]]
    expected_subjects = {(pid, p.stem) for pid in JUDGING for p in (XERO.runtime / pid).glob("*.json")}
    out["missing_subjects"] = [f"{pid} {subject}" for pid, subject in sorted(expected_subjects - set(judged))]
    signed = [r for r in await store.latest(eid, AuditReportVersion) if r.status is ReportStatus.signed]
    out["report_opinion"] = [r.opinion_type.value for r in signed]
    out["report_heading"] = [r.sections["opinion"].strip().splitlines()[:1] for r in signed]
    out["contracts"] = len(await deps.financial_store.contracts(eid))
    out["confirmations"] = len(await store.latest(eid, Confirmation))
    out["pointers"], out["unresolved_pointers"] = await unresolved_pointers(runner, eid)
    out["plans"] = sorted([p.id, p.status.value] for p in await store.latest(eid, AuditPlan))
    moves = await deps.trail.list(eid, TrailKind.transition, limit=10_000)
    out["entered_p2_by"] = [
        e.procedure_id for e in moves if (e.payload["object"], e.payload["to"]) == ("engagement", "P2_Executing")
    ]
    return out


async def unresolved_pointers(runner: GraphRunner, eid: str) -> tuple[int, list[str]]:
    """项目的全部证据指针 (范围同 R56 的 everything: 判断, 合同, 函证, 例外, 评分卡, 人工结果) 经存储解析, 客户画像
    指针按项目采用的画像解析; 返回指针数与解析不了的 (来源, 定位, 页与原因). R250 的程序级评测也用."""
    deps = runner.deps
    engagement = await deps.engagement_store.get(eid)
    profile = await deps.engagement_store.client_profile(engagement.client_id) if engagement else None
    resolver = PointerResolver(deps.judgments.validator, eid, None, profile)
    started = Started(runner, deps.output_store, deps.engagement_store)  # type: ignore[arg-type]
    unresolved = []
    pointers = await evidence_pointers(started, eid, everything=True)
    for p in pointers:
        resolved, reason = await resolver.resolve(p)
        if resolved is None:
            unresolved.append(f"{p.source_type} {p.source_id} {p.locator} p{p.page}: {reason}")
    return len(pointers), unresolved


def assert_hard(data: dict[str, Any]) -> None:
    """硬性检查 (需求 R311 验收 2): 走到归档 (调用方已由驱动保证, run_error 始终为空); 每个判断对象有模型结论, 重跑至多
    一次 (驱动保证); 证据指针全部解析通过; 签字报告一份, 意见类型与 FIN-01 决定一致, 意见段首行为该意见的标题; 合同 3 份
    与函证 2 户都已抽取; 总体审计计划两个修订都出了 (每个修订一次通过, 或 failed 后重跑一次), PLAN-1 已取代, PLAN-2
    批准后由它推进到执行阶段 (需求 R317). 软性的差异只告警 (warn_soft)."""
    assert data["missing_subjects"] == [], data["missing_subjects"]
    assert data["unresolved_pointers"] == [], data["unresolved_pointers"]
    assert data["pointers"] > 30
    heading = REPORT_HEADINGS[OpinionType(data["fin_disposition"])][0]
    assert (data["report_opinion"], data["report_heading"]) == ([data["fin_disposition"]], [[heading]])
    assert (data["contracts"], data["confirmations"]) == (3, 2)
    assert all(n <= 1 for n in data["reruns"].values()), data["reruns"]
    assert data["plans"] == [["PLAN-1", "superseded"], ["PLAN-2", "approved"]], data["plans"]
    assert data["entered_p2_by"] == ["PLN-15"], data["entered_p2_by"]


def warn_soft(data: dict[str, Any]) -> None:
    """软性检查 (需求 R311 验收 3): 判断的建议动作与固定输出不同的逐条告警 (如 BA-091, XJ-499 是否为 misstatement)."""
    for row in data["differences"]:
        warnings.warn(
            f"{row['procedure_id']} {row['subject_id']}: 建议动作 {row['proposed_action']}, 固定输出 {row['fixture']}",
            stacklevel=1,
        )


async def real_walk(
    tmp_path: Path,
    budget: Budget,
    name: str,
    fin: FinPolicy = candidate_opinion,
    root: Path = EVAL_DIR,
    **settings: Any,
) -> tuple[LiveWalk, dict[str, Any], list[TrailEntry]]:
    """组装根的真实依赖 (真实 Postgres 存储与检查点, 规则 SQL, 数据集载入, 登记与函证 mock; 功能集为演示配置, 大模型按
    settings) 上照页面走法走到归档, 外面包上预算; 汇总写 root (缺省 var/eval) 下的 <name>-<UTC>.json (中途失败也写出
    已走到的部分).
    返回走法记录, 汇总与该项目的 llm_call 留痕. 真实模型的用例与连库自测共用."""
    async with real_storage(
        tmp_path,
        dataset=XERO_DATASET,
        period=XERO_PERIOD,
        load_dataset=False,
        fresh_profile=False,
        FEATURES=",".join(sorted(DEMO_FEATURES)),
        **settings,
    ) as run:
        install_budget(run.deps, budget)
        service = run.data_service()
        runner = run.s.runner
        walk = LiveWalk()
        try:
            async with api_client(runner, run.owner, deps=run.deps, xero=xero_link(run.deps, service)) as client:
                await drive(client, runner, service, walk, fin=fin, budget=budget)
        finally:
            data = await evaluate(runner, walk, budget)
            budget.reported = True
            write_summary(name, data, root=root)
        calls = await run.deps.trail.list(walk.eid or "", TrailKind.llm_call, limit=10_000)
        return walk, data, calls


@pytest.mark.slow
@pytest.mark.external
@pytest.mark.live(max_calls=MAX_CALLS)  # 用量上限 (需求 R115), LIVE_MAX_CALLS 等可覆盖
async def test_demo_walkthrough_live(tmp_path: Path, live_budget: Budget) -> None:
    """验收 1-4: 真实大模型下照页面走法从新建项目走到归档 (real_walk, LLM_MODE live); 硬性与软性检查见 assert_hard,
    warn_soft; 闸门依次为 ACC-01, PLN-01/03, PLN-15 两次, FIN-01, RPT-01 两次; 留痕的模型为配置的; 汇总写
    var/eval/demo-live-<UTC>.json."""
    configured = live_llm_settings()  # 没配时跳过
    XERO.ready()
    llm = {key: getattr(configured, key) for key in LLM_KEYS}
    walk, data, calls = await real_walk(tmp_path, live_budget, "demo-live", LLM_MODE="live", **llm)
    warn_soft(data)
    assert_hard(data)
    assert walk.gates == GATES
    assert calls and all(e.model and e.model.startswith(configured.LLM_MODEL) for e in calls)


@pytest.mark.slow
@pytest.mark.external
async def test_live_path_on_real_storage_with_mock_llm(tmp_path: Path) -> None:
    """连库自测: 真实模型用例的同一条路径 (real_walk), 大模型换成组装根的 mock (按项目数据集读演示运行副本); FIN-01 选
    无保留意见 (固定输出的报告两稿按它写). 走到归档, 硬性检查都过, 没有软性差异与重跑, 调用 47 次 (同 R287 的离线
    走法)."""
    XERO.ready()
    budget = Budget(max_calls=MAX_CALLS)
    walk, data, calls = await real_walk(
        tmp_path / "run",
        budget,
        "demo-live-selftest",
        fin=lambda material: OpinionType.unqualified.value,
        root=tmp_path,
        LLM_MODE="mock",
    )
    assert_hard(data)
    assert (data["differences"], data["reruns"], data["troubled_tasks"], data["usage"]["calls"]) == ([], {}, [], 47)
    assert walk.gates == GATES and len(calls) == 47


async def offline_walk(tmp_path: Path, failing: dict[tuple[str, str], int]) -> tuple[LiveWalk, dict[str, Any]]:
    """不联网走一遍: 同一驱动与检查, 大模型换成演示运行副本的固定输出 (离线假存储, 规则引擎为 XERO_RULES, 同 R287 的
    离线走法; failing 中的对象先失败几次, 见 FlakyLLM), 外面照样包上预算. FIN-01 选无保留意见: 固定输出的报告两稿按
    无保留意见写 (候选意见为保留; 真实模型按所选意见起草, 见 test_demo_walkthrough_live). 汇总写在临时目录."""
    XERO.ready()
    store = CreatingEngagementStore()
    llm = SwitchableLLMClient(FlakyLLM(LayeredMockLLMClient([XERO.runtime]), failing), "mock")
    datasets = demo_source()
    deps = new_xero_deps(
        trail=FakeTrail(),
        llm=llm,
        review_llm=llm,
        rules=FakeRuleEngine(dict(XERO_RULES)),
        features=DEMO_FEATURES,
        engagement_store=store,
    )
    deps = replace(deps, registry=DatasetRegistrySource(store, datasets.supplement_dir))
    budget = Budget(max_calls=MAX_CALLS)
    install_budget(deps, budget)
    runner = offline_runner(deps)
    service = DataService(deps, runner, demo=datasets)
    walk = LiveWalk()
    async with api_client(runner, NUMBAT_AUDITOR.id, xero=xero_link(deps, service)) as client:
        await drive(client, runner, service, walk, fin=lambda material: OpinionType.unqualified.value, budget=budget)
    data = await evaluate(runner, walk, budget)
    write_summary("demo-live-selftest", data, root=tmp_path)
    return walk, data


@pytest.mark.external
async def test_live_driver_on_mock_outputs(tmp_path: Path) -> None:
    """不联网的自测 (offline_walk): 走到归档, 硬性检查都过, 没有软性差异 (固定输出与自己比), 没有重跑与非 normal 任务;
    闸门依次为 ACC-01, PLN-01/03, PLN-15 两次, FIN-01, RPT-01 两次; 调用 47 次 (同 R287 的离线走法), 17 个判断对象,
    决定同演示决定表."""
    walk, data = await offline_walk(tmp_path, {})
    assert_hard(data)
    assert (data["differences"], data["reruns"], data["troubled_tasks"]) == ([], {}, [])
    assert (walk.gates, walk.candidate, data["usage"]["calls"]) == (GATES, "qualified", 47)
    actions = {(r["procedure_id"], r["subject_id"]): r["proposed_action"] for r in data["judgments"]}
    assert len(actions) == 17 and (actions["CSH-02", "BA-091"], actions["GEN-02", "XJ-499"]) == ("misstatement",) * 2
    assert data["decisions"] == {"confirm": 14, "misstatement": 2, "override": 1}
    assert len(list(tmp_path.glob("demo-live-selftest-*.json"))) == 1


ORC1021 = XERO17_INVOICES[0]  # REV-02 判断对象里没有规则例外的一张 (有例外的 ORC1026 失败时转 unattributed, 不能重跑)


@pytest.mark.parametrize(
    ("failing", "rerun", "troubled"),
    [
        (("REV-02", ORC1021), f"REV-02 {ORC1021}", ["REV-02", ORC1021, "failed"]),
        (("PLN-15", "r2"), "PLN-15 r2", ["PLN-15", "r2", "failed"]),  # 第 2 修订整条失败 (需求 R317)
    ],
    ids=["rev02", "pln15"],
)
@pytest.mark.external
async def test_live_driver_reruns_a_failed_object_once(
    tmp_path: Path, failing: tuple[str, str], rerun: str, troubled: list[str]
) -> None:
    """自测重跑一路: 该对象 (ORC1021 的 REV-02 判断, 或 PLN-15 第 2 修订的计划叙述) 前 3 次调用输出不是 JSON, 成为
    failed 任务; 驱动重跑一次, 第 2 次执行给出结果, 照常走到归档, 硬性检查都过; 汇总记重跑一次与这条 failed 任务,
    调用多 3 次. ORC1021 的结论仍为固定输出的 accept."""
    _, data = await offline_walk(tmp_path, {failing: 3})
    assert_hard(data)
    assert (data["reruns"], data["troubled_tasks"]) == ({rerun: 1}, [troubled])
    assert (data["usage"]["calls"], data["decisions"]["rerun"]) == (50, 1)
    [row] = [r for r in data["judgments"] if r["subject_id"] == ORC1021]
    assert (row["proposed_action"], row["same"]) == ("accept", True)


@pytest.mark.external
async def test_failed_object_with_rule_findings_is_a_hard_failure(tmp_path: Path) -> None:
    """有规则例外的对象 (ORC1026 带 REV-08 的 R03-1) 判断失败时转为 unattributed 任务 (复核队列的规则, 不能重跑):
    驱动按决定表推翻, 照常走到归档; 该对象没有模型结论, 硬性检查记为缺结论而不过 (需求 R311 验收 2)."""
    _, data = await offline_walk(tmp_path, {("REV-02", ORC1026): 3})
    assert (data["reruns"], data["troubled_tasks"]) == ({}, [["REV-02", ORC1026, "unattributed"]])
    assert data["missing_subjects"] == [f"REV-02 {ORC1026}"]
    with pytest.raises(AssertionError, match=ORC1026):
        assert_hard(data)


def view_of(kind: str, subject: str, **extra: Any) -> dict[str, Any]:
    """合成的复核队列一项 (只填驱动用到的字段)."""
    task = {"id": f"T-{subject}", "kind": kind, "procedure_id": "GEN-02", "target_version": 1, "subject_id": None}
    target: dict[str, Any] = {"subject_id": subject} if kind != "unattributed" else {"detail": {"entry_no": subject}}
    return {"task": task, "target": target, "allowed_resolutions": [], "draft_rules": {}} | extra


def test_review_bodies_follow_the_decision_table() -> None:
    """各类任务的决定都合复核接口的请求体 (ReviewRecordIn), 且按演示决定表: 争议任务原判断与期望一致时维持, 否则
    "两者都不采用" 给出期望的结论, 期望错报时附草稿; 无解释的例外认定错报或推翻; 同一对象第二次失败即判失败."""
    table, walk = decision_table("USR-1"), LiveWalk()
    both = {"allowed_resolutions": ["a6_upheld", "a7_upheld", "neither"]}
    cases = {
        "a6 认定错报, 期望错报": (
            view_of("dispute", "XJ-499", **both, draft_rules={"a6_upheld": "required"}),
            "a6_upheld",
        ),
        "a6 接受, 期望错报": (view_of("dispute", "XJ-499", **both, draft_rules={"a6_upheld": "hidden"}), "neither"),
        "a6 认定错报, 期望确认": (
            view_of("dispute", "XJ-397", **both, draft_rules={"a6_upheld": "required"}),
            "neither",
        ),
        "a6 接受, 期望确认": (view_of("dispute", "XJ-397", **both, draft_rules={"a6_upheld": "hidden"}), "a6_upheld"),
        "期望推翻": (view_of("dispute", "XJ-575", **both, draft_rules={"a6_upheld": "hidden"}), "neither"),
    }
    for name, (view, resolution) in cases.items():
        body = review_body(view, table, walk)
        record = ReviewRecordIn.model_validate(body)
        assert record.dispute_resolution == resolution, name
        assert (record.misstatement_draft is not None) == ("期望错报" in name), name
    assert ReviewRecordIn.model_validate(review_body(view_of("unattributed", "XJ-499"), table, walk)).decision == (
        "misstatement"
    )
    assert ReviewRecordIn.model_validate(review_body(view_of("unattributed", "XJ-601"), table, walk)).decision == (
        "override"
    )
    failed = view_of("failed", "XJ-601")
    failed["task"]["subject_id"] = "XJ-601"
    assert ReviewRecordIn.model_validate(review_body(failed, table, walk)).decision == "rerun"
    with pytest.raises(pytest.fail.Exception, match="重跑后又失败"):
        review_body(failed, table, walk)
