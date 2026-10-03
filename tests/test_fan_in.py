"""R13 fan_in 入库与并入: 本轮暂存结果在一个事务内写入 Store, 以 Store 返回的对象并入状态, 重算本轮执行过的程序状态.

R14 合并前校验: Agent 片段在 fieldwork 取得时按派发时的状态校验, 不合规的暂存为 A1 写的失败项; fan_in 再按已并入
本轮前面各项的状态查 ID, 版本与外键, 不过的项改为失败项. 两种情况都与同轮合规项一起提交.
"""

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from backend.contracts.errors import RuleError
from backend.contracts.models import (
    AgentId,
    AuditException,
    Confirmation,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewTask,
    RunStatus,
    TaskKind,
    TaskStatus,
    Versioned,
)
from backend.contracts.state import EngagementState, StagedResult, StatePatch
from backend.orchestration import nodes as nodes_module
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import route_schedule
from backend.orchestration.merge import check_refs
from backend.orchestration.nodes import FieldworkInput, GraphNodes, apply_patch
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    NUMBAT_REPLIES,
    DocumentWrite,
    FakeEngagementStore,
    FakeOutputStore,
    ScriptA3,
    ScriptA6,
    ScriptAgent,
    assert_pack_synced,
    finished,
    make_run,
    make_runner,
    numbat_p2_agents,
    preset_rerun,
    wait_idle,
)
from tests.test_decisions import acc01_decision, pln_decision
from tests.test_fieldwork import assembled, dispatch, p2_agents

EID = ENGAGEMENT.id
COMPLETED, AWAITING = ProcedureStatus.completed, ProcedureStatus.awaiting_human
LAYER_ZERO = ["CSH-04", "CSH-05", "REV-03", "REV-08"]


async def after_layer_zero() -> tuple[GraphNodes, FakeOutputStore, EngagementState]:
    """装配后派发第 0 层两个实例 (Numbat 脚本 Agent), 状态的 staged_results 为它们的 4 个暂存项, fan_in 尚未执行."""
    nodes, store, state = await assembled()
    nodes.agents.update(numbat_p2_agents(nodes.deps))
    return nodes, store, await stage_layer_zero(nodes, state)


async def stage_layer_zero(nodes: GraphNodes, state: EngagementState) -> EngagementState:
    cash = await nodes.fieldwork(dispatch(state, "CSH-04", "CSH-05", instance="fw-0-0"))
    revenue = await nodes.fieldwork(dispatch(state, "REV-03", "REV-08", instance="fw-0-1"))
    state["staged_results"] = [*cash["staged_results"], *revenue["staged_results"]]
    return state


def kinds(commit: list[Any]) -> Counter[str]:
    return Counter(type(o).__name__ for o in commit)


def by_id(objs: list[Any]) -> dict[str, Any]:
    return {o.id: o for o in objs}


async def test_fan_in_writes_round_in_one_commit() -> None:
    """验收 1: 4 个暂存项一次提交; 回函在该事务内写文件状态; 状态各字段等于 Store 返回的对象; 暂存区清空."""
    nodes, store, state = await after_layer_zero()
    commits = len(store.commits)

    patch = await nodes.fan_in(state)

    [commit] = store.commits[commits:]
    assert kinds(commit) == {
        "ProcedureRun": 4,
        "AuditException": 2,
        "Judgment": 3,
        "ReviewTask": 3,
        "Confirmation": 2,
        "Procedure": 4,
        "DocumentWrite": 2,
    }
    assert [w for w in commit if isinstance(w, DocumentWrite)] == [
        DocumentWrite(d.id, "received") for d in NUMBAT_REPLIES
    ]
    for field, model in [
        ("procedure_runs", ProcedureRun),
        ("exceptions", AuditException),
        ("judgments", Judgment),
        ("review_tasks", ReviewTask),
        ("confirmations", Confirmation),
    ]:
        assert patch[field], field
        for obj in patch[field]:
            assert await store.get(EID, model, obj.id, obj.version) == obj
    assert patch["documents"] == NUMBAT_REPLIES
    assert patch["staged_results"] is None
    assert apply_patch(state, patch)["staged_results"] == []


async def test_fan_in_recomputes_executed_programs() -> None:
    """验收 2: 有待决任务的 REV-03 为 awaiting_human, 其余三条 completed; 未执行的不变; 程序包与 Store 一致."""
    nodes, store, state = await after_layer_zero()
    before = by_id(state["procedure_pack"])

    patch = await nodes.fan_in(state)

    pack = by_id(patch["procedure_pack"])
    assert {pid: (pack[pid].version, pack[pid].status) for pid in LAYER_ZERO} == {
        "CSH-04": (2, COMPLETED),
        "CSH-05": (2, COMPLETED),
        "REV-03": (2, AWAITING),
        "REV-08": (2, COMPLETED),
    }
    assert all(pack[pid] is before[pid] for pid in before if pid not in LAYER_ZERO)
    await assert_pack_synced(patch["procedure_pack"], store)


async def test_fan_in_clears_rerun_subjects() -> None:
    """重跑只重判的对象 (rerun_subject_ids) 只对紧接着的一次执行有效: 重算出的新版本清空."""
    nodes, store, state = await assembled()
    nodes.agents.update(numbat_p2_agents(nodes.deps))
    rev03 = by_id(state["procedure_pack"])["REV-03"]
    rerun = rev03.model_copy(
        update={"version": 2, "status": ProcedureStatus.executing, "rerun_subject_ids": ["CON-003"]}
    )
    await store.put(EID, [rerun], actor="A1")
    state["procedure_pack"] = [rerun if p.id == "REV-03" else p for p in state["procedure_pack"]]
    state = await stage_layer_zero(nodes, state)

    patch = await nodes.fan_in(state)

    rev03 = by_id(patch["procedure_pack"])["REV-03"]
    assert (rev03.version, rev03.status, rev03.rerun_subject_ids) == (3, AWAITING, [])


async def test_pending_task_from_earlier_run_keeps_program_awaiting() -> None:
    """状态里已有该程序上一次执行留下的待决任务, 本轮执行没有新任务: 仍为 awaiting_human."""
    nodes, _, state = await after_layer_zero()
    state = apply_patch(state, await nodes.fan_in(state))
    rev03 = by_id(state["procedure_pack"])["REV-03"]
    state["staged_results"] = [
        StagedResult(
            procedure_id="REV-03",
            run_id="RUN-REV-03-2",
            writer="A6",  # type: ignore[typeddict-item]
            patch={"procedure_runs": [finished(make_run("REV-03", 2))]},
        )
    ]

    patch = await nodes.fan_in(state)

    assert by_id(patch["procedure_pack"])["REV-03"].version == rev03.version + 1
    assert by_id(patch["procedure_pack"])["REV-03"].status is AWAITING


async def test_new_version_of_earlier_exception_goes_in_same_commit() -> None:
    """验收 3: 暂存项含前置程序例外的新版本 (X-REV-08-1-R01-1 v2): 与其他对象同一次提交, 并入后状态为 v2."""
    nodes, store, state = await after_layer_zero()
    state = apply_patch(state, await nodes.fan_in(state))
    exception = by_id(state["exceptions"])["X-REV-08-1-R01-1"]
    attributed = exception.model_copy(
        update={
            "version": 2,
            "disposition": ExceptionDisposition.attributed,
            "attributed_by": "J-REV-02-1-SI-2026-0612",
        }
    )
    run = finished(make_run("REV-02"))
    state["staged_results"] = [
        StagedResult(
            procedure_id="REV-02",
            run_id=run.id,
            writer="A6",  # type: ignore[typeddict-item]
            patch={"procedure_runs": [run], "exceptions": [attributed]},
        )
    ]
    commits = len(store.commits)

    patch = await nodes.fan_in(state)

    [commit] = store.commits[commits:]
    assert run in commit and attributed in commit
    assert [(type(o).__name__, o.id) for o in commit if isinstance(o, Procedure)] == [("Procedure", "REV-02")]
    assert by_id(apply_patch(state, patch)["exceptions"])[exception.id] == attributed


async def test_failed_item_written_with_others() -> None:
    """验收 4: REV-08 执行失败的暂存项 (A1 写的失败执行与 failed 任务) 与其他项同一次提交; REV-08 为 awaiting_human."""
    nodes, store, state = await assembled()
    nodes.agents.update(numbat_p2_agents(nodes.deps))

    def broken(ctx: Any) -> StatePatch:
        raise RuleError("missing column source_id")

    nodes.agents["REV-08"] = ScriptA6(nodes.deps, broken)
    state = await stage_layer_zero(nodes, state)
    commits = len(store.commits)

    patch = await nodes.fan_in(state)

    [commit] = store.commits[commits:]
    run = await store.get(EID, ProcedureRun, "RUN-REV-08-1")
    task = await store.get(EID, ReviewTask, "T-RUN-REV-08-1-v1")
    assert run is not None and task is not None
    assert run.status is RunStatus.failed and run in commit
    assert (task.kind, task.status) == (TaskKind.failed, TaskStatus.pending) and task in commit
    assert kinds(commit)["ProcedureRun"] == 4
    assert by_id(patch["procedure_pack"])["REV-08"].status is AWAITING


async def test_fan_in_replay_writes_nothing_new() -> None:
    """验收 5: 上次已全部写入 Store, 检查点没跟上, 再跑: Store 无新增, 返回与首次相同."""
    nodes, store, state = await after_layer_zero()
    first = await nodes.fan_in(state)
    rows = list(store.rows)
    registered = list(nodes.deps.financial_store.document_refs)  # type: ignore[attr-defined]

    second = await nodes.fan_in(state)

    assert store.rows == rows
    assert nodes.deps.financial_store.document_refs == registered  # type: ignore[attr-defined]
    assert second == first


async def test_program_versions_taken_from_store() -> None:
    """Store 已有同号程序版本而内容不同 (Store 与检查点不一致): 状态取 Store 返回的那一版, 程序包与 Store 一致."""
    nodes, store, state = await after_layer_zero()
    rev08 = by_id(state["procedure_pack"])["REV-08"]
    ahead = rev08.model_copy(update={"version": 2, "status": AWAITING})
    await store.put(EID, [ahead], actor="A1")

    patch = await nodes.fan_in(state)

    assert by_id(patch["procedure_pack"])["REV-08"] == ahead
    await assert_pack_synced(patch["procedure_pack"], store)


async def test_fan_in_without_staged_results_only_clears() -> None:
    """验收 6: 暂存区为空 (await_events 之后回到 fan_in): 只清空, 不提交."""
    nodes, store, state = await assembled()
    commits = list(store.commits)

    assert await nodes.fan_in(state) == {"staged_results": None}
    assert store.commits == commits


async def test_graph_merges_layer_zero_and_waits_for_review() -> None:
    """图走过两道闸门: 第 0 层并入后 REV-03 有 3 条待决任务; 派完可派发的程序后停在 await_events 等复核;
    状态与 Store 一致."""
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

    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert runner.run_error(EID) is None
    assert snapshot.next == ("await_events",)
    values = snapshot.values
    assert values["staged_results"] == []
    pack = by_id(values["procedure_pack"])
    assert {pid: pack[pid].status for pid in LAYER_ZERO} == {
        "CSH-04": COMPLETED,
        "CSH-05": COMPLETED,
        "REV-03": AWAITING,
        "REV-08": COMPLETED,
    }
    assert [t.id for t in values["review_tasks"] if t.status is TaskStatus.pending] == [
        f"T-J-REV-03-1-CON-00{n}-v1" for n in (1, 2, 3)
    ]
    await assert_pack_synced(values["procedure_pack"], store)
    stored: list[Versioned] = [r for r in store.rows if isinstance(r, ProcedureRun | Judgment | Confirmation)]
    assert len(stored) == len(values["procedure_runs"]) + len(values["judgments"]) + len(values["confirmations"])


def scorecard_from_a6(patch: StatePatch) -> StatePatch:
    """A6 越权写承接评分卡 (A2 的字段)."""
    return {**patch, "acceptance_scorecard": None}


def judgment_of_unknown_program(patch: StatePatch) -> StatePatch:
    """判断与任务的 procedure_id 写成程序包里没有的 REV-99."""
    return {
        **patch,
        "judgments": [j.model_copy(update={"procedure_id": "REV-99"}) for j in patch["judgments"]],
        "review_tasks": [t.model_copy(update={"procedure_id": "REV-99"}) for t in patch["review_tasks"]],
    }


@pytest.mark.parametrize(
    ("procedure_id", "corrupt", "detail", "discarded"),
    [
        ("REV-08", scorecard_from_a6, "acceptance_scorecard", (AuditException, "X-REV-08-1-R01-1")),
        ("REV-03", judgment_of_unknown_program, "REV-99", (Judgment, "J-REV-03-1-CON-001")),
    ],
)
async def test_rejected_patch_written_as_failure_with_others(
    procedure_id: str,
    corrupt: Callable[[StatePatch], StatePatch],
    detail: str,
    discarded: tuple[type[Versioned], str],
) -> None:
    """R14 验收 1-3: Agent 片段不合规 (越权字段, 引用程序包里没有的程序): 片段不入库, 只写 A1 的失败执行与 failed 任务,
    与同轮合规项同一次提交; 该程序重算为 awaiting_human."""
    nodes, store, state = await assembled()
    nodes.agents.update(numbat_p2_agents(nodes.deps))
    agent = cast(ScriptAgent, nodes.agents[procedure_id])
    script = agent.script
    agent.script = lambda ctx: corrupt(script(ctx))
    state = await stage_layer_zero(nodes, state)
    commits = len(store.commits)

    patch = await nodes.fan_in(state)

    [commit] = store.commits[commits:]
    run_id = f"RUN-{procedure_id}-1"
    run = await store.get(EID, ProcedureRun, run_id)
    task = await store.get(EID, ReviewTask, f"T-{run_id}-v1")
    assert run is not None and run in commit
    assert run.status is RunStatus.failed and run.failure_reason is not None
    assert run.failure_reason.startswith("merge_rejected") and detail in run.failure_reason
    assert task is not None and task in commit and (task.kind, task.status) == (TaskKind.failed, TaskStatus.pending)
    model, discarded_id = discarded
    assert await store.get(EID, model, discarded_id) is None
    assert {r.procedure_id for r in commit if isinstance(r, ProcedureRun)} == set(LAYER_ZERO)  # 合规项照常入库
    assert by_id(patch["procedure_pack"])[procedure_id].status is AWAITING
    await assert_pack_synced(patch["procedure_pack"], store)


XID = "X-CSH-05-1-B01-1"  # 第 0 层 CSH-05 的例外, 下面几个测试里由两条重跑的程序同时改写


async def rerun_round() -> tuple[GraphNodes, FakeOutputStore, EngagementState]:
    """第 0 层 REV-03, REV-08 都执行失败, 审计师对两个 failed 任务都选 rerun (决定写出的对象已读回状态):
    下一轮两条程序同在实例 fw-0-1 重跑, 与之并行的还有第 1 层的 DOC-06."""
    nodes, store, state = await assembled()
    nodes.agents.update(numbat_p2_agents(nodes.deps))

    def broken(ctx: Any) -> StatePatch:
        raise RuleError("首轮执行失败")

    for pid in ("REV-03", "REV-08"):
        nodes.agents[pid] = ScriptA6(nodes.deps, broken)
    state = await stage_layer_zero(nodes, state)
    state = apply_patch(state, await nodes.fan_in(state))
    for pid in ("REV-03", "REV-08"):
        task = await store.get(EID, ReviewTask, f"T-RUN-{pid}-1-v1")
        assert task is not None
        record = await preset_rerun(store, task, record_id=f"D-{pid}")
        state = apply_patch(
            state,
            {
                "procedure_pack": await store.latest(EID, Procedure),
                "review_tasks": await store.latest(EID, ReviewTask),
                "review_records": [record],
            },
        )
    state.update(await nodes.schedule(state))  # type: ignore[typeddict-item]
    return nodes, store, state


async def dispatch_round(nodes: GraphNodes, state: EngagementState) -> EngagementState:
    """按 route_schedule 派发本轮全部实例并行执行, 暂存结果并入状态. 每项都已在 fieldwork 中按派发时的状态校验过."""
    sends = route_schedule(nodes)(state)
    assert isinstance(sends, list)
    outputs = await asyncio.gather(*(nodes.fieldwork(send.arg) for send in sends))
    for patch in outputs:
        state = apply_patch(state, patch)
    assert [
        (i["procedure_id"], i["writer"]) for i in state["staged_results"] if i["procedure_id"].startswith("REV")
    ] == [
        ("REV-03", AgentId.A6),
        ("REV-08", AgentId.A6),
    ]
    return state


def writes_exception(exception: AuditException) -> Callable[[Any], StatePatch]:
    return lambda ctx: {"procedure_runs": [finished(ctx.run)], "exceptions": [exception]}


async def test_item_stale_after_earlier_item_becomes_failure() -> None:
    """R14: REV-03 把 CSH-05 的例外改出 v2, 同轮 REV-08 仍交回 v1. 两项各自按派发快照都校验通过, 合起来版本倒退:
    REV-08 改为失败执行与 failed 任务, REV-03 与 DOC-06 照常写入, 都在同一次提交."""
    nodes, store, state = await rerun_round()
    old = by_id(state["exceptions"])[XID]
    changed = old.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    nodes.agents["REV-03"] = ScriptA6(nodes.deps, writes_exception(changed))
    nodes.agents["REV-08"] = ScriptA6(nodes.deps, writes_exception(old))
    state = await dispatch_round(nodes, state)
    commits = len(store.commits)

    patch = await nodes.fan_in(state)

    [commit] = store.commits[commits:]
    assert changed in commit
    good = await store.get(EID, ProcedureRun, "RUN-REV-03-2")
    rejected = await store.get(EID, ProcedureRun, "RUN-REV-08-2")
    assert good is not None and good.status is RunStatus.succeeded and good in commit
    assert rejected is not None and rejected.status is RunStatus.failed and rejected in commit
    assert rejected.failure_reason == f"merge_rejected: exceptions 中 {XID} v1 低于状态中已有的 v2"
    assert await store.get(EID, ProcedureRun, "RUN-DOC-06-1") in commit
    assert await store.get(EID, ReviewTask, "T-RUN-REV-08-2-v1") in commit
    assert by_id(patch["procedure_pack"])["REV-08"].status is AWAITING


async def test_task_on_target_updated_by_earlier_item_becomes_failure() -> None:
    """R14: REV-03 把例外改出 v2, 同轮 REV-08 建了指向 v1 的待决任务: 不能留下永远版本不符的任务, REV-08 改为失败."""
    nodes, store, state = await rerun_round()
    old = by_id(state["exceptions"])[XID]
    changed = old.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    nodes.agents["REV-03"] = ScriptA6(nodes.deps, writes_exception(changed))

    def stale_task(ctx: Any) -> StatePatch:
        task = nodes.deps.queue.make_task(TaskKind.unattributed, old, "REV-08", ctx.run.id, now=ctx.run.started_at)
        return {"procedure_runs": [finished(ctx.run)], "review_tasks": [task]}

    nodes.agents["REV-08"] = ScriptA6(nodes.deps, stale_task)
    state = await dispatch_round(nodes, state)

    patch = await nodes.fan_in(state)

    assert check_refs(patch, apply_patch(state, patch)) == []
    rejected = await store.get(EID, ProcedureRun, "RUN-REV-08-2")
    assert rejected is not None and rejected.status is RunStatus.failed
    assert rejected.failure_reason is not None and "target_version" in rejected.failure_reason
    assert await store.get(EID, ReviewTask, f"T-{XID}-v1") is None


async def test_same_version_with_different_content_becomes_failure() -> None:
    """R14: 同轮两项都把例外写成 v2 而内容不同: 后一项的 v2 写 Store 时会被跳过, 改为失败执行, 不悄悄丢失."""
    nodes, store, state = await rerun_round()
    old = by_id(state["exceptions"])[XID]
    first = old.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    second = old.model_copy(update={"version": 2, "amount_cents": 1})
    nodes.agents["REV-03"] = ScriptA6(nodes.deps, writes_exception(first))
    nodes.agents["REV-08"] = ScriptA6(nodes.deps, writes_exception(second))
    state = await dispatch_round(nodes, state)

    await nodes.fan_in(state)

    assert await store.get(EID, AuditException, XID) == first
    rejected = await store.get(EID, ProcedureRun, "RUN-REV-08-2")
    assert rejected is not None and rejected.failure_reason == (
        f"merge_rejected: exceptions 中 {XID} v2 与状态中已有的同版本内容不同"
    )


def task_on(exception: AuditException, procedure_id: str, nodes: GraphNodes) -> Callable[[Any], StatePatch]:
    """执行成功并建一条指向该例外 (当前版本) 的待决任务."""

    def script(ctx: Any) -> StatePatch:
        task = nodes.deps.queue.make_task(
            TaskKind.unattributed, exception, procedure_id, ctx.run.id, now=ctx.run.started_at
        )
        return {"procedure_runs": [finished(ctx.run)], "review_tasks": [task]}

    return script


async def test_later_update_that_makes_earlier_task_stale_becomes_failure() -> None:
    """R14 第 2 轮: 反方向. REV-03 先建指向例外 v1 的待决任务, 同轮 REV-08 再把例外改为 v2: 后者使已接纳的任务过期,
    改为失败; 任务与例外 v1 保持一致."""
    nodes, store, state = await rerun_round()
    old = by_id(state["exceptions"])[XID]
    changed = old.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    nodes.agents["REV-03"] = ScriptA6(nodes.deps, task_on(old, "REV-03", nodes))
    nodes.agents["REV-08"] = ScriptA6(nodes.deps, writes_exception(changed))
    state = await dispatch_round(nodes, state)

    patch = await nodes.fan_in(state)

    assert check_refs(patch, apply_patch(state, patch)) == []
    assert await store.get(EID, AuditException, XID) == old
    stored_task = await store.get(EID, ReviewTask, f"T-{XID}-v1")
    assert stored_task is not None and stored_task.status is TaskStatus.pending
    rejected = await store.get(EID, ProcedureRun, "RUN-REV-08-2")
    assert rejected is not None and rejected.status is RunStatus.failed
    assert rejected.failure_reason is not None and "随之过期" in rejected.failure_reason


class NoClock(datetime):
    """重放时不得读取当前时间: 调 now() 即失败."""

    @classmethod
    def now(cls, tz: Any = None) -> "NoClock":
        raise AssertionError("重放不得读取当前时间")


@pytest.mark.parametrize("conflict", ["lower_version", "stale_task", "same_version"])
async def test_rejected_item_replays_identically(conflict: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """被拒项已随本轮写入 Store, 检查点没跟上, fan_in 重跑: 返回与首次相同, Store 不新增, 不读当前时间;
    失败执行的结束时间为原执行的."""
    nodes, store, state = await rerun_round()
    old = by_id(state["exceptions"])[XID]
    first = old.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    nodes.agents["REV-03"] = ScriptA6(nodes.deps, writes_exception(first))
    scripts = {
        "lower_version": writes_exception(old),
        "stale_task": task_on(old, "REV-08", nodes),
        "same_version": writes_exception(old.model_copy(update={"version": 2, "amount_cents": 1})),
    }
    nodes.agents["REV-08"] = ScriptA6(nodes.deps, scripts[conflict])
    state = await dispatch_round(nodes, state)
    first_patch = await nodes.fan_in(state)
    rows = list(store.rows)
    rejected = await store.get(EID, ProcedureRun, "RUN-REV-08-2")
    staged = next(i for i in state["staged_results"] if i["procedure_id"] == "REV-08")
    assert rejected is not None and rejected.status is RunStatus.failed
    assert rejected.ended_at == staged["patch"]["procedure_runs"][0].ended_at
    monkeypatch.setattr(nodes_module, "datetime", NoClock)

    second_patch = await nodes.fan_in(state)

    assert second_patch == first_patch
    assert store.rows == rows


async def test_graph_replays_fan_in_after_commit_without_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """图中 fan_in 写完 Store 后抛错, 检查点仍停在 fan_in 前: 继续执行时重跑, 结果与首次相同, Store 不新增."""
    nodes, store, state = await rerun_round()
    old = by_id(state["exceptions"])[XID]
    changed = old.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    nodes.agents["REV-03"] = ScriptA6(nodes.deps, writes_exception(changed))
    nodes.agents["REV-08"] = ScriptA6(nodes.deps, writes_exception(old))
    state = await dispatch_round(nodes, state)
    outputs: list[StatePatch] = []

    async def crash_once(input_state: EngagementState) -> StatePatch:
        patch = await nodes.fan_in(input_state)
        outputs.append(patch)
        if len(outputs) == 1:
            raise RuntimeError("Store 已提交, 检查点尚未更新")
        return patch

    graph = StateGraph(EngagementState)
    graph.add_node("fan_in", crash_once)
    graph.add_edge(START, "fan_in")
    graph.add_edge("fan_in", END)
    app = graph.compile(checkpointer=InMemorySaver(serde=checkpoint_serde()))
    config: RunnableConfig = {"configurable": {"thread_id": EID}}
    with pytest.raises(RuntimeError, match="检查点尚未更新"):
        await app.ainvoke(state, config)
    assert (await app.aget_state(config)).next == ("fan_in",)
    rows = list(store.rows)
    monkeypatch.setattr(nodes_module, "datetime", NoClock)

    await app.ainvoke(None, config)

    assert outputs[0] == outputs[1]
    assert store.rows == rows
    assert (await app.aget_state(config)).values == apply_patch(state, outputs[0])


@pytest.mark.parametrize(("slow", "cancel"), [("fw-0-1", False), ("fw-1-0", False), ("fw-0-1", True)])
async def test_staged_order_follows_dispatch_not_completion(slow: str, cancel: bool) -> None:
    """同轮两项写同一函证内容不同的 v2: 暂存顺序按派发顺序 (调度表行序), 与哪个实例先完成无关, 中途取消后恢复也一样;
    先派的 REV-03 (fw-0-1) 胜出, 后派的 DOC-06 (fw-1-0) 改为失败."""
    nodes, store, state = await rerun_round()
    old = state["confirmations"][0]
    first = old.model_copy(update={"version": 2, "confirmed_balance_cents": 100})
    second = old.model_copy(update={"version": 2, "confirmed_balance_cents": 200})
    nodes.agents["REV-03"] = ScriptA6(
        nodes.deps, lambda ctx: {"procedure_runs": [finished(ctx.run)], "confirmations": [first]}
    )
    nodes.agents["REV-08"] = ScriptA6(nodes.deps, lambda ctx: {"procedure_runs": [finished(ctx.run)]})
    nodes.agents["DOC-06"] = ScriptA3(
        nodes.deps, lambda ctx: {"procedure_runs": [finished(ctx.run)], "confirmations": [second]}
    )
    fast_done, release_slow = asyncio.Event(), asyncio.Event()
    if not cancel:
        release_slow.set()
    calls = {"fw-0-1": 0, "fw-1-0": 0}
    finished_order: list[str] = []

    async def fieldwork(payload: FieldworkInput) -> StatePatch:
        instance = payload["instance"]
        calls[instance] += 1
        if instance == slow:
            await fast_done.wait()
            await release_slow.wait()
        output = await nodes.fieldwork(payload)
        finished_order.append(instance)
        if instance != slow:
            fast_done.set()
        return output

    graph = StateGraph(EngagementState)
    graph.add_node("schedule", nodes.schedule)
    graph.add_node("fieldwork", fieldwork)  # type: ignore[arg-type]
    graph.add_node("fan_in", nodes.fan_in)
    graph.add_edge(START, "schedule")
    graph.add_conditional_edges("schedule", route_schedule(nodes), ["fieldwork", "fan_in"])
    graph.add_edge("fieldwork", "fan_in")
    graph.add_edge("fan_in", END)
    app = graph.compile(checkpointer=InMemorySaver(serde=checkpoint_serde()), interrupt_before=["fan_in"])
    config: RunnableConfig = {"configurable": {"thread_id": EID}}
    fast = "fw-1-0" if slow == "fw-0-1" else "fw-0-1"
    if cancel:
        run = asyncio.create_task(app.ainvoke(state, config))
        await asyncio.wait_for(fast_done.wait(), 3)
        for _ in range(1000):  # 等先完成的实例结果存进检查点
            if any(t.result is not None for t in (await app.aget_state(config)).tasks):
                break
            await asyncio.sleep(0.001)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        release_slow.set()
        await app.ainvoke(None, config)
        assert (calls[fast], calls[slow]) == (1, 2)  # 先完成的不重跑
    else:
        await app.ainvoke(state, config)
    snapshot = await app.aget_state(config)
    assert snapshot.next == ("fan_in",)
    assert [i["procedure_id"] for i in snapshot.values["staged_results"]] == ["REV-03", "REV-08", "DOC-06"]
    assert finished_order == [fast, slow]

    await app.ainvoke(None, config)

    assert await store.get(EID, Confirmation, old.id) == first
    loser = await store.get(EID, ProcedureRun, "RUN-DOC-06-1")
    assert loser is not None and loser.failure_reason is not None and "同版本内容不同" in loser.failure_reason


async def test_graph_dispatches_cash_line_before_waiting_for_revenue_review() -> None:
    """R15: 第 0 层并入后 REV-03 等复核, 现金线不停: 第 1 层 DOC-06, 第 2 层 CSH-02 各派一轮并完成;
    收入线的 REV-02 依赖 REV-03, 仍为 planned; 再没有可派发的, 停在 await_events."""
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

    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert runner.run_error(EID) is None
    assert snapshot.next == ("await_events",)
    pack = by_id(snapshot.values["procedure_pack"])
    assert {pid: pack[pid].status for pid in ["DOC-06", "CSH-02", "REV-02"]} == {
        "DOC-06": COMPLETED,
        "CSH-02": COMPLETED,
        "REV-02": ProcedureStatus.planned,
    }
    runs = {r.id: r.instance for r in snapshot.values["procedure_runs"] if r.instance}
    assert runs == {
        "RUN-CSH-04-1": "fw-0-0",
        "RUN-CSH-05-1": "fw-0-0",
        "RUN-REV-03-1": "fw-0-1",
        "RUN-REV-08-1": "fw-0-1",
        "RUN-DOC-06-1": "fw-1-0",
        "RUN-CSH-02-1": "fw-2-0",
    }
    await assert_pack_synced(snapshot.values["procedure_pack"], store)
