"""主图拓扑 (A 负责): 有哪些节点, 节点之间怎么连. 流程见 Demo需求文档.md 4.1 节:

p0_intel → gate_acc01 → { p1_await_data | declined | p0_intel (客户画像更新后重跑) }
p1_await_data (等审计师接入数据并确认已齐) → p1_ingest → { p1_analytics | fan_in (执行阶段补跑 DOC-04 后) }
p1_analytics → gate_p1_risk → { assemble_pack | p1_analytics }
assemble_pack → schedule ⇉ fieldwork × N ⇉ fan_in → { p1_ingest | schedule | await_events | await_data | completion }
await_events, await_data → fan_in
completion → gate_fin → { report_draft | completion }
report_draft → gate_rpt01 → { archive | report_draft }

读法: → 固定边; { a | b } 条件边, 由路由函数按状态选一个; ⇉ 用 Send 并行派发多个实例.
四道闸门 (gate_*) 前静态中断, 等审计师决定; 每道闸门另有 represent 出口, 回到自己重新呈报.
p1_await_data, await_events, await_data 与 P2 以外节点等人 (失败或有待复核任务) 时在节点内 interrupt()
(种类见 waits.py).
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, TypeVar

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from backend.contracts.models import (
    GateId,
    Phase,
    Procedure,
    ProcedureStatus,
    TaskStatus,
)
from backend.contracts.state import EngagementState, StatePatch, fill_defaults
from backend.orchestration import gates
from backend.orchestration.nodes import NODE_PROCEDURES, FieldworkInput, GraphNodes
from backend.orchestration.scheduler import p2_procedures, ready_procedures
from backend.services.features import BASELINE
from backend.services.procedure_library import ProcedureLibrary

# 闸门的节点, 出口, 材料与路由都在登记表 (gates.GATES); 这里给基线功能集下的两个常用视图
GATE_NODES: dict[GateId, str] = {spec.gate: spec.node for spec in gates.registered(BASELINE)}  # 闸门 → 节点名
GATES = gates.gate_nodes(BASELINE)  # 基线功能集下的闸门节点名 (测试与工具用); 编译按功能集取 gates.gate_nodes

UNFINISHED = (ProcedureStatus.planned, ProcedureStatus.executing)  # 还要派发执行的程序状态
SETTLED = (ProcedureStatus.completed, ProcedureStatus.no_data)  # P2 结清: 已完成, 或未执行 (无数据, 需求 R280)
LATE = NODE_PROCEDURES["p1_ingest"]  # 执行阶段等到数据后经 p1_ingest 补跑的 P2 以外程序 (需求 R272)


T = TypeVar("T")


class StateNode(Protocol):
    """节点函数: 收整个状态, 返回要更新的片段. 参数名须为 state, 与 LangGraph add_node 的声明一致."""

    def __call__(self, state: EngagementState) -> Awaitable[StatePatch]: ...


class FieldworkNode(Protocol):
    """fieldwork 节点函数: 收 Send 带来的 FieldworkInput."""

    def __call__(self, payload: FieldworkInput) -> Awaitable[StatePatch]: ...


def node_with_defaults(node: StateNode) -> StateNode:
    """登记节点函数时包一层: 先经 fill_defaults 补上检查点缺的键 (旧检查点没有新字段) 再调用. functools.wraps 让
    LangGraph 按原函数的签名注入参数 (如 config), 包装函数原样转交."""

    @functools.wraps(node)
    async def wrapped(state: EngagementState, **injected: Any) -> StatePatch:
        return await node(fill_defaults(state), **injected)

    return wrapped


def fieldwork_with_defaults(node: FieldworkNode) -> FieldworkNode:
    """fieldwork 的输入是 Send 带来的 FieldworkInput, 补的是其中的状态快照 (旧检查点里待派发的 Send 带旧状态)."""

    @functools.wraps(node)
    async def wrapped(payload: FieldworkInput, **injected: Any) -> StatePatch:
        return await node(
            FieldworkInput(
                instance=payload["instance"],
                procedure_ids=payload["procedure_ids"],
                state=fill_defaults(payload["state"]),
            ),
            **injected,
        )

    return wrapped


def route_with_defaults(route: Callable[[EngagementState], T]) -> Callable[[EngagementState], T]:
    """登记路由函数时包一层, 同 node_with_defaults."""

    @functools.wraps(route)
    def wrapped(state: EngagementState, **injected: Any) -> T:
        return route(fill_defaults(state), **injected)

    return wrapped


def gate_route(gate: GateId) -> Callable[[EngagementState], gates.GateRoute]:
    """某道闸门单独的路由函数 (gates.gate_route 按闸门编号取登记行)."""
    return gates.gate_route(gates.spec_of(gate))


class ScheduleStuck(RuntimeError):
    """P2 调度卡住: 程序状态与复核任务对不上 (如 awaiting_human 却没有待处理任务), 再走会空转或永远暂停."""


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
    派不出时返回 "fan_in", 由 after_fan_in 判断去向 (程序等数据时去 await_data, 卡住时由它抛 ScheduleStuck, 不会
    来回空转). 外层传 nodes 是为了拿程序库与并行上限 (闭包, 同 gate_route).
    一次最多派 nodes.parallel_cap 个实例: 不同层的实例同时就绪时 (如几条程序同时重跑) 可能超过上限,
    按调度表顺序 (层号小的在前) 派到上限, 其余留到 fan_in 之后的下一轮.
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
        return sends[: nodes.parallel_cap] if sends else "fan_in"

    return route


AfterFanIn = Literal["p1_ingest", "schedule", "await_events", "await_data", "completion"]


def after_fan_in(nodes: GraphNodes) -> Callable[[EngagementState], AfterFanIn]:
    """生成 fan_in 之后的路由函数. 按顺序判断, 先命中先走 (先执行, 没有可执行的再等):

    P2 以外等到了数据的程序 (LATE 中回到 planned 的, 即 DOC-04) → p1_ingest 补跑 (需求 R272);
    有可派发的 P2 程序 (待执行且前置都已完成) → schedule, 不因别的程序等复核或等数据而停下;
    有待决的复核任务 → await_events 等审计师;
    P2 程序全部结清 (completed, 或 no_data 未执行) 且没有程序在等数据 → completion;
    有程序在等数据 (awaiting_data, 任何阶段) → await_data 等数据到达 (需求 R272).
    都不是时抛 ScheduleStuck: 有待执行的程序却派不出 (前置卡在 awaiting_human 而没有待决任务),
    或 awaiting_human 却没有待决任务, 进 await_events 会永远等不到决定.
    外层传 nodes 是为了拿程序库 (闭包, 同 route_schedule).
    """

    def route(state: EngagementState) -> AfterFanIn:
        pack = state["procedure_pack"]
        if any(p.id in LATE and p.status is ProcedureStatus.planned for p in pack):
            return "p1_ingest"
        if ready_procedures(pack, nodes.deps.library):
            return "schedule"
        if any(t.status is TaskStatus.pending for t in state["review_tasks"]):
            return "await_events"
        p2 = p2_procedures(pack)
        waiting_data = any(p.status is ProcedureStatus.awaiting_data for p in pack)
        if all(p.status in SETTLED for p in p2) and not waiting_data:
            return "completion"
        if waiting_data:
            return "await_data"
        unfinished = [p for p in p2 if p.status in UNFINISHED]
        if unfinished:
            raise ScheduleStuck(_stuck_detail(pack, unfinished, nodes.deps.library))
        waiting = [p.id for p in p2 if p.status is ProcedureStatus.awaiting_human]
        raise ScheduleStuck(f"P2 调度卡住: {', '.join(waiting)} 为 awaiting_human, 却没有待决的复核任务")

    return route


def after_ingest(state: EngagementState) -> Literal["p1_analytics", "fan_in"]:
    """p1_ingest 之后: 执行阶段 (补跑等到了合同的 DOC-04, 需求 R272) 回 fan_in, 否则 (P1) 往下到 p1_analytics."""
    return "fan_in" if state["phase"] is Phase.P2_Executing else "p1_analytics"


def build_graph(nodes: GraphNodes, features: frozenset[str] = BASELINE) -> StateGraph[EngagementState]:
    """搭主图: 先登记节点, 再连固定边, 最后连条件边与闸门出口. 返回未编译的图.

    features: 启用的功能 (services/features.py). 基线功能集下的主图即下面的结构 (tests/test_graph.py 守卫);
    各功能的新节点与闸门只在该功能启用时加入 (由各功能组的需求加, 目前没有).
    """
    g = StateGraph(EngagementState)

    # ── 节点: 名字 → 节点函数. 节点与路由函数都包一层 fill_defaults, 旧检查点缺的键读到缺省值 ──
    g.add_node("p0_intel", node_with_defaults(nodes.p0_intel))
    g.add_node("p1_await_data", node_with_defaults(nodes.p1_await_data))
    g.add_node("p1_ingest", node_with_defaults(nodes.p1_ingest))
    g.add_node("p1_analytics", node_with_defaults(nodes.p1_analytics))
    g.add_node("assemble_pack", node_with_defaults(nodes.assemble_pack))
    g.add_node("schedule", node_with_defaults(nodes.schedule))
    # fieldwork 接收 Send 传来的 FieldworkInput 而不是整个状态, 与 add_node 声明的类型对不上, 故忽略类型检查
    g.add_node("fieldwork", fieldwork_with_defaults(nodes.fieldwork))  # type: ignore[call-overload]
    g.add_node("fan_in", node_with_defaults(nodes.fan_in))
    g.add_node("await_events", node_with_defaults(nodes.await_events))
    g.add_node("await_data", node_with_defaults(nodes.await_data))
    g.add_node("completion", node_with_defaults(nodes.completion))
    g.add_node("report_draft", node_with_defaults(nodes.report_draft))
    g.add_node("archive", node_with_defaults(nodes.archive))
    g.add_node("declined", node_with_defaults(nodes.declined))
    gate_nodes = gates.node_gates(features)  # 闸门节点 → 挂在其上的闸门, 按功能集过滤
    for node in gate_nodes:
        g.add_node(node, node_with_defaults(nodes.gate))  # 闸门节点什么都不做, 出口全由节点的路由决定

    # ── 固定边: A 执行完一定去 B ──
    g.add_edge(START, "p0_intel")
    g.add_edge("p0_intel", "gate_acc01")
    g.add_edge("p1_await_data", "p1_ingest")
    # P1 中往下到 p1_analytics; 执行阶段补跑 DOC-04 (等到了合同) 后回 fan_in
    g.add_conditional_edges("p1_ingest", route_with_defaults(after_ingest), ["p1_analytics", "fan_in"])
    g.add_edge("p1_analytics", "gate_p1_risk")
    g.add_edge("assemble_pack", "schedule")
    # ── P2 循环: schedule → fieldwork × N → fan_in → 再判断去哪 ──
    # 条件边: 执行完由路由函数选下一个节点; 第三个参数列出所有可能的去向
    g.add_conditional_edges("schedule", route_with_defaults(route_schedule(nodes)), ["fieldwork", "fan_in"])
    g.add_edge("fieldwork", "fan_in")  # 所有并行实例都结束后才进 fan_in
    g.add_conditional_edges(
        "fan_in",
        route_with_defaults(after_fan_in(nodes)),
        ["p1_ingest", "schedule", "await_events", "await_data", "completion"],
    )
    g.add_edge("await_events", "fan_in")  # 审计师处理完复核, 回 fan_in 重新判断
    g.add_edge("await_data", "fan_in")  # 程序要的数据到了 (或重查), 回 fan_in 重新判断
    g.add_edge("completion", "gate_fin")
    g.add_edge("report_draft", "gate_rpt01")
    g.add_edge("archive", END)
    g.add_edge("declined", END)

    # ── 闸门出口: 每个闸门节点一条条件边, 出口键 → 下一个节点 (gates.path_map; represent 回节点自身) ──
    for node, specs in gate_nodes.items():
        g.add_conditional_edges(node, route_with_defaults(gates.node_route(specs)), gates.path_map(specs, features))
    return g


def compile_graph(
    nodes: GraphNodes, checkpointer: BaseCheckpointSaver[str] | None = None
) -> CompiledStateGraph[EngagementState]:
    """编译主图: checkpointer 每步存检查点 (测试用内存, 正式运行用 Postgres); 闸门节点前静态中断.
    功能集取 nodes.deps.features."""
    features = nodes.deps.features
    interrupts = gates.gate_nodes(features)
    return build_graph(nodes, features).compile(checkpointer=checkpointer, interrupt_before=interrupts)
