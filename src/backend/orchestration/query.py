"""StateQuery (A 负责): 项目总览与闸门材料, 供前端读取.

图的位置 (停在哪道闸门, 是否等复核) 读检查点; 程序状态与复核任务读 AuditOutputStore; 是否在执行读 runner.
"""

from collections import Counter

from langgraph.graph.state import CompiledStateGraph

from backend.agents.base import AgentDeps
from backend.contracts.models import ClientProfile, Engagement, GateId, Phase, Procedure, ReviewTask, TaskStatus
from backend.contracts.state import EngagementState
from backend.contracts.views import AcceptanceMaterial, EngagementOverview, GateView, ProcedureSummary
from backend.orchestration.graph import GATE_NODES
from backend.orchestration.runner import GraphRunner

GATE_OF_NODE: dict[str, GateId] = {node: gate for gate, node in GATE_NODES.items()}  # 闸门节点名 -> 闸门
PHASE_ORDER: list[Phase] = list(Phase)  # 阶段的定义顺序 P0 ... P4, 总览按此排列程序


def current_gate(next_nodes: tuple[str, ...]) -> GateId | None:
    """图停在哪道闸门前: next_nodes 中有闸门节点则返回该闸门, 否则返回 None.

    例: ("gate_acc01",) → GateId.ACC_01; ("p0_intel",) → None; () (未启动或已结束) → None.
    """
    for node in next_nodes:
        if node in GATE_OF_NODE:
            return GATE_OF_NODE[node]
    return None


class GraphStateQuery:
    """StateQuery 的实现: 给前端读项目总览与闸门材料."""

    def __init__(self, app: CompiledStateGraph[EngagementState], deps: AgentDeps, runner: GraphRunner) -> None:
        self.app = app  # 编译好的主图, 用于读检查点
        self.deps = deps  # 外部依赖
        self.runner = runner  # 用于读项目是否在执行及最近一次错误

    async def _engagement_and_profile(self, eid: str) -> tuple[Engagement, ClientProfile]:
        """项目及其客户的最新版画像; 任一查不到抛 LookupError."""
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        profile = await self.deps.engagement_store.client_profile(engagement.client_id)
        if profile is None:
            raise LookupError(f"client profile {engagement.client_id} not found")
        return engagement, profile

    async def overview(self, eid: str) -> EngagementOverview:
        """项目总览: 前端每 2 秒轮询一次, 据此显示进度, 当前闸门, 程序状态表与待办数."""
        engagement, profile = await self._engagement_and_profile(eid)
        snapshot = await self.app.aget_state(GraphRunner.config(eid))
        values = snapshot.values  # 未启动的项目为空字典, 故下面用 get 取值

        # 程序状态与待决任务以 Store 为准: DecisionSubmit 写入后, Store 先于检查点更新
        procedures = await self.deps.output_store.latest(eid, Procedure)
        pending = await self.deps.output_store.latest(eid, ReviewTask, status=TaskStatus.pending)
        pending_by_procedure = Counter(t.procedure_id for t in pending)  # 程序编号 -> 待决任务数, 没有的计 0

        summaries = []
        for p in sorted(procedures, key=lambda p: (PHASE_ORDER.index(p.phase), p.id)):
            template = self.deps.library.load(p.id)  # 名称, 执行者, 引擎取自程序模板
            summaries.append(
                ProcedureSummary(
                    id=p.id,
                    name=template.name,
                    phase=p.phase,
                    owner=template.owner,
                    engine=template.engine,
                    autonomy_level=p.autonomy_level,
                    status=p.status,
                    pending_tasks=pending_by_procedure[p.id],
                )
            )

        return EngagementOverview(
            engagement=engagement,
            client_name=profile.legal_name,
            running=self.runner.is_running(eid),
            run_error=self.runner.run_error(eid),
            current_gate=current_gate(snapshot.next),
            # 节点内 interrupt() 暂停时, 对应的待执行任务带 interrupts; 闸门是静态中断, 不带
            awaiting_review=any(task.interrupts for task in snapshot.tasks),
            procedures=summaries,
            pending_tasks=len(pending),
            materiality_cents=values.get("materiality_cents"),
            performance_materiality_cents=values.get("performance_materiality_cents"),
            clearly_trivial_cents=values.get("clearly_trivial_cents"),
            opinion_route=values.get("opinion_route"),
        )

    async def gate(self, eid: str) -> GateView | None:
        """当前闸门的呈报材料; 不在闸门 (执行中, 等复核或已结束) 返回 None. 本版先实现 ACC-01."""
        snapshot = await self.app.aget_state(GraphRunner.config(eid))
        gate = current_gate(snapshot.next)
        if gate is None:
            return None
        if gate is not GateId.ACC_01:
            raise NotImplementedError(f"{gate} 的闸门材料尚未实现")

        scorecard = snapshot.values["acceptance_scorecard"]
        # 状态中的例外含历次执行的 (upsert_by_id 只增不删), 只取当前评分卡引用的
        exceptions = [x for x in snapshot.values["exceptions"] if x.id in scorecard.exception_ids]
        _, profile = await self._engagement_and_profile(eid)
        material = AcceptanceMaterial(scorecard=scorecard, exceptions=exceptions, client_profile=profile)
        # target_id, target_version 标明材料版本: 审计师提交决定时带回, DecisionSubmit 据此判断是否过期
        return GateView(gate=gate, target_id=scorecard.id, target_version=scorecard.version, material=material)
