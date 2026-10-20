"""R16 await_events 读回决定: 图停在节点内等复核, 决定写入 Store 后以决定 ID 恢复, 读回六类对象的最新版本并入状态."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from backend.contracts.models import (
    Assertion,
    AuditException,
    Decision,
    Direction,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    Misstatement,
    MisstatementDraft,
    MisstatementStatus,
    MisstatementType,
    Procedure,
    ProcedureStatus,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    TaskStatus,
)
from backend.contracts.state import EngagementState
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    make_graph_nodes,
    make_runner,
    numbat_p2_agents,
    wait_idle,
)
from tests.test_decisions import acc01_decision, pln_decision
from tests.test_fieldwork import p2_agents

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)


async def waiting_for_review() -> tuple[GraphRunner, FakeOutputStore]:
    """Numbat 跑过两道闸门, P2 派完可派发的程序后停在 await_events: 只剩 REV-03 的 3 个待决任务
    (合成数据集第 7 节第 8 行)."""
    runner, store = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        agents=lambda deps: {**p2_agents(deps), **numbat_p2_agents(deps)},
    )
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert (await runner.app.aget_state(CONFIG)).next == ("await_events",)
    return runner, store


async def confirm(store: FakeOutputStore, n: int, *, completes: bool = False) -> ReviewRecord:
    """模拟审计师确认 REV-03 对第 n 份合同的判断, DecisionSubmit 写入 Store 的对象 (由 R18 实现): 决定 D-n,
    判断 v2 confirmed, 任务 v2 decided, 同一事务; completes 为真时 (最后一个任务) 另写 REV-03 completed 新版本."""
    task = await store.get(EID, ReviewTask, f"T-J-REV-03-1-CON-00{n}-v1")
    assert task is not None
    judgment = await store.get(EID, Judgment, task.target_id)
    assert judgment is not None
    record = ReviewRecord(
        id=f"D-{n}",
        task_id=task.id,
        target_version=task.target_version,
        decision=Decision.confirm,
        reviewed_by="U-1",
        decided_at=NOW,
    )
    objs: list[Any] = [
        record,
        judgment.model_copy(update={"version": 2, "review_status": ReviewStatus.confirmed}),
        task.model_copy(update={"version": 2, "status": TaskStatus.decided}),
    ]
    if completes:
        rev03 = await store.get(EID, Procedure, "REV-03")
        assert rev03 is not None
        objs.append(rev03.model_copy(update={"version": rev03.version + 1, "status": ProcedureStatus.completed}))
    async with store.transaction(EID):
        await store.put(EID, objs, actor="U-1")
    return record


def by_id(objs: list[Any]) -> dict[str, Any]:
    return {o.id: o for o in objs}


async def test_overview_while_waiting_for_review() -> None:
    """验收 1: 总览显示等复核, 不在闸门, 3 个待决任务, REV-03 等人处理且有 3 个任务; 没有闸门材料."""
    runner, _ = await waiting_for_review()
    query = GraphStateQuery(runner.app, runner.deps, runner)

    overview = await query.overview(EID)

    assert (overview.awaiting_review, overview.current_gate, overview.pending_tasks) == (True, None, 3)
    rev03 = next(p for p in overview.procedures if p.id == "REV-03")
    assert (rev03.status, rev03.pending_tasks) == (ProcedureStatus.awaiting_human, 3)
    assert await query.gate(EID) is None


async def test_resume_reads_back_decision_and_waits_again() -> None:
    """验收 2: 确认第 1 份合同后以 D-1 恢复: 状态含决定, 判断与任务为 v2; 回到 await_events, 剩 2 个待决任务."""
    runner, store = await waiting_for_review()
    record = await confirm(store, 1)

    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    values = snapshot.values
    assert snapshot.next == ("await_events",)
    assert record in values["review_records"]
    assert by_id(values["judgments"])["J-REV-03-1-CON-001"].version == 2
    assert by_id(values["review_tasks"])["T-J-REV-03-1-CON-001-v1"].status is TaskStatus.decided
    assert len([t for t in values["review_tasks"] if t.status is TaskStatus.pending]) == 2


async def test_last_decision_completes_program_and_leaves_review() -> None:
    """验收 3: 第 3 个决定连同 REV-03 completed 新版本写入后恢复: 状态中 REV-03 completed, 图离开 await_events;
    REV-02 随之可派发, 跑完后 P2 全部完成, 停在 FIN-01 闸门前."""
    runner, store = await waiting_for_review()
    for n in (1, 2):
        await confirm(store, n)
        await runner.app.ainvoke(Command(resume=f"D-{n}"), CONFIG)
    await confirm(store, 3, completes=True)

    await runner.app.ainvoke(Command(resume="D-3"), CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    pack = by_id(snapshot.values["procedure_pack"])
    assert pack["REV-03"].status is ProcedureStatus.completed
    assert pack["REV-03"] == await store.get(EID, Procedure, "REV-03")
    assert pack["REV-02"].status is ProcedureStatus.completed
    assert snapshot.next == ("gate_fin",)


async def test_unknown_record_raises_with_cause_and_recovery() -> None:
    """验收 4: 恢复值在 Store 中没有对应决定: 抛 LookupError, 写明是哪个 ID, 原因与恢复办法; 状态不变.
    这个值已交给节点, 再恢复仍报同样的错; 按异常里的办法以 fan_in 身份重写检查点后, 图重新等复核,
    以已写入的 D-1 恢复正常读回."""
    runner, store = await waiting_for_review()
    before = (await runner.app.aget_state(CONFIG)).values

    with pytest.raises(LookupError) as e:
        await runner.app.ainvoke(Command(resume="D-404"), CONFIG)

    message = str(e.value)
    assert "D-404 不在 Store 中" in message and "DecisionSubmit" in message
    assert 'as_node="fan_in"' in message and "ainvoke(None, config)" in message  # 恢复办法的两步都写明
    assert (await runner.app.aget_state(CONFIG)).values == before
    record = await confirm(store, 1)
    with pytest.raises(LookupError, match="D-404"):
        await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    await runner.app.aupdate_state(CONFIG, {}, as_node="fan_in")
    await runner.app.ainvoke(None, CONFIG)
    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    assert record in snapshot.values["review_records"]
    assert len([t for t in snapshot.values["review_tasks"] if t.status is TaskStatus.pending]) == 2
    assert snapshot.next == ("await_events",)


async def test_rerun_with_same_record_leaves_state_unchanged() -> None:
    """验收 5: 节点以同一决定 ID 重跑 (如读回后, 写检查点前中断): 读回的仍是 Store 最新版本, 并入已含它们的状态,
    状态不变, 不抛 VersionConflict."""
    runner, store = await waiting_for_review()
    await confirm(store, 1)
    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)
    merged: EngagementState = (await runner.app.aget_state(CONFIG)).values  # type: ignore[assignment]
    nodes = make_graph_nodes(runner.deps)
    graph = StateGraph(EngagementState)
    graph.add_node("await_events", nodes.await_events)
    graph.add_edge(START, "await_events")
    graph.add_edge("await_events", END)
    app = graph.compile(checkpointer=InMemorySaver(serde=checkpoint_serde()))
    config = GraphRunner.config("replay")
    await app.ainvoke(merged, config)

    result = await app.ainvoke(Command(resume="D-1"), config)

    assert result == merged


async def test_new_exception_version_and_misstatement_read_back() -> None:
    """决定写出的例外新版本 (归因) 与新错报都读回状态, 与 Store 最新版本逐字段相等 (六类对象整体读回)."""
    runner, store = await waiting_for_review()
    task = await store.get(EID, ReviewTask, "T-J-REV-03-1-CON-001-v1")
    judgment = await store.get(EID, Judgment, "J-REV-03-1-CON-001")
    exception = await store.get(EID, AuditException, "X-CSH-05-1-B01-1")
    assert task is not None and judgment is not None and exception is not None
    draft = MisstatementDraft(
        type=MisstatementType.factual,
        account="Revenue",
        assertion=Assertion.Cutoff,
        amount_cents=14200000,
        direction=Direction.overstated,
    )
    record = ReviewRecord(
        id="D-1",
        task_id=task.id,
        target_version=1,
        decision=Decision.misstatement,
        reason="控制权在期后转移",
        misstatement_draft=draft,
        reviewed_by="U-1",
        decided_at=NOW,
    )
    misstatement = Misstatement(  # 功能 misstatement 关闭时的旧口径 (FIN-04 按状态累计, 需求 R166)
        id="M-D-1",
        judgment_id=judgment.id,
        exception_id=None,
        review_record_id=record.id,
        status=MisstatementStatus.uncorrected,
        **draft.model_dump(),
    )
    attributed = exception.model_copy(
        update={"version": 2, "disposition": ExceptionDisposition.attributed, "attributed_by": judgment.id}
    )
    async with store.transaction(EID):
        await store.put(
            EID,
            [
                record,
                judgment.model_copy(update={"version": 2, "review_status": ReviewStatus.confirmed}),
                task.model_copy(update={"version": 2, "status": TaskStatus.decided}),
                misstatement,
                attributed,
            ],
            actor="U-1",
        )

    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    values = (await runner.app.aget_state(CONFIG)).values
    assert (
        by_id(values["exceptions"])[attributed.id] == attributed == await store.get(EID, AuditException, attributed.id)
    )
    assert by_id(values["misstatements"])["M-D-1"] == misstatement == await store.get(EID, Misstatement, "M-D-1")
