"""R13 fan_in 入库与并入: 本轮暂存结果在一个事务内写入 Store, 以 Store 返回的对象并入状态, 重算本轮执行过的程序状态."""

from collections import Counter
from typing import Any

from backend.contracts.errors import RuleError
from backend.contracts.models import (
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
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.nodes import GraphNodes, apply_patch
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    NUMBAT_REPLIES,
    DocumentWrite,
    FakeEngagementStore,
    FakeOutputStore,
    ScriptA6,
    assert_pack_synced,
    finished,
    make_run,
    make_runner,
    numbat_p2_agents,
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
    """图走过两道闸门: 第 0 层并入后, REV-03 有 3 条待决任务, 停在 await_events 等复核; 状态与 Store 一致."""
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
