"""R12 fieldwork 节点: 一个实例依次执行派给它的程序, 结果只放进 staged_results 不写 Store; 一次派发不超过并行上限."""

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.contracts.errors import RuleError
from backend.contracts.models import (
    AgentId,
    AuditException,
    GateVerdict,
    ProcedureRun,
    ProcedureStatus,
    RunStatus,
    RunTrigger,
    TaskKind,
)
from backend.contracts.state import EngagementState, StagedResult, StatePatch
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import route_schedule
from backend.orchestration.nodes import FieldworkInput, GraphNodes
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    ScriptA3,
    ScriptA5,
    ScriptA6,
    ScriptAgent,
    finished,
    make_runner,
    wait_idle,
)
from tests.test_assemble import P2, after_pln_approve
from tests.test_decisions import acc01_decision, pln_decision

EID = ENGAGEMENT.id
A6_PROGRAMS = ["REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02"]
Script = Callable[[RunContext], StatePatch]


def done(ctx: RunContext) -> StatePatch:
    """执行成功, 只交回执行记录的终态."""
    return {"procedure_runs": [finished(ctx.run)]}


def p2_agents(deps: AgentDeps, script: Script = done) -> dict[str, ProcedureAgent[Any]]:
    """P2 七条程序的脚本 Agent, 身份同真实负责的 Agent: DOC-06 为 A3, CSH-04 为 A5, 其余为 A6."""
    agents: dict[str, ProcedureAgent[Any]] = {pid: ScriptA6(deps, script) for pid in A6_PROGRAMS}
    agents["DOC-06"] = ScriptA3(deps, script)
    agents["CSH-04"] = ScriptA5(deps, script)
    return agents


async def assembled(script: Script = done) -> tuple[GraphNodes, FakeOutputStore, EngagementState]:
    """装配后的项目: 状态与 Store 有固定集与 P2 七条 (planned 第 1 版), P2 程序由脚本 Agent 执行. 并行上限 2."""
    nodes, store, _, state = after_pln_approve()
    state.update(await nodes.assemble_pack(state))  # type: ignore[typeddict-item]
    nodes.agents.update(p2_agents(nodes.deps, script))
    return nodes, store, state


def dispatch(state: EngagementState, *procedure_ids: str, instance: str = "fw-0-1") -> FieldworkInput:
    return FieldworkInput(instance=instance, procedure_ids=list(procedure_ids), state=state)


def fail_rev08(ctx: RunContext) -> StatePatch:
    """REV-08 规则 SQL 出错, 其余程序成功."""
    if ctx.run.procedure_id == "REV-08":
        raise RuleError("missing column source_id")
    return done(ctx)


async def test_fieldwork_runs_programs_in_order_and_stages_results() -> None:
    """验收 1, 2: 依次执行, 执行记录带实例名; 每条程序一个暂存项, 写入者为 A6; Store 不变."""
    seen: list[ProcedureRun] = []

    def record(ctx: RunContext) -> StatePatch:
        seen.append(ctx.run)
        return done(ctx)

    nodes, store, state = await assembled(record)
    rows, commits = list(store.rows), list(store.commits)

    patch = await nodes.fieldwork(dispatch(state, "REV-03", "REV-08"))

    assert [(r.id, r.instance, r.trigger) for r in seen] == [
        ("RUN-REV-03-1", "fw-0-1", RunTrigger.initial),
        ("RUN-REV-08-1", "fw-0-1", RunTrigger.initial),
    ]
    assert seen[1].params_snapshot["window_days"] == 20  # 收入目标的风险等级为 significant
    assert patch == {
        "staged_results": [
            StagedResult(
                procedure_id=r.procedure_id, run_id=r.id, writer=AgentId.A6, patch={"procedure_runs": [finished(r)]}
            )
            for r in seen
        ]
    }
    assert store.rows == rows and store.commits == commits


async def test_agent_error_stages_failed_run_and_task_by_a1() -> None:
    """验收 3: REV-08 抛 RuleError: 暂存项由 A1 写, 只有失败执行与 failed 任务; 同实例的 REV-03 照常暂存."""
    nodes, store, state = await assembled(fail_rev08)
    rows = list(store.rows)

    rev03, rev08 = (await nodes.fieldwork(dispatch(state, "REV-03", "REV-08")))["staged_results"]

    assert (rev03["writer"], rev03["patch"]["procedure_runs"][0].status) == (AgentId.A6, RunStatus.succeeded)
    assert (rev08["procedure_id"], rev08["run_id"], rev08["writer"]) == ("REV-08", "RUN-REV-08-1", AgentId.A1)
    assert set(rev08["patch"]) == {"procedure_runs", "review_tasks"}
    [run] = rev08["patch"]["procedure_runs"]
    [task] = rev08["patch"]["review_tasks"]
    assert (run.id, run.status, run.failure_reason, run.instance) == (
        "RUN-REV-08-1",
        RunStatus.failed,
        "RuleError: missing column source_id",
        "fw-0-1",
    )
    assert (task.id, task.kind, task.target_id, task.subject_id) == ("T-RUN-REV-08-1-v1", TaskKind.failed, run.id, None)
    assert store.rows == rows


async def test_next_run_after_failed_run_in_state_is_rerun() -> None:
    """验收 4: 状态已有 RUN-REV-08-1 时再派发 REV-08: 建 RUN-REV-08-2, trigger 为 rerun."""
    nodes, _, state = await assembled(fail_rev08)
    [failed] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]
    state["procedure_runs"] = failed["patch"]["procedure_runs"]
    nodes.agents.update(p2_agents(nodes.deps))

    [again] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]

    [run] = again["patch"]["procedure_runs"]
    assert (again["run_id"], run.run_no, run.trigger, run.status) == (
        "RUN-REV-08-2",
        2,
        RunTrigger.rerun,
        RunStatus.succeeded,
    )


async def test_replay_takes_terminal_run_from_store_without_calling_agent() -> None:
    """验收 5: 状态没有而 Store 已有 RUN-REV-08-1 终态 (fan_in 写了 Store, 检查点没跟上): 不调 Agent, 产出取 Store."""

    def rev08(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        exception = AuditException(
            id="X-REV-08-1-R01-1",
            procedure_id="REV-08",
            run_id=run.id,
            rule_id="R01",
            severity="high",
            source_ids=["SI-2026-0618"],
            amount_cents=8400000,
            detail={},
            evidence=[],
        )
        return {"procedure_runs": [run], "exceptions": [exception]}

    nodes, store, state = await assembled(rev08)
    [first] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]
    await store.put(EID, [*first["patch"]["procedure_runs"], *first["patch"]["exceptions"]], actor="A6")
    agent = cast(ScriptAgent, nodes.agents["REV-08"])

    [replayed] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]

    assert agent.calls == 1
    assert (replayed["run_id"], replayed["writer"]) == ("RUN-REV-08-1", AgentId.A6)
    assert replayed["patch"]["procedure_runs"] == [await store.get(EID, ProcedureRun, "RUN-REV-08-1")]
    assert replayed["patch"]["exceptions"] == [await store.get(EID, AuditException, "X-REV-08-1-R01-1")]
    assert replayed["patch"]["procedure_runs"] == first["patch"]["procedure_runs"]
    assert replayed["patch"]["exceptions"] == first["patch"]["exceptions"]


async def test_replay_of_failed_run_is_written_by_a1() -> None:
    """Store 已有的是失败执行: 取回执行记录与 failed 任务, 写入者为 A1."""
    nodes, store, state = await assembled(fail_rev08)
    [first] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]
    await store.put(EID, [*first["patch"]["procedure_runs"], *first["patch"]["review_tasks"]], actor="A1")
    agent = cast(ScriptAgent, nodes.agents["REV-08"])

    [replayed] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]

    assert agent.calls == 1
    assert replayed == first


async def test_cancellation_is_not_turned_into_failure() -> None:
    """验收 7: 只拦 Exception; 取消照常抛出, 不转成失败执行."""

    def cancel(ctx: RunContext) -> StatePatch:
        raise asyncio.CancelledError

    nodes, store, state = await assembled(cancel)
    rows = list(store.rows)

    with pytest.raises(asyncio.CancelledError):
        await nodes.fieldwork(dispatch(state, "REV-03", "REV-08"))
    assert store.rows == rows


async def test_graph_runs_layer_zero_and_stops_before_fan_in() -> None:
    """验收 6: 图走过两道闸门, 装配后派发第 0 层两个实例; 停在 fan_in 前时暂存 4 项, 分属 fw-0-0 与 fw-0-1."""
    runner, store = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        agents=p2_agents,
        interrupt_before=["fan_in"],
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
    assert snapshot.next == ("fan_in",)
    staged = snapshot.values["staged_results"]
    assert sorted((s["patch"]["procedure_runs"][0].instance, s["procedure_id"], s["writer"]) for s in staged) == [
        ("fw-0-0", "CSH-04", AgentId.A5),
        ("fw-0-0", "CSH-05", AgentId.A6),
        ("fw-0-1", "REV-03", AgentId.A6),
        ("fw-0-1", "REV-08", AgentId.A6),
    ]
    assert all(isinstance(s["writer"], AgentId) for s in staged)  # 读回检查点后仍是枚举, 不是字符串
    assert not [r for r in store.rows if isinstance(r, ProcedureRun) and r.procedure_id in P2]


async def test_dispatch_is_capped_across_layers() -> None:
    """一次派发的实例数不超过并行上限 (2): 三个实例同时就绪时按调度表顺序派前两个, 第三个下一轮派."""
    nodes, _, state = await assembled()
    state.update(await nodes.schedule(state))  # type: ignore[typeddict-item]
    done_, running = ProcedureStatus.completed, ProcedureStatus.executing
    statuses = {"CSH-04": done_, "CSH-05": running, "DOC-06": running, "REV-03": done_, "REV-08": running}
    state["procedure_pack"] = [
        p.model_copy(update={"status": statuses.get(p.id, p.status)}) for p in state["procedure_pack"]
    ]

    sends = route_schedule(nodes)(state)

    assert isinstance(sends, list)
    assert [(s.arg["instance"], s.arg["procedure_ids"]) for s in sends] == [
        ("fw-0-0", ["CSH-05"]),
        ("fw-0-1", ["REV-08"]),
    ]

    statuses.update({"CSH-05": done_, "REV-08": done_})
    state["procedure_pack"] = [
        p.model_copy(update={"status": statuses.get(p.id, p.status)}) for p in state["procedure_pack"]
    ]
    sends = route_schedule(nodes)(state)

    assert isinstance(sends, list)
    assert [(s.arg["instance"], s.arg["procedure_ids"]) for s in sends] == [
        ("fw-1-0", ["DOC-06"]),
        ("fw-1-1", ["REV-02"]),
    ]
