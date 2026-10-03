"""主图拓扑 (A 负责): 有哪些节点, 节点之间怎么连. 流程见 Demo需求文档.md 4.1 节:

p0_intel → gate_acc01 → { p1_ingest | declined | p0_intel (客户画像更新后重跑) }
p1_ingest → p1_analytics → gate_p1_risk → { assemble_pack | p1_analytics }
assemble_pack → schedule ⇉ fieldwork × N ⇉ fan_in → { schedule | await_events | completion }
completion → gate_fin → { report_draft | completion }
report_draft → gate_rpt01 → { archive | report_draft }

读法: → 固定边; { a | b } 条件边, 由路由函数按状态选一个; ⇉ 用 Send 并行派发多个实例.
四道闸门 (gate_*) 前静态中断, 等审计师决定; 每道闸门另有 represent 出口, 回到自己重新呈报.
"""

from collections.abc import Callable, Hashable
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from backend.contracts.models import GateId, GateVerdict, Procedure, ProcedureStatus, TaskStatus, Versioned
from backend.contracts.state import EngagementState
from backend.orchestration.nodes import FieldworkInput, GraphNodes
from backend.orchestration.scheduler import p2_procedures, ready_procedures
from backend.services.procedure_library import ProcedureLibrary

# 闸门 → 图里的节点名. 四个节点共用一个节点函数 nodes.gate, 只是名字不同
GATE_NODES: dict[GateId, str] = {
    GateId.ACC_01: "gate_acc01",
    GateId.PLN_01_03: "gate_p1_risk",
    GateId.FIN_01: "gate_fin",
    GateId.RPT_01: "gate_rpt01",
}
GATES = list(GATE_NODES.values())  # 四个闸门节点名, 编译时设为静态中断点

# 闸门路由函数的返回值, 即闸门的出口名: 通过, 驳回, 重跑 ACC-04, 重新呈报.
# 信息不足一律重呈 (represent); rerun 只给 ACC-01 用: 客户画像更新后回 p0_intel 重跑 ACC-04
GateRoute = Literal["approve", "reject", "rerun", "represent"]

UNFINISHED = (ProcedureStatus.planned, ProcedureStatus.executing)  # 还要派发执行的程序状态


class ScheduleStuck(RuntimeError):
    """P2 调度卡住: 程序状态与复核任务对不上 (如 awaiting_human 却没有待处理任务), 再走会空转或永远暂停."""


def _gate_target(state: EngagementState, gate: GateId) -> tuple[str, int] | None:
    """当前闸门材料的 (id, version); 决定须绑定它, 旧版本上的决定视为无效. 还没有材料时返回 None.

    每道闸门呈报的材料不同: ACC-01 是承接评分卡, PLN-01/03 是重要性候选, FIN-01 是未更正错报汇总表,
    RPT-01 是最新一版报告.
    """
    obj: Versioned | None
    if gate is GateId.ACC_01:
        obj = state["acceptance_scorecard"]
    elif gate is GateId.PLN_01_03:
        obj = state["materiality_candidates"]
    elif gate is GateId.FIN_01:
        obj = state["uncorrected_schedule"]
    else:
        reports = state["report_versions"]
        # key=lambda r: r.revision 表示按 revision 比大小, 取修订轮次最大的一版
        obj = max(reports, key=lambda r: r.revision) if reports else None
    return (obj.id, obj.version) if obj is not None else None


def gate_route(gate: GateId) -> Callable[[EngagementState], GateRoute]:
    """生成某道闸门的路由函数: 闸门节点执行后 (或 aupdate_state 以闸门身份写入后) 按状态选出口.

    LangGraph 的路由函数只接收 state 一个参数, 而四道闸门的判断只差 gate 不同,
    所以外层按 gate 各生成一个, 内层 route 记住外层的 gate (闭包).
    """

    def route(state: EngagementState) -> GateRoute:
        # 按顺序判断, 先命中先返回
        # 1. ACC-01: 评分卡依据的客户画像比最新的旧, 不论最后一条决定是什么都先重跑
        scorecard = state["acceptance_scorecard"]
        if (
            gate is GateId.ACC_01
            and scorecard is not None
            and scorecard.client_profile_version < state["client_profile_version"]
        ):
            return "rerun"
        # 2. 本闸门还没有决定 (gate_decisions 里混有四道闸门的决定, 只看本闸门的)
        decisions = [d for d in state["gate_decisions"] if d.gate is gate]
        if not decisions:
            return "represent"
        # 3. 以最后一条决定为准; 它绑定的材料不是当前版本 (如评分卡已重跑出新版), 视为过期;
        #    材料缺失 (状态与流程不符) 也不放行, 留在闸门重新呈报
        d = decisions[-1]
        target = _gate_target(state, gate)
        if target is None or (d.target_id, d.target_version) != target:
            return "represent"
        # 4. 按决定种类出闸门
        if d.decision is GateVerdict.approve:
            return "approve"
        if d.decision is GateVerdict.reject:
            return "reject"
        return "represent"  # 信息不足

    return route


def _stuck_detail(pack: list[Procedure], unfinished: list[Procedure], library: ProcedureLibrary) -> str:
    """列出每条卡住的程序在等哪些没完成的前置, 如 "REV-02 等 REV-08 (awaiting_human)"."""
    status = {p.id: p.status for p in p2_procedures(pack)}
    parts = []
    for p in unfinished:
        deps = library.load(p.id).depends_on
        waiting = [f"{d} ({status[d]})" for d in deps if d in status and status[d] is not ProcedureStatus.completed]
        parts.append(f"{p.id} 等 {', '.join(waiting)}")
    return "P2 调度卡住, 派不出程序: " + "; ".join(parts)


def route_schedule(nodes: GraphNodes) -> Callable[[EngagementState], list[Send] | str]:
    """生成 schedule 之后的路由函数: 给有可执行程序的每个实例发一个 Send, 并行执行 fieldwork.

    返回 Send 列表时, LangGraph 同时启动多个 fieldwork, 每个拿到自己的 FieldworkInput;
    P2 程序都已结束时返回 "fan_in", 直接去汇合. 外层传 nodes 是为了拿程序库 (闭包, 同 gate_route).
    派不出却还有计划中或执行中的程序时抛 ScheduleStuck: 返回 fan_in 会与 after_fan_in 来回空转到递归上限.
    """

    def route(state: EngagementState) -> list[Send] | str:
        # 本轮可以执行的程序: 前置程序都已完成
        ready = set(ready_procedures(state["procedure_pack"], nodes.deps.library))
        # 调度表每行是一个实例; 只给有可执行程序的实例发 Send, 并且只带上它的可执行程序
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
        if sends:
            return sends
        # 一个都派不出, 却还有要执行的程序: 说明前置卡在不会再变的状态上, 正常不会出现
        unfinished = [p for p in p2_procedures(state["procedure_pack"]) if p.status in UNFINISHED]
        if unfinished:
            raise ScheduleStuck(_stuck_detail(state["procedure_pack"], unfinished, nodes.deps.library))
        return "fan_in"

    return route


def after_fan_in(state: EngagementState) -> Literal["schedule", "await_events", "completion"]:
    """fan_in 汇合之后去哪. 按顺序判断, 先命中先走:

    有待决的复核任务 → await_events 等审计师; 还有计划中或执行中的 P2 程序 → schedule 派下一轮;
    P2 程序全部完成 → completion. 剩下的只可能是 awaiting_human 却没有待决任务, 抛 ScheduleStuck:
    进 await_events 会永远等不到决定.
    """
    p2 = p2_procedures(state["procedure_pack"])
    if any(t.status is TaskStatus.pending for t in state["review_tasks"]):
        return "await_events"
    if any(p.status in UNFINISHED for p in p2):
        return "schedule"
    if all(p.status is ProcedureStatus.completed for p in p2):
        return "completion"
    waiting = [p.id for p in p2 if p.status is ProcedureStatus.awaiting_human]
    raise ScheduleStuck(f"P2 调度卡住: {', '.join(waiting)} 为 awaiting_human, 却没有待决的复核任务")


def build_graph(nodes: GraphNodes) -> StateGraph[EngagementState]:
    """搭主图: 先登记节点, 再连固定边, 最后连条件边与闸门出口. 返回未编译的图."""
    g = StateGraph(EngagementState)

    # ── 节点: 名字 → 节点函数 ──
    g.add_node("p0_intel", nodes.p0_intel)
    g.add_node("p1_ingest", nodes.p1_ingest)
    g.add_node("p1_analytics", nodes.p1_analytics)
    g.add_node("assemble_pack", nodes.assemble_pack)
    g.add_node("schedule", nodes.schedule)
    # fieldwork 接收 Send 传来的 FieldworkInput 而不是整个状态, 与 add_node 声明的类型对不上, 故忽略类型检查
    g.add_node("fieldwork", nodes.fieldwork)  # type: ignore[arg-type]
    g.add_node("fan_in", nodes.fan_in)
    g.add_node("await_events", nodes.await_events)
    g.add_node("completion", nodes.completion)
    g.add_node("report_draft", nodes.report_draft)
    g.add_node("archive", nodes.archive)
    g.add_node("declined", nodes.declined)
    for node in GATES:
        g.add_node(node, nodes.gate)  # 闸门节点什么都不做, 出口全由 gate_route 决定

    # ── 固定边: A 执行完一定去 B ──
    g.add_edge(START, "p0_intel")
    g.add_edge("p0_intel", "gate_acc01")
    g.add_edge("p1_ingest", "p1_analytics")
    g.add_edge("p1_analytics", "gate_p1_risk")
    g.add_edge("assemble_pack", "schedule")
    # ── P2 循环: schedule → fieldwork × N → fan_in → 再判断去哪 ──
    # 条件边: 执行完由路由函数选下一个节点; 第三个参数列出所有可能的去向
    g.add_conditional_edges("schedule", route_schedule(nodes), ["fieldwork", "fan_in"])
    g.add_edge("fieldwork", "fan_in")  # 所有并行实例都结束后才进 fan_in
    g.add_conditional_edges("fan_in", after_fan_in, ["schedule", "await_events", "completion"])
    g.add_edge("await_events", "fan_in")  # 审计师处理完复核, 回 fan_in 重新判断
    g.add_edge("completion", "gate_fin")
    g.add_edge("report_draft", "gate_rpt01")
    g.add_edge("archive", END)
    g.add_edge("declined", END)

    # ── 闸门出口: 路由函数返回的出口名 → 下一个节点 ──
    exits: dict[GateId, dict[Hashable, str]] = {
        GateId.ACC_01: {"approve": "p1_ingest", "reject": "declined", "rerun": "p0_intel"},
        GateId.PLN_01_03: {"approve": "assemble_pack", "reject": "p1_analytics"},
        GateId.FIN_01: {"approve": "report_draft", "reject": "completion"},
        GateId.RPT_01: {"approve": "archive", "reject": "report_draft"},
    }
    for gate, node in GATE_NODES.items():
        # 每道闸门再加一个 represent 出口, 指向闸门自己
        g.add_conditional_edges(node, gate_route(gate), {**exits[gate], "represent": node})
    return g


def compile_graph(
    nodes: GraphNodes, checkpointer: BaseCheckpointSaver[str] | None = None
) -> CompiledStateGraph[EngagementState]:
    """编译主图: checkpointer 每步存检查点 (测试用内存, 正式运行用 Postgres); 四道闸门前静态中断."""
    return build_graph(nodes).compile(checkpointer=checkpointer, interrupt_before=GATES)
