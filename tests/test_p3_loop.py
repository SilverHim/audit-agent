"""需求 R167: 错报去向分流与两轮闭环的图测试 (功能 misstatement; 设计稿 v2.1 第 7.2 节第 20 至 33 步).

Xero 形状的两轮闭环在主图上跑通. 检查点以 fan_in 身份写入 P2 已结清的状态: 两条错报 M-D-XJ499 (M-1) 与
M-D-CSH02-091 (M-5) 为 v1 identified, 带来源判断 (misstatement_fakes.fin03_state).
- 真实的 A8 执行 FIN-03, FIN-09 并整理 FIN-08 的登记, A10 派发 FIN-08; 主模型读 Xero fixture 正本, 个别用例换脚本回复.
- 临时程序库另加桩 FIN-15 (final_review): 第 1 次执行出 J-FIN-15-1-826, -804, -805 与 normal 任务, 之后不出新判断.
- 更正通知载入的分录同时记进假财务存储 (同真实载入写总账), FIN-09 的指针因此解析得了; FIN-09 的 V01 跟着已载入的分录
  走 (xero_v01_rows 按已载入的凭证号取, R165 严审取舍 3).
- 程序库没有 PLN-19 与 FIN-02, p3_reassess 与 completion 为空操作.
- 分录段的 GEN-05 增量扫描 (R194) 还没合入, 本文件只测 R165, R166 这一段.
决定依次: FIN-03 confirm ×2, FIN-08 第 1 轮登记, 更正通知 D-CN1, FIN-09 confirm ×2, 对桩 FIN-15 三条认定错报, FIN-03
confirm ×3, FIN-08 第 2 轮登记, D-CN2, FIN-09 confirm ×3, 之后停在 FIN-01 闸门 (STEPS). 变体: 不匹配后重新沟通, 部分
更正通知, 两种中断后重启 (进程中途被杀, 决定写进 Store 后中断), FIN-01 退回.
"""

import asyncio
import functools
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from backend.adapters.llm import MockLLMClient
from backend.agents.base import AgentDeps, RunContext
from backend.contracts.models import (
    Assertion,
    AuditException,
    CorrectionNotice,
    Decision,
    Direction,
    Engagement,
    GateDecision,
    GateVerdict,
    Judgment,
    Misstatement,
    MisstatementDraft,
    MisstatementStatus,
    MisstatementType,
    Phase,
    ProcedureRun,
    ReviewRecord,
    ReviewTask,
    TaskKind,
    TaskStatus,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.ports import LLMRequest, LLMResponse
from backend.contracts.state import StatePatch
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import P3_NODES
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner, correction_wait, resume_required
from backend.services.features import BASELINE, MISSTATEMENT
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    EngagementWrite,
    FakeOutputStore,
    FakeRuleEngine,
    FakeTrail,
    ScriptA8,
    finished,
    make_ctx,
    stopping_graph,
)
from tests.fixture_sets import XERO
from tests.interrupts import CRASH, CrashingTrail
from tests.misstatement_fakes import CSH02, FIN09, XJ499, fin03_deps, fin03_state, fin09_fixture, notice
from tests.misstatement_fakes import round2_sources as fin15_proposals
from tests.test_correction_verification import MISMATCH
from tests.test_fin_gate import fin_decision
from tests.test_misstatement_communication import DECIDED_1, DECIDED_2, registered
from tests.test_p3_flow import stub_library
from tests.test_review_decisions import record
from tests.xero_fakes import (
    LoadingDatasets,
    XeroFakeDataStore,
    registration,
    round_file,
    xero_engagement,
    xero_fin09_result,
    xero_financial_store,
)

pytestmark = pytest.mark.external  # Xero 快照, 补造与 Xero fixture

EID = ENGAGEMENT.id  # 同 xero_engagement().id; 收集阶段不读快照
CONFIG = GraphRunner.config(EID)
FEATURES = BASELINE | {MISSTATEMENT}
PACK = ("FIN-03", "FIN-08", FIN09, "FIN-04", "FIN-15", "FIN-01")  # 完成阶段的程序包 (FIN-15 为桩)
F03, F04, F05 = "M-D-F03", "M-D-F04", "M-D-F05"  # 第 2 轮的三条 (认定自桩 FIN-15 对 826, 804, 805 的判断)
ROUND1, ROUND2 = [XJ499, CSH02], [F03, F04, F05]
ROUND2_AT = datetime(2026, 10, 22, tzinfo=UTC)  # 第 2 轮更正通知: AJE-X02 至 X04 录入 (10-21 23:30Z) 之后
RECOMMUNICATED_AT = datetime(2026, 10, 23, tzinfo=UTC)  # 不匹配变体: 重新沟通的登记 (第 2 轮通知之后)


def draft(kind: MisstatementType, account: str, amount: int, code: str) -> MisstatementDraft:
    return MisstatementDraft(
        type=kind,
        account=account,
        assertion=Assertion.Completeness,
        amount_cents=amount,
        direction=Direction.understated,
        account_code=code,
    )


# 对桩 FIN-15 三条判断认定错报 (验收 2): 科目 → (决定 ID, 草稿)
FOUND = {
    "826": ("D-F03", draft(MisstatementType.factual, "Superannuation payable (826)", 426120, "826")),
    "804": ("D-F04", draft(MisstatementType.factual, "Wages payable (804)", 201000, "804")),
    "805": ("D-F05", draft(MisstatementType.judgmental, "Annual leave (805)", 268736, "805")),
}


class Replies:
    """主模型与复核模型: 读 Xero fixture 正本; overrides 中 (程序, fixture_key) 的回复 (原文) 依次先用, 用完再读正本."""

    def __init__(self, overrides: dict[tuple[str, str], list[str]] | None = None) -> None:
        self.fixtures = MockLLMClient(XERO.fixtures)
        self.overrides = {key: list(raws) for key, raws in (overrides or {}).items()}
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, output_model: type[Any]) -> LLMResponse[Any]:
        self.requests.append(req)
        queue = self.overrides.get((req.procedure_id, req.fixture_key))
        if not queue:
            return await self.fixtures.complete(req, output_model)
        raw = queue.pop(0)
        try:
            output, error = output_model.model_validate_json(raw), None
        except ValidationError as e:
            output, error = None, str(e)
        return LLMResponse(output=output, raw=raw, model="scripted", input_tokens=1, output_tokens=1, error=error)


@dataclass
class Loop:
    """跑两轮闭环的主图: runner (恢复后换成新的), 依赖, 各次停下时的 (节点, 等什么), 桩 FIN-15 收到的 run_no.
    - kill_at 中的节点下一次开始执行时取消后台任务 (模拟进程中途被杀), 由 restart 换新 runner 接着跑.
    - crash_next 为真时, 下一个决定写进 Store 后即中断 (CrashingTrail), 由 restart 补做."""

    runner: GraphRunner
    deps: AgentDeps
    stops: list[tuple[str, str]] = field(default_factory=list)
    fin15_runs: list[int] = field(default_factory=list)
    kill_at: set[str] = field(default_factory=set)
    killed: list[str] = field(default_factory=list)
    crash_next: bool = False
    crashed: list[str] = field(default_factory=list)
    restarted_at: list[tuple[tuple[str, ...], bool]] = field(default_factory=list)

    @property
    def store(self) -> FakeOutputStore:
        assert isinstance(self.deps.output_store, FakeOutputStore)
        return self.deps.output_store

    @property
    def trail(self) -> CrashingTrail:
        assert isinstance(self.deps.trail, CrashingTrail)
        return self.deps.trail

    async def settle(self) -> None:
        """等后台停下 (被取消的照样等完), 没有出错; 没被取消时记下停在哪里."""
        task = self.runner.tasks.get(EID)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        assert self.runner.run_error(EID) is None, self.runner.run_error(EID)
        if task is None or not task.cancelled():
            self.stops.append(await stop_of(self.runner))

    async def submit(self, decision: ReviewRecord | GateDecision | CorrectionNotice) -> None:
        """经唯一写入口提交, 等后台停下. crash_next 时决定写进 Store 后中断: 留痕与检查点都没有它, 重启后补做."""
        submit = GraphDecisionSubmit(self.runner.app, self.deps, self.runner).submit(EID, decision)
        if not self.crash_next:
            await submit
            await self.settle()
            return
        self.crash_next = False
        self.trail.fail_next = True
        with pytest.raises(RuntimeError, match=CRASH):
            await submit
        snapshot = await self.runner.app.aget_state(CONFIG)
        assert decision.id in {r.id for r in self.store.rows if isinstance(r, type(decision))}
        assert await self.trail.find_success(EID, decision.id) is None
        assert await resume_required(self.deps, await self.engagement(), snapshot) == decision.id
        self.crashed.append(decision.id)
        await self.restart()

    async def engagement(self) -> Engagement:
        engagement = await self.deps.engagement_store.get(EID)
        assert engagement is not None
        return engagement

    async def restart(self) -> None:
        """进程重启: 新 runner (同一检查点) recover, 后台接着跑到下一次停下. 记下重启时检查点的 (next, 是否在节点内
        等待)."""
        snapshot = await self.runner.app.aget_state(CONFIG)
        self.restarted_at.append((snapshot.next, any(t.interrupts for t in snapshot.tasks)))
        self.runner = GraphRunner(self.runner.app, self.deps)
        await self.runner.recover()
        await self.settle()

    def latest(self, model: type[Any], oid: str) -> Any:
        return max((r for r in self.store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)

    def versions(self, mid: str) -> list[Misstatement]:
        return sorted(
            (r for r in self.store.rows if isinstance(r, Misstatement) and r.id == mid), key=lambda r: r.version
        )

    async def pending(self, procedure_id: str) -> list[ReviewTask]:
        tasks = await self.store.latest(EID, ReviewTask, status=TaskStatus.pending)
        return sorted((t for t in tasks if t.procedure_id == procedure_id), key=lambda t: t.id)

    def runs(self, procedure_id: str) -> list[str]:
        return sorted({r.id for r in self.store.rows if isinstance(r, ProcedureRun) and r.procedure_id == procedure_id})


async def stop_of(runner: GraphRunner) -> tuple[str, str]:
    """图停在哪里: 节点内等待为 (节点, 载荷的程序或等待种类), 闸门前静态中断为 (闸门节点, "gate")."""
    snapshot = await runner.app.aget_state(CONFIG)
    waits = [(t.name, w.value) for t in snapshot.tasks for w in t.interrupts]
    if waits:
        [(node, payload)] = waits
        return node, payload.get("procedure_id") or payload["awaiting"]
    assert snapshot.next, "图既不在等待也没有下一步"
    return snapshot.next[0], "gate"


def killable(
    loop_ref: list[Loop], name: str, node: Callable[..., Awaitable[StatePatch]]
) -> Callable[..., Awaitable[StatePatch]]:
    """节点外包一层: 该节点在 kill_at 中时, 这次开始执行即取消后台任务, 节点本身不执行 (检查点停在上一步之后)."""

    @functools.wraps(node)
    async def wrapped(state: Any, **injected: Any) -> StatePatch:
        loop = loop_ref[0]
        if name in loop.kill_at:
            loop.kill_at.discard(name)
            loop.killed.append(name)
            loop.runner.tasks[EID].cancel()
            raise asyncio.CancelledError
        return await node(state, **injected)

    return wrapped


async def start(tmp_path: Path, llm: Replies | None = None) -> Loop:
    """见模块 docstring: 推进到第一次停下 (misstatement_triage 等 FIN-03 的复核)."""
    financial = xero_financial_store()
    rules = FakeRuleEngine({FIN09: xero_fin09_result(financial)})
    client = llm if llm is not None else Replies()
    deps = fin03_deps(
        CrashingTrail(),
        client,
        rules=rules,
        financial_store=financial,
        library=stub_library(tmp_path / "knowledge", ["FIN-15"]),
        features=FEATURES,
    )
    deps = replace(deps, datasets=LoadingDatasets(cast(XeroFakeDataStore, deps.data)))
    loop_ref: list[Loop] = []

    def propose(ctx: RunContext) -> StatePatch:
        """桩 FIN-15: 第 1 次执行对 826, 804, 805 各出一条建议认定错报的判断与 normal 任务, 之后什么都不出."""
        run = finished(ctx.run)
        loop_ref[0].fin15_runs.append(run.run_no)
        if run.run_no > 1:
            return {"procedure_runs": [run]}
        judgments = fin15_proposals()
        tasks = [deps.queue.make_task(TaskKind.normal, j, "FIN-15", run.id, NOW) for j in judgments]
        return {"procedure_runs": [run], "judgments": judgments, "review_tasks": tasks}

    nodes = GraphNodes(deps, 2)
    nodes.agents["FIN-15"] = ScriptA8(deps, propose)
    for name in (*P3_NODES, "completion"):
        setattr(nodes, name, killable(loop_ref, name, getattr(nodes, name)))
    runner = GraphRunner(stopping_graph(nodes, ()), deps)
    loop = Loop(runner, deps)
    loop_ref.append(loop)

    engagement = xero_engagement()
    pack = [make_ctx(deps, pid, engagement=engagement).procedure for pid in PACK]
    state = fin03_state(phase=Phase.P2_Executing)
    async with deps.output_store.transaction(EID):
        await deps.output_store.put(EID, [*pack, *state["misstatements"]], actor="A1")
    await runner.app.aupdate_state(CONFIG, state | {"procedure_pack": pack}, as_node="fan_in")
    runner.acquire(EID)
    runner.launch(EID, None)
    await loop.settle()
    return loop


# ── 各步决定 ──


async def confirm_all(loop: Loop, procedure_id: str) -> None:
    """依次确认该程序的全部待决任务 (按任务 ID)."""
    for task in await loop.pending(procedure_id):
        await loop.submit(record(task.id, id=f"D-{task.id}"))


async def register(loop: Loop, n: int, at: datetime) -> None:
    """对 FIN-08 待决的 manual 任务登记第 n 轮补造记录 (都同意), 依据的错报版本为当前的."""
    [task] = await loop.pending("FIN-08")
    current = await loop.store.latest(EID, Misstatement)
    await loop.submit(registered(task.id, registration(n, current), at.date(), at, oid=f"D-M{task.run_id[-1]}"))


async def propose_found(loop: Loop) -> None:
    """对桩 FIN-15 的三条判断各提交 misstatement (FOUND)."""
    for task in await loop.pending("FIN-15"):
        rid, found = FOUND[task.target_id.rsplit("-", 1)[-1]]
        await loop.submit(record(task.id, Decision.misstatement, id=rid, misstatement_draft=found))


ROUND2_NOTICE = notice(
    "D-CN2", misstatement_ids=ROUND2, adjustment_entry_nos=["AJE-X02", "AJE-X03", "AJE-X04"], decided_at=ROUND2_AT
)
Step = Callable[[Loop], Awaitable[None]]
STEPS: list[tuple[str, Step]] = [  # 两轮闭环的各步 (名字, 动作), 依次
    ("FIN-03 第 1 轮", lambda loop: confirm_all(loop, "FIN-03")),
    ("FIN-08 第 1 轮", lambda loop: register(loop, 1, DECIDED_1)),
    ("D-CN1", lambda loop: loop.submit(notice())),
    ("FIN-09 第 1 轮", lambda loop: confirm_all(loop, FIN09)),
    ("FIN-15 认定", propose_found),
    ("FIN-03 第 2 轮", lambda loop: confirm_all(loop, "FIN-03")),
    ("FIN-08 第 2 轮", lambda loop: register(loop, 2, DECIDED_2)),
    ("D-CN2", lambda loop: loop.submit(ROUND2_NOTICE)),
    ("FIN-09 第 2 轮", lambda loop: confirm_all(loop, FIN09)),
]


async def drive(loop: Loop, steps: Sequence[tuple[str, Step]] = STEPS) -> Loop:
    for _, step in steps:
        await step(loop)
    return loop


def outcome(loop: Loop) -> dict[str, Any]:
    """可比较的结果: Store 中各对象的全部版本 (错报, 判断, 任务, 汇总表, 例外), 执行记录, 决定与更正通知的 ID;
    留痕中 decision 与 transition 各键的条数, llm_call 各程序的条数 (恢复不重复写, 不重复调模型)."""

    def of(model: type[Any], fields: Sequence[str]) -> list[tuple[Any, ...]]:
        rows = [r for r in loop.store.rows if isinstance(r, model)]
        return sorted({(r.id, r.version, *(json.dumps(getattr(r, f), default=str) for f in fields)) for r in rows})

    return {
        "misstatements": of(
            Misstatement,
            [
                "status",
                "classification_judgment_id",
                "verified_by_judgment_id",
                "adjustment_entry_nos",
                "correction_notice_id",
                "needs_recommunication",
            ],
        ),
        "judgments": of(Judgment, ["review_status", "misstatement_update"]),
        "tasks": of(ReviewTask, ["status", "kind"]),
        "schedules": of(UncorrectedSchedule, ["items", "corrected_ids", "total_cents", "candidate_opinion"]),
        "runs": of(ProcedureRun, ["status"]),
        "records": sorted({r.id for r in loop.store.rows if isinstance(r, ReviewRecord)}),
        "notices": sorted({r.id for r in loop.store.rows if isinstance(r, CorrectionNotice)}),
        "exceptions": of(AuditException, ["disposition", "attributed_by", "source_ids"]),
        "trail": sorted(
            Counter(
                (e.kind.value, e.idempotency_key)
                for e in loop.trail.entries
                if e.kind in (TrailKind.decision, TrailKind.transition)
            ).items()
        ),
        "llm_calls": sorted(
            Counter(e.procedure_id for e in loop.trail.entries if e.kind is TrailKind.llm_call).items()
        ),
    }


async def path(loop: Loop) -> list[str]:
    """从写入检查点起依次经过的图节点 (相邻重复的合并), 同 test_p3_flow."""
    history = [s async for s in loop.runner.app.aget_state_history(CONFIG)]
    steps = [s.next[0] for s in reversed(history) if s.next]
    return [n for i, n in enumerate(steps) if i == 0 or steps[i - 1] != n]


def merged(stops: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [s for i, s in enumerate(stops) if i == 0 or stops[i - 1] != s]


# 两轮闭环依次停下的地方 (验收 1): 节点内等人 (FIN-03, FIN-08, FIN-09, 桩 FIN-15 的复核), 等更正通知, FIN-01 闸门
STOPS = [
    ("misstatement_triage", "FIN-03"),
    ("communicate", "FIN-08"),
    ("await_correction", "correction"),
    ("verify_correction", FIN09),
    ("final_review", "FIN-15"),
    ("misstatement_triage", "FIN-03"),
    ("communicate", "FIN-08"),
    ("await_correction", "correction"),
    ("verify_correction", FIN09),
    ("gate_fin", "gate"),
]
ROUND = ["misstatement_triage", "communicate", "await_correction", "verify_correction", "p3_reassess", "summarize"]
PATH = [*ROUND, "final_review", *ROUND, "final_review", "completion", "gate_fin"]  # 经过的节点 (相邻重复合并)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """不中断地走完两轮 (STEPS) 的结果 (outcome), 恢复的用例与它比较."""

    async def run() -> dict[str, Any]:
        return outcome(await drive(await start(tmp_path_factory.mktemp("baseline"))))

    return asyncio.run(run())


async def test_two_rounds_reach_gate_fin(tmp_path: Path, baseline: dict[str, Any]) -> None:
    """验收 1-5: 依次停在 STOPS (全程 run_error 为空, Loop.settle 每次都查), 经过的节点为 PATH.
    - 决定: FIN-03 confirm ×2, FIN-08 第 1 轮, D-CN1 [AJE-X01, AJE-X05], FIN-09 confirm ×2, 对 T-J-FIN-15-1-826, -804,
      -805 认定错报 (D-F03 至 D-F05), FIN-03 confirm ×3, FIN-08 第 2 轮, D-CN2 [AJE-X02 至 X04], FIN-09 confirm ×3.
    - 执行: FIN-03, FIN-08, FIN-09, FIN-04, 桩 FIN-15 各 2 次. SCHEDULE v1 没有未更正的项目, corrected_ids 为第 1 轮
      两条, total 0, unqualified; v2 的 corrected_ids 为 5 条, total 0, 重要性 32431, unqualified; FIN-01 闸门绑定 v2.
    - 错报版本: M-D-XJ499 与 M-D-F03 都是 v1 identified → v2 已分类 → v3 communicated → v4 verifying → v5 corrected,
      分类, 通知与验证依次为 J-FIN-03-1 / D-CN1 / J-FIN-09-1 (AJE-X01) 与 J-FIN-03-2 / D-CN2 / J-FIN-09-2 (AJE-X02).
    - 阶段 P3_Completion 只随第 1 次分诊 (RUN-FIN-03-1) 写, 迁移留痕记在 FIN-03 下, 回流不再写."""
    loop = await drive(await start(tmp_path))

    assert merged(loop.stops) == STOPS
    assert await path(loop) == PATH
    tasks = sorted({r.task_id for r in loop.store.rows if isinstance(r, ReviewRecord)})
    assert tasks == sorted(
        [
            *(f"T-J-FIN-03-1-{m}-v1" for m in ROUND1),
            *(f"T-J-FIN-09-1-{m}-v1" for m in ROUND1),
            *(f"T-J-FIN-15-1-{code}-v1" for code in FOUND),
            *(f"T-J-FIN-03-2-{m}-v1" for m in ROUND2),
            *(f"T-J-FIN-09-2-{m}-v1" for m in ROUND2),
            "T-RUN-FIN-08-1-v1",
            "T-RUN-FIN-08-2-v1",
        ]
    )
    found = {
        r.id: r.misstatement_draft
        for r in loop.store.rows
        if isinstance(r, ReviewRecord) and r.decision is Decision.misstatement
    }
    assert found == {rid: d for rid, d in FOUND.values()}
    assert sorted({r.id for r in loop.store.rows if isinstance(r, CorrectionNotice)}) == ["D-CN1", "D-CN2"]
    assert loop.latest(CorrectionNotice, "D-CN1").adjustment_entry_nos == ["AJE-X01", "AJE-X05"]

    for pid in ("FIN-03", "FIN-08", FIN09, "FIN-04", "FIN-15"):
        assert loop.runs(pid) == [f"RUN-{pid}-1", f"RUN-{pid}-2"], pid
    assert loop.fin15_runs == [1, 2]
    v1, v2 = sorted((r for r in loop.store.rows if isinstance(r, UncorrectedSchedule)), key=lambda r: r.version)
    assert (v1.version, v1.items, v1.corrected_ids, v1.total_cents, v1.candidate_opinion) == (
        1,
        [],
        ROUND1,
        0,
        "unqualified",
    )
    assert (v2.version, v2.items, v2.corrected_ids, v2.total_cents, v2.materiality_cents, v2.candidate_opinion) == (
        2,
        [],
        [*ROUND1, *ROUND2],
        0,
        32431,
        "unqualified",
    )
    snapshot = await loop.runner.app.aget_state(CONFIG)
    assert (snapshot.values["uncorrected_schedule"].id, snapshot.values["uncorrected_schedule"].version) == (
        "SCHEDULE",
        2,
    )

    for mid, n, cn, entry in ((XJ499, 1, "D-CN1", "AJE-X01"), (F03, 2, "D-CN2", "AJE-X02")):
        history = [
            (
                m.version,
                m.status,
                m.classification_judgment_id,
                m.correction_notice_id,
                m.verified_by_judgment_id,
                m.adjustment_entry_nos,
            )
            for m in loop.versions(mid)
        ]
        classified, verified = f"J-FIN-03-{n}-{mid}", f"J-FIN-09-{n}-{mid}"
        assert history == [
            (1, MisstatementStatus.identified, None, None, None, []),
            (2, MisstatementStatus.identified, classified, None, None, []),
            (3, MisstatementStatus.communicated, classified, None, None, []),
            (4, MisstatementStatus.verifying, classified, cn, None, []),
            (5, MisstatementStatus.corrected, classified, cn, verified, [entry]),
        ], mid
    assert {m: loop.latest(Misstatement, m).status for m in [*ROUND1, *ROUND2]} == dict.fromkeys(
        [*ROUND1, *ROUND2], MisstatementStatus.corrected
    )

    commits = loop.store.commits
    phased = [
        i for i, c in enumerate(commits) if any(isinstance(o, EngagementWrite) and "phase" in o.changes for o in c)
    ]
    runs = {o.id for i in phased for o in commits[i] if isinstance(o, ProcedureRun)}
    dispatched = next(i for i, c in enumerate(commits) if any(getattr(o, "id", None) == "RUN-FIN-08-1" for o in c))
    assert runs == {"RUN-FIN-03-1"} and max(phased) < dispatched  # 第 1 次分诊 (及其重放) 之后不再写阶段
    trail = loop.deps.trail
    assert isinstance(trail, FakeTrail)
    moves = [e for e in trail.entries if e.kind is TrailKind.transition and e.payload["object"] == "engagement"]
    assert [(e.payload["from"], e.payload["to"], e.procedure_id) for e in moves] == [
        ("P2_Executing", "P3_Completion", "FIN-03")
    ]
    assert outcome(loop) == baseline


def history(loop: Loop, mid: str) -> list[tuple[Any, ...]]:
    """错报各版本的 (版本, 状态, 须重新沟通, 更正分录, 更正通知, 验证判断)."""
    return [
        (
            m.version,
            m.status,
            m.needs_recommunication,
            m.adjustment_entry_nos,
            m.correction_notice_id,
            m.verified_by_judgment_id,
        )
        for m in loop.versions(mid)
    ]


def run_order(loop: Loop) -> list[str]:
    """各执行记录第一次写入的先后 (按 Store 的提交)."""
    seen: list[str] = []
    for commit in loop.store.commits:
        seen += [o.id for o in commit if isinstance(o, ProcedureRun) and o.id not in seen]
    return seen


def recommunicated(task: ReviewTask, misstatement: Misstatement) -> ReviewRecord:
    """不匹配变体的第 3 次 FIN-08 登记: 对 M-D-F03 重新沟通 (第 2 轮通知之后), 管理层同意补录 AJE-X02."""
    row = {
        "misstatement_id": misstatement.id,
        "base_version": misstatement.version,
        "communicated_at": "2026-10-22T06:00:00+00:00",
        "communicated_to": round_file(2)["recipient"],
        "responded_at": "2026-10-22T09:00:00+00:00",
        "response": "AJE-X02 漏录, 已补录",
        "agrees_to_correct": True,
    }
    return registered(task.id, [row], RECOMMUNICATED_AT.date(), RECOMMUNICATED_AT, oid="D-M3")


async def test_mismatch_recommunicates_then_corrects(tmp_path: Path) -> None:
    """验收 6: D-CN2 只载入 AJE-X03, X04 (漏了 M-D-F03 的 AJE-X02), 主模型对 826 判未更正 (MISMATCH), FIN-09 出
    X-FIN-09-2-V02-1; 确认后 M-D-F03 v5 communicated, needs_recommunication 真, 其余两条 corrected.
    - 回 communicate: RUN-FIN-08-3 的材料只列 M-D-F03; 登记同意 (D-M3) 后 v6 communicated, 标记清掉.
    - await_correction 只等 M-D-F03; D-CN3 [AJE-X02] → v7 verifying → RUN-FIN-09-3 (主模型读 fixture 正本) → 确认后
      v8 corrected.
    - 之后照常进 p3_reassess, summarize (RUN-FIN-04-2 在 RUN-FIN-09-3 之后, 每轮仍只一次), final_review, FIN-01 闸门;
      SCHEDULE v2 的 corrected_ids 为 5 条."""
    loop = await drive(await start(tmp_path, Replies({(FIN09, "826"): [fin09_fixture("826", **MISMATCH)]})), STEPS[:7])
    partial = notice(
        "D-CN2", misstatement_ids=ROUND2, adjustment_entry_nos=["AJE-X03", "AJE-X04"], decided_at=ROUND2_AT
    )
    await loop.submit(partial)
    [v02] = [x for x in loop.store.rows if isinstance(x, AuditException)]
    assert (v02.id, v02.rule_id, v02.source_ids) == ("X-FIN-09-2-V02-1", "V02", ["826"])
    await confirm_all(loop, FIN09)
    assert history(loop, F03)[-1] == (5, MisstatementStatus.communicated, True, [], "D-CN2", None)
    assert [loop.latest(Misstatement, m).status for m in (F04, F05)] == [MisstatementStatus.corrected] * 2
    assert loop.latest(AuditException, v02.id).attributed_by == f"J-FIN-09-2-{F03}"

    [task] = await loop.pending("FIN-08")
    assert task.manual_request is not None
    assert (task.run_id, [m["id"] for m in task.manual_request.material["misstatements"]]) == ("RUN-FIN-08-3", [F03])
    await loop.submit(recommunicated(task, loop.latest(Misstatement, F03)))
    assert correction_wait(await loop.runner.app.aget_state(CONFIG)) == {
        "awaiting": "correction",
        "misstatement_ids": [F03],
        "records": [],
    }
    await loop.submit(
        notice(
            "D-CN3",
            misstatement_ids=[F03],
            adjustment_entry_nos=["AJE-X02"],
            decided_at=datetime(2026, 10, 24, tzinfo=UTC),
        )
    )
    await confirm_all(loop, FIN09)

    assert merged(loop.stops) == [
        *STOPS[:-1],
        ("communicate", "FIN-08"),
        ("await_correction", "correction"),
        ("verify_correction", FIN09),
        ("gate_fin", "gate"),
    ]
    again = ["communicate", "await_correction", "verify_correction"]
    assert await path(loop) == [*ROUND, "final_review", *ROUND[:4], *again, *ROUND[4:], "final_review", *PATH[-2:]]
    assert history(loop, F03)[4:] == [
        (5, MisstatementStatus.communicated, True, [], "D-CN2", None),
        (6, MisstatementStatus.communicated, False, [], "D-CN2", None),
        (7, MisstatementStatus.verifying, False, [], "D-CN3", None),
        (8, MisstatementStatus.corrected, False, ["AJE-X02"], "D-CN3", f"J-FIN-09-3-{F03}"),
    ]
    assert loop.latest(Misstatement, F03).communicated_at == datetime(2026, 10, 22, 6, tzinfo=UTC)
    assert loop.runs("FIN-08") == ["RUN-FIN-08-1", "RUN-FIN-08-2", "RUN-FIN-08-3"]
    assert loop.runs(FIN09) == ["RUN-FIN-09-1", "RUN-FIN-09-2", "RUN-FIN-09-3"]
    assert loop.runs("FIN-04") == ["RUN-FIN-04-1", "RUN-FIN-04-2"]
    order = run_order(loop)
    assert order.index("RUN-FIN-09-3") < order.index("RUN-FIN-04-2") < order.index("RUN-FIN-15-2")
    schedule = loop.latest(UncorrectedSchedule, "SCHEDULE")
    assert (schedule.version, schedule.items, schedule.corrected_ids) == (2, [], [*ROUND1, *ROUND2])


async def test_partial_notice_waits_for_the_rest(tmp_path: Path) -> None:
    """验收 7: D-CN1 只列 M-D-XJ499 与 [AJE-X01] → RUN-FIN-09-1 只验它 → 确认后回 await_correction, 只等 M-D-CSH02-091
    (FIN-04 还没执行) → D-CN1b {M-D-CSH02-091, [AJE-X05]} → RUN-FIN-09-2 → 确认后两条都 corrected, 第 1 轮 summarize
    执行一次 (RUN-FIN-04-1 在 RUN-FIN-09-2 之后), SCHEDULE v1 的 corrected_ids 为两条. 之后第 2 轮照常走到 FIN-01 闸门,
    FIN-09 第 2 轮为 RUN-FIN-09-3, FIN-04 共两次."""
    loop = await drive(await start(tmp_path), STEPS[:2])
    await loop.submit(notice(misstatement_ids=[XJ499], adjustment_entry_nos=["AJE-X01"]))
    assert [t.id for t in await loop.pending(FIN09)] == [f"T-J-FIN-09-1-{XJ499}-v1"]
    await confirm_all(loop, FIN09)
    snapshot = await loop.runner.app.aget_state(CONFIG)
    assert correction_wait(snapshot) == {"awaiting": "correction", "misstatement_ids": [CSH02], "records": []}
    assert loop.runs("FIN-04") == []
    await loop.submit(notice("D-CN1b", misstatement_ids=[CSH02], adjustment_entry_nos=["AJE-X05"]))
    assert [t.id for t in await loop.pending(FIN09)] == [f"T-J-FIN-09-2-{CSH02}-v1"]
    await confirm_all(loop, FIN09)

    assert merged(loop.stops) == [*STOPS[:4], ("await_correction", "correction"), *STOPS[3:5]]
    assert await path(loop) == [*ROUND[:4], *ROUND[2:], "final_review"]
    assert history(loop, XJ499)[-1] == (
        5,
        MisstatementStatus.corrected,
        False,
        ["AJE-X01"],
        "D-CN1",
        f"J-FIN-09-1-{XJ499}",
    )
    assert history(loop, CSH02)[3:] == [
        (4, MisstatementStatus.verifying, False, [], "D-CN1b", None),
        (5, MisstatementStatus.corrected, False, ["AJE-X05"], "D-CN1b", f"J-FIN-09-2-{CSH02}"),
    ]
    assert loop.runs("FIN-04") == ["RUN-FIN-04-1"]
    order = run_order(loop)
    assert order.index("RUN-FIN-09-2") < order.index("RUN-FIN-04-1")
    schedule = loop.latest(UncorrectedSchedule, "SCHEDULE")
    assert (schedule.version, schedule.items, schedule.corrected_ids, schedule.total_cents) == (1, [], ROUND1, 0)

    await drive(loop, STEPS[4:])
    assert merged(loop.stops)[-5:] == STOPS[-5:]
    assert loop.runs(FIN09) == ["RUN-FIN-09-1", "RUN-FIN-09-2", "RUN-FIN-09-3"]
    assert loop.runs("FIN-04") == ["RUN-FIN-04-1", "RUN-FIN-04-2"]
    assert loop.latest(UncorrectedSchedule, "SCHEDULE").corrected_ids == [*ROUND1, *ROUND2]


# 进程中途被杀 (验收 8): 第 1 条流程中每类等待交出决定后, 后台推进到下一个节点时取消; (该步, 被杀的节点)
KILLS = [
    ("FIN-03 第 1 轮", "communicate"),  # 节点内等复核 (分诊)
    ("FIN-08 第 1 轮", "await_correction"),  # 人工程序的登记
    ("D-CN1", "verify_correction"),  # 等更正通知
    ("FIN-09 第 1 轮", "p3_reassess"),  # 节点内等复核 (验证)
    ("FIN-15 认定", "misstatement_triage"),  # 完成阶段复核 (回流第 2 轮)
    ("FIN-09 第 2 轮", "final_review"),  # 第 2 轮: 桩 FIN-15 不等人, 被杀在它执行之前
]


async def drive_with(loop: Loop, at: str, arm: Callable[[Loop], None]) -> Loop:
    """同 drive, 在名为 at 的那步之前调 arm (布置中断), 该步之后重启 (restart)."""
    for name, step in STEPS:
        if name == at:
            arm(loop)
        await step(loop)
        if name == at and not loop.crashed:
            await loop.restart()
    return loop


@pytest.mark.parametrize(("at", "node"), KILLS, ids=[node for _, node in KILLS])
async def test_restart_after_kill_matches_baseline(
    tmp_path: Path, baseline: dict[str, Any], at: str, node: str
) -> None:
    """验收 8: 交出决定后, 后台进 node 时取消 (node 没执行, 检查点停在上一步之后); 新 runner recover 从检查点继续
    (不在等待, 也没有缺的决定: ainvoke(None)), 停点与不中断时相同, Store 的结果, 留痕的决定与迁移, 各程序的模型调用
    条数都与 baseline 相同 (没有重跑已完成的节点)."""
    loop = await drive_with(await start(tmp_path), at, lambda loop: loop.kill_at.add(node))

    assert loop.killed == [node] and loop.restarted_at == [((node,), False)]
    assert merged(loop.stops) == STOPS
    assert await path(loop) == PATH
    assert outcome(loop) == baseline


# 决定写进 Store 后中断 (验收 8): 该步的第一个决定
CRASHES = ["FIN-03 第 1 轮", "FIN-08 第 1 轮", "D-CN1", "FIN-09 第 1 轮", "FIN-15 认定"]


@pytest.mark.parametrize(
    "at", CRASHES, ids=["fin03_review", "fin08_manual", "notice", "fin09_review", "fin15_misstatement"]
)
async def test_decision_in_store_only_is_caught_up(tmp_path: Path, baseline: dict[str, Any], at: str) -> None:
    """验收 8: 该步第一个决定的业务事务提交后即中断 (decision 留痕没写, 没交给后台, 检查点仍停在等待; Loop.submit 查
    resume_required 报的正是它). 新 runner recover 补做: 补写它的留痕 (只一条), 以 Command(resume=决定 ID) 交给等待的
    节点; 之后照常提交其余决定. 停点, 经过的节点与结果都与 baseline 相同."""

    def arm(loop: Loop) -> None:
        loop.crash_next = True

    loop = await drive_with(await start(tmp_path), at, arm)

    [(waiting, interrupted)] = loop.restarted_at
    assert len(loop.crashed) == 1 and interrupted and waiting[0] in P3_NODES
    assert merged(loop.stops) == STOPS
    assert await path(loop) == PATH
    assert outcome(loop) == baseline


REJECT = fin_decision(GateVerdict.reject, id="D-FIN-R", target_version=2)


async def rejected_loop(tmp_path: Path, kill: str | None = None) -> Loop:
    """两轮走完后 FIN-01 退回 (REJECT); kill 给了时, 退回后后台进该节点时取消, 再重启."""
    loop = await drive(await start(tmp_path))
    if kill is not None:
        loop.kill_at.add(kill)
    await loop.submit(REJECT)
    if kill is not None:
        assert loop.killed == [kill]
        await loop.restart()
    return loop


@pytest.fixture(scope="module")
def rejected(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """不中断地走完两轮再退回的结果 (outcome)."""

    async def run() -> dict[str, Any]:
        return outcome(await rejected_loop(tmp_path_factory.mktemp("rejected")))

    return asyncio.run(run())


@pytest.mark.parametrize("kill", [None, "summarize"], ids=["direct", "killed"])
async def test_fin01_reject_reruns_summarize(tmp_path: Path, rejected: dict[str, Any], kill: str | None) -> None:
    """验收 9: FIN-01 退回 (绑定 SCHEDULE v2) → summarize: RUN-FIN-04-3 出 SCHEDULE v3, 内容同 v2; 桩 FIN-15 RUN-3
    不出新判断; 不重跑 FIN-03, FIN-08, FIN-09; 回到 FIN-01 闸门, 检查点的汇总表为 v3. killed: 退回后后台进 summarize
    时取消, 重启后结果与不中断时 (rejected) 相同 (验收 8 的闸门一类)."""
    loop = await rejected_loop(tmp_path, kill)

    assert merged(loop.stops) == STOPS and loop.stops[-2:] == [("gate_fin", "gate")] * 2  # 退回后再停在闸门
    assert loop.restarted_at == ([] if kill is None else [(("summarize",), False)])
    assert await path(loop) == [*PATH, "summarize", "final_review", "completion", "gate_fin"]
    assert {pid: len(loop.runs(pid)) for pid in PACK} == {
        "FIN-03": 2,
        "FIN-08": 2,
        FIN09: 2,
        "FIN-04": 3,
        "FIN-15": 3,
        "FIN-01": 0,
    }
    assert loop.fin15_runs == [1, 2, 3]
    v2, v3 = (r for r in loop.store.rows if isinstance(r, UncorrectedSchedule) and r.version in (2, 3))
    content = {"id", "version"}
    assert v3.version == 3 and v3.model_dump(exclude=content) == v2.model_dump(exclude=content)
    assert not [j for j in loop.store.rows if isinstance(j, Judgment) and j.id.startswith("J-FIN-15-3")]
    snapshot = await loop.runner.app.aget_state(CONFIG)
    assert snapshot.values["uncorrected_schedule"].version == 3
    assert outcome(loop) == rejected
