"""主图拓扑（A 负责）。流程见 Demo需求文档.md 4.1 节：

p0_intel → gate_acc01 → p1_ingest → p1_analytics → gate_p1_risk → assemble_pack
  → schedule ⇉ fieldwork × N ⇉ fan_in → { schedule | await_events | completion }
completion → gate_fin → report_draft → gate_rpt01 → { report_draft | archive }
"""

from collections.abc import Callable, Hashable
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from backend.contracts.models import GateId, GateVerdict, ProcedureStatus, TaskStatus, Versioned
from backend.contracts.state import EngagementState
from backend.orchestration.nodes import FieldworkInput, GraphNodes
from backend.orchestration.scheduler import p2_procedures, ready_procedures

GATE_NODES: dict[GateId, str] = {
    GateId.ACC_01: "gate_acc01",
    GateId.PLN_01_03: "gate_p1_risk",
    GateId.FIN_01: "gate_fin",
    GateId.RPT_01: "gate_rpt01",
}
GATES = list(GATE_NODES.values())

GateRoute = Literal["approve", "reject", "insufficient_info", "represent"]


def _gate_target(state: EngagementState, gate: GateId) -> tuple[str, int] | None:
    """当前闸门材料的 (id, version)；决定须绑定它，旧版本上的决定视为无效。"""
    obj: Versioned | None
    if gate is GateId.ACC_01:
        obj = state["acceptance_scorecard"]
    elif gate is GateId.PLN_01_03:
        obj = state["materiality_candidates"]
    elif gate is GateId.FIN_01:
        obj = state["uncorrected_schedule"]
    else:
        reports = state["report_versions"]
        obj = max(reports, key=lambda r: r.revision) if reports else None
    return (obj.id, obj.version) if obj is not None else None


def gate_route(gate: GateId) -> Callable[[EngagementState], GateRoute]:
    def route(state: EngagementState) -> GateRoute:
        decisions = [d for d in state["gate_decisions"] if d.gate is gate]
        if not decisions:
            return "represent"
        d = decisions[-1]
        target = _gate_target(state, gate)
        if target is not None and (d.target_id, d.target_version) != target:
            return "represent"
        if d.decision is GateVerdict.approve:
            return "approve"
        if d.decision is GateVerdict.reject:
            return "reject"
        return "insufficient_info" if gate is GateId.ACC_01 else "represent"

    return route


def route_schedule(nodes: GraphNodes) -> Callable[[EngagementState], list[Send] | str]:
    def route(state: EngagementState) -> list[Send] | str:
        ready = set(ready_procedures(state["procedure_pack"], nodes.deps.library))
        sends = [
            Send(
                "fieldwork",
                FieldworkInput(
                    instance=row.instance, procedure_ids=[p for p in row.procedure_ids if p in ready], state=state
                ),
            )
            for row in state["schedule_table"]
            if ready.intersection(row.procedure_ids)
        ]
        return sends or "fan_in"

    return route


def after_fan_in(state: EngagementState) -> Literal["schedule", "await_events", "completion"]:
    p2 = p2_procedures(state["procedure_pack"])
    if any(t.status is TaskStatus.pending for t in state["review_tasks"]):
        return "await_events"
    if any(p.status in (ProcedureStatus.planned, ProcedureStatus.executing) for p in p2):
        return "schedule"
    if all(p.status is ProcedureStatus.completed for p in p2):
        return "completion"
    return "await_events"


def build_graph(nodes: GraphNodes) -> StateGraph[EngagementState]:
    g = StateGraph(EngagementState)
    g.add_node("p0_intel", nodes.p0_intel)
    g.add_node("p1_ingest", nodes.p1_ingest)
    g.add_node("p1_analytics", nodes.p1_analytics)
    g.add_node("assemble_pack", nodes.assemble_pack)
    g.add_node("schedule", nodes.schedule)
    g.add_node("fieldwork", nodes.fieldwork)  # type: ignore[arg-type]
    g.add_node("fan_in", nodes.fan_in)
    g.add_node("await_events", nodes.await_events)
    g.add_node("completion", nodes.completion)
    g.add_node("report_draft", nodes.report_draft)
    g.add_node("archive", nodes.archive)
    g.add_node("declined", nodes.declined)
    for node in GATES:
        g.add_node(node, nodes.gate)

    g.add_edge(START, "p0_intel")
    g.add_edge("p0_intel", "gate_acc01")
    g.add_edge("p1_ingest", "p1_analytics")
    g.add_edge("p1_analytics", "gate_p1_risk")
    g.add_edge("assemble_pack", "schedule")
    g.add_conditional_edges("schedule", route_schedule(nodes), ["fieldwork", "fan_in"])
    g.add_edge("fieldwork", "fan_in")
    g.add_conditional_edges("fan_in", after_fan_in, ["schedule", "await_events", "completion"])
    g.add_edge("await_events", "fan_in")
    g.add_edge("completion", "gate_fin")
    g.add_edge("report_draft", "gate_rpt01")
    g.add_edge("archive", END)
    g.add_edge("declined", END)

    exits: dict[GateId, dict[Hashable, str]] = {
        GateId.ACC_01: {"approve": "p1_ingest", "reject": "declined", "insufficient_info": "p0_intel"},
        GateId.PLN_01_03: {"approve": "assemble_pack", "reject": "p1_analytics"},
        GateId.FIN_01: {"approve": "report_draft", "reject": "completion"},
        GateId.RPT_01: {"approve": "archive", "reject": "report_draft"},
    }
    for gate, node in GATE_NODES.items():
        g.add_conditional_edges(node, gate_route(gate), {**exits[gate], "represent": node})
    return g


def compile_graph(
    nodes: GraphNodes, checkpointer: BaseCheckpointSaver[str] | None = None
) -> CompiledStateGraph[EngagementState]:
    return build_graph(nodes).compile(checkpointer=checkpointer, interrupt_before=GATES)
