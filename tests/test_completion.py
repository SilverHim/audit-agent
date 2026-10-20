"""R23 completion 节点: A9 执行 FIN-04, SCHEDULE, 执行记录, FIN-04 新版本与阶段 P3_Completion 同一事务写入."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from backend.contracts.models import (
    GateId,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RunStatus,
    RunTrigger,
    UncorrectedSchedule,
)
from backend.contracts.state import EngagementState
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.graph import gate_route
from backend.orchestration.nodes import GraphNodes, apply_patch
from backend.orchestration.runner import GraphRunner
from tests.fakes import ENGAGEMENT, EngagementWrite
from tests.test_assemble import after_pln_approve
from tests.test_graph import _decision
from tests.test_qc import misstatement

EID = ENGAGEMENT.id


async def test_completion_writes_schedule_and_phase_in_one_commit() -> None:
    """验收 4: RUN-FIN-04-1 succeeded, 参数快照为重要性与明显微小门槛; SCHEDULE, 执行记录, FIN-04 completed 新版本
    与阶段 P3_Completion 在同一次提交; 状态阶段为 P3_Completion."""
    nodes, store, engagements, state = after_pln_approve()
    state["misstatements"] = [misstatement(1, 14200000)]
    commits = len(store.commits)

    patch = await nodes.completion(state)

    [commit] = store.commits[commits:]
    [run] = [o for o in commit if isinstance(o, ProcedureRun)]
    assert (run.id, run.status, run.params_snapshot) == (
        "RUN-FIN-04-1",
        RunStatus.succeeded,
        {"materiality_cents": 12000000, "clearly_trivial_cents": 600000},
    )
    [schedule] = [o for o in commit if isinstance(o, UncorrectedSchedule)]
    assert (schedule.version, schedule.total_cents) == (1, 14200000)
    [fin04] = [o for o in commit if isinstance(o, Procedure)]
    assert (fin04.id, fin04.status) == ("FIN-04", ProcedureStatus.completed)
    assert EngagementWrite("update", EID, {"phase": Phase.P3_Completion}) in commit
    engagement = await engagements.get(EID)
    assert engagement is not None and engagement.phase is Phase.P3_Completion
    assert patch["phase"] is Phase.P3_Completion
    assert patch["uncorrected_schedule"] == schedule


async def test_completion_again_after_fin01_reject() -> None:
    """验收 5: 已有 SCHEDULE v1 与 RUN-FIN-04-1 时再执行 (FIN-01 驳回后回到 completion):
    RUN-FIN-04-2 (rerun), SCHEDULE v2."""
    nodes, store, _, state = after_pln_approve()
    state["misstatements"] = [misstatement(1, 14200000)]
    state = apply_patch(state, await nodes.completion(state))

    patch = await nodes.completion(state)

    [run] = patch["procedure_runs"]
    assert (run.id, run.trigger) == ("RUN-FIN-04-2", RunTrigger.rerun)
    assert patch["uncorrected_schedule"].version == 2
    assert await store.get(EID, UncorrectedSchedule, "SCHEDULE", 2) == patch["uncorrected_schedule"]


def completion_graph(nodes: GraphNodes) -> Any:
    """只含 completion 与 FIN-01 闸门的小图, 闸门前中断; 驳回回 completion."""
    graph = StateGraph(EngagementState)
    graph.add_node("completion", nodes.completion)
    graph.add_node("gate_fin", nodes.gate)
    graph.add_edge(START, "completion")
    graph.add_edge("completion", "gate_fin")
    graph.add_conditional_edges(
        "gate_fin", gate_route(GateId.FIN_01), {"approve": END, "reject": "completion", "represent": "gate_fin"}
    )
    return graph.compile(checkpointer=InMemorySaver(serde=checkpoint_serde()), interrupt_before=["gate_fin"])


async def test_old_checkpoint_replay_restores_later_runs() -> None:
    """回退到第一次 completion 前的检查点重放 (Store 已有第 1, 2 次执行与 SCHEDULE v2): 取回最新的 SCHEDULE v2,
    并一并取回产出它的第 2 次执行, 程序包中 FIN-04 为 Store 最新版本之前的那一版 (第 1 次执行时写的)."""
    nodes, _, _, before_first = after_pln_approve()
    before_first["misstatements"] = [misstatement(1, 14200000)]
    first = await nodes.completion(before_first)
    second = await nodes.completion(apply_patch(before_first, first))

    replay = await nodes.completion(before_first)

    assert replay["uncorrected_schedule"] == second["uncorrected_schedule"]
    assert [r.id for r in replay["procedure_runs"]] == ["RUN-FIN-04-1", "RUN-FIN-04-2"]


async def test_reject_after_old_checkpoint_replay_produces_next_schedule() -> None:
    """接上: 图在 FIN-01 闸门前, 审计师驳回呈报的 SCHEDULE v2, 回到 completion: 出第 3 次执行与 SCHEDULE v3,
    不撞上已有的第 2 次; FIN-04 新版本接在 Store 最新版本之后."""
    nodes, store, _, before_first = after_pln_approve()
    before_first["misstatements"] = [misstatement(1, 14200000)]
    first = await nodes.completion(before_first)
    await nodes.completion(apply_patch(before_first, first))
    fin04_before = await store.get(EID, Procedure, "FIN-04")
    assert fin04_before is not None
    app = completion_graph(nodes)
    config = GraphRunner.config(EID)
    await app.ainvoke(before_first, config)  # 从第一次 completion 前开始: 重放第 1 次
    assert (await app.aget_state(config)).values["uncorrected_schedule"].version == 2

    rejected = _decision(23, GateId.FIN_01, GateVerdict.reject, target_version=2)
    await app.aupdate_state(config, {"gate_decisions": [rejected]}, as_node="gate_fin")
    await app.ainvoke(None, config)

    final = await app.aget_state(config)
    assert final.next == ("gate_fin",)
    assert final.values["uncorrected_schedule"].version == 3
    assert final.values["procedure_runs"][-1].id == "RUN-FIN-04-3"
    fin04 = await store.get(EID, Procedure, "FIN-04")
    assert fin04 is not None and fin04.version == fin04_before.version + 1
    assert {p.id: p for p in final.values["procedure_pack"]}["FIN-04"] == fin04


async def test_old_checkpoint_replay_blocked_while_later_run_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """FIN-04 第 1 次成功, FIN-01 驳回后第 2 次失败, 节点正在等审计师决定; 此时回退到第一次 completion 前的检查点重放:
    第 2 次的待决任务属于那次等待, 重放恢复不了它, 报错写明待决任务并要求从最新的检查点恢复, 不越过等待呈报旧材料."""
    nodes, store, _, before_first = after_pln_approve()
    before_first["misstatements"] = [misstatement(1, 14200000)]
    first = await nodes.completion(before_first)

    async def broken(ctx: object) -> None:
        raise RuntimeError("FIN-01 驳回后的 FIN-04 重做失败")

    monkeypatch.setattr(nodes.agents["FIN-04"], "outcome", broken)  # 节点经 outcome 调 Agent (需求 R324)
    waiting = completion_graph(nodes)
    await waiting.ainvoke(apply_patch(before_first, first), GraphRunner.config(EID))
    run2 = await store.get(EID, ProcedureRun, "RUN-FIN-04-2")
    assert run2 is not None and run2.status is RunStatus.failed

    with pytest.raises(RuntimeError, match=r"T-RUN-FIN-04-2-v1.*须从最新的检查点恢复"):
        await completion_graph(nodes).ainvoke(before_first, GraphRunner.config(EID))
