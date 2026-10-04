"""R22 P2 端到端: 经 runner 与 DecisionSubmit, 用 Numbat 脚本 Agent 从项目启动走过 ACC-01, PLN-01/03 与全部复核,
进入完成阶段 (FIN-04) 后停在 FIN-01 闸门前, 结果与合成数据集设计第 7 节一致; P2 中途取消后恢复, 已完成的实例不重跑."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from backend.agents.base import AgentDeps, RunContext
from backend.contracts.models import (
    AuditException,
    Decision,
    GateVerdict,
    Judgment,
    Misstatement,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    TaskStatus,
    UncorrectedSchedule,
)
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    ScriptA6,
    ScriptAgent,
    assert_pack_synced,
    make_runner,
    numbat_all_p2_agents,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)
P2 = ["REV-08", "REV-03", "REV-02", "CSH-04", "CSH-05", "CSH-02", "DOC-06"]
# 合成数据集设计第 7 节的复核决定, 按提交顺序:
# REV-03 三条 confirm, SI-2026-0612 认定错报, SI-2026-0618 confirm, BA-01 confirm
REVIEWS = [
    record("T-J-REV-03-1-CON-001-v1", id="D-REV03-1"),
    record("T-J-REV-03-1-CON-002-v1", id="D-REV03-2"),
    record("T-J-REV-03-1-CON-003-v1", id="D-REV03-3"),
    record(
        "T-J-REV-02-1-SI-2026-0612-v1",
        Decision.misstatement,
        id="D-REV02-0612",
        misstatement_draft=NUMBAT_CUTOFF_DRAFT,
    ),
    record("T-J-REV-02-1-SI-2026-0618-v1", id="D-REV02-0618"),
    record("T-J-CSH-02-1-BA-01-v1", id="D-CSH02"),
]


def numbat(deps: AgentDeps) -> dict[str, Any]:
    return numbat_all_p2_agents(deps)


async def start_numbat(agents: Callable[[AgentDeps], dict[str, Any]] = numbat) -> Started:
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagement_store, agents=agents)
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    return s


async def approve_materiality(s: Started) -> None:
    """PLN-01/03 approve: 收入基准 1%, 执行重要性 75%, 明显微小 5%, 不改风险等级 (第 7 节)."""
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))


async def review_all(s: Started) -> None:
    """按第 7 节逐条提交复核决定; 每次提交前图都停在 await_events, 总览显示等复核."""
    query = GraphStateQuery(s.runner.app, s.runner.deps, s.runner)
    for decision in REVIEWS:
        assert await s.next_nodes() == ("await_events",), decision.task_id
        assert (await query.overview(EID)).awaiting_review
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)


async def assert_numbat_result(s: Started) -> None:
    """停在 FIN-01 闸门前, 结果与第 7 节一致."""
    store = s.output_store
    assert await s.next_nodes() == ("gate_fin",)
    procedures = {p.id: p for p in await store.latest(EID, Procedure)}
    assert {pid: procedures[pid].status for pid in P2} == dict.fromkeys(P2, ProcedureStatus.completed)
    judgments = await store.latest(EID, Judgment)
    assert sorted(j.id for j in judgments) == [
        "J-CSH-02-1-BA-01",
        "J-REV-02-1-SI-2026-0612",
        "J-REV-02-1-SI-2026-0618",
        "J-REV-03-1-CON-001",
        "J-REV-03-1-CON-002",
        "J-REV-03-1-CON-003",
    ]
    assert all((j.version, j.review_status) == (2, ReviewStatus.confirmed) for j in judgments)
    tasks = await store.latest(EID, ReviewTask)
    assert len(tasks) == 6 and all(t.status is TaskStatus.decided for t in tasks)
    assert sorted(r.id for r in await store.latest(EID, ReviewRecord)) == sorted(d.id for d in REVIEWS)
    exceptions = {x.id: x for x in await store.latest(EID, AuditException)}
    assert {xid: exceptions[xid].attributed_by for xid in exceptions if xid != "X-PLN-07-1-A01-1"} == {
        "X-REV-08-1-R01-1": "J-REV-02-1-SI-2026-0612",
        "X-CSH-05-1-B01-1": "J-CSH-02-1-BA-01",
        "X-CSH-02-1-D01-1": "J-CSH-02-1-BA-01",
    }
    [misstatement] = await store.latest(EID, Misstatement)
    assert (misstatement.amount_cents, misstatement.direction) == (14200000, NUMBAT_CUTOFF_DRAFT.direction)
    runs = [r for r in await store.latest(EID, ProcedureRun) if r.procedure_id in P2]
    assert sorted(r.procedure_id for r in runs) == sorted(P2)
    assert all(r.run_no == 1 and r.instance for r in runs)
    overview = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).overview(EID)
    assert (overview.pending_tasks, overview.run_error, overview.engagement.phase) == (0, None, Phase.P3_Completion)
    schedule = await store.get(EID, UncorrectedSchedule, "SCHEDULE")  # 完成阶段 FIN-04 的汇总表 (第 7 节第 14 行)
    assert schedule is not None
    assert (schedule.total_cents, schedule.candidate_opinion) == (14200000, OpinionType.qualified)
    snapshot = await s.runner.app.aget_state(CONFIG)
    await assert_pack_synced(snapshot.values["procedure_pack"], store)


async def test_numbat_p2_flow_reaches_fin01() -> None:
    """验收 1, 2: 启动 → ACC-01 approve → PLN-01/03 approve → 逐条复核 → 停在 FIN-01 闸门前, 结果同第 7 节."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)

    await review_all(s)

    await assert_numbat_result(s)


class BlockOnce(ScriptA6):
    """第一次执行在 execute 里等一个永远不会 set 的事件 (模拟 P2 中途卡住, 由测试取消); 之后照常."""

    async def execute(self, ctx: RunContext) -> None:
        await super().execute(ctx)
        if self.calls == 1:
            await asyncio.Event().wait()


async def until(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(500):
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


async def test_cancel_mid_p2_and_resume_without_rerunning_finished_instance() -> None:
    """验收 3: fw-0-1 的 REV-08 第一次卡住; 等 fw-0-0 的结果写入检查点后取消后台任务, 再继续执行:
    fw-0-0 的 CSH-04, CSH-05 不重跑 (各调用 1 次), 同实例的 REV-03 重跑 (2 次); 最终结果同上."""
    holder: dict[str, ScriptAgent] = {}  # 程序编号 -> 脚本 Agent, 用来数调用次数

    def agents(deps: AgentDeps) -> dict[str, Any]:
        found = {pid: cast(ScriptAgent, agent) for pid, agent in numbat(deps).items()}
        found["REV-08"] = BlockOnce(deps, found["REV-08"].script)
        holder.update(found)
        return dict(found)

    s = await start_numbat(agents)
    await approve_materiality(s)

    async def cash_instance_saved() -> bool:
        snapshot = await s.runner.app.aget_state(CONFIG)
        return any(t.name == "fieldwork" and t.result is not None for t in snapshot.tasks)

    await until(cash_instance_saved)
    task = s.runner.tasks[EID]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not s.runner.is_running(EID)

    s.runner.acquire(EID)
    s.runner.launch(EID, None)
    await wait_idle(s.runner, EID)

    assert {pid: holder[pid].calls for pid in ("CSH-04", "CSH-05", "REV-03", "REV-08")} == {
        "CSH-04": 1,
        "CSH-05": 1,
        "REV-03": 2,
        "REV-08": 2,
    }
    await review_all(s)
    await assert_numbat_result(s)
