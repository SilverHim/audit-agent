"""StateQuery (A 负责): 项目总览与闸门材料, 供前端读取.

图的位置 (停在哪道闸门, 是否等复核) 读检查点; 程序状态与复核任务读 AuditOutputStore; 是否在执行读 runner.
"""

from collections import Counter

from langgraph.graph.state import CompiledStateGraph

from backend.agents.base import AgentDeps
from backend.contracts.models import (
    ClientProfile,
    Engagement,
    Phase,
    Procedure,
    ProcedureRun,
    RerunRequest,
    ReviewTask,
    TaskStatus,
)
from backend.contracts.state import EngagementState, fill_defaults
from backend.contracts.views import (
    EngagementOverview,
    GateView,
    ProcedureSummary,
)
from backend.orchestration.gates import current_gate, current_spec
from backend.orchestration.runner import GraphRunner, at_rerun_point, awaiting_data, resume_required, review_waits
from backend.services.data_quality import missing_tables
from backend.services.staleness import rerunnable, stale_procedures, superseded_runs

PHASE_ORDER: list[Phase] = list(Phase)  # 阶段的定义顺序 P0 ... P4, 总览按此排列程序


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
        values = fill_defaults(snapshot.values)  # 未启动的项目检查点为空, 下面几项取缺省值 None

        # 程序状态与待决任务以 Store 为准: DecisionSubmit 写入后, Store 先于检查点更新
        procedures = await self.deps.output_store.latest(eid, Procedure)
        pending = await self.deps.output_store.latest(eid, ReviewTask, status=TaskStatus.pending)
        pending_by_procedure = Counter(t.procedure_id for t in pending)  # 程序编号 -> 待决任务数, 没有的计 0
        # 过期 (需求 R274): 读的数据在最近一次执行之后变过, 或依赖的程序过期或在它之后重跑过 (services.staleness);
        # 只针对有当前结果的程序, 已被重跑取代而还没有新执行的不算
        runs = await self.deps.output_store.latest(eid, ProcedureRun)
        superseded = superseded_runs(await self.deps.output_store.latest(eid, RerunRequest))
        ordered = sorted(procedures, key=lambda p: (PHASE_ORDER.index(p.phase), p.id))
        changes = await self.deps.data.changes(eid)
        stale = set(stale_procedures(ordered, runs, changes, self.deps.library, superseded))
        allowed = set(rerunnable(ordered, stale))

        summaries = []
        for p in ordered:
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
                    stale=p.id in stale,
                    rerunnable=p.id in allowed,
                )
            )

        waiting_data = awaiting_data(snapshot)
        return EngagementOverview(
            engagement=engagement,
            client_name=profile.legal_name,
            running=self.runner.is_running(eid),
            run_error=self.runner.run_error(eid) or await self._resume_hint(eid),
            current_gate=current_gate(snapshot.next, values),
            # 节点内 interrupt() 暂停时, 对应的待执行任务带 interrupts; 闸门是静态中断, 不带. 等数据就绪另计
            awaiting_review=bool(review_waits(snapshot)),
            awaiting_data=waiting_data,
            missing_tables=missing_tables(await self.deps.data.stats(eid)) if waiting_data else [],
            procedures=summaries,
            stale_procedure_ids=[p.id for p in ordered if p.id in stale],
            # 图停在可重跑的位置且未锁定, 不在执行 (RESUME_REQUIRED 等另见 run_error, 提交时再判断)
            can_rerun=not engagement.locked and not self.runner.is_running(eid) and at_rerun_point(snapshot),
            pending_tasks=len(pending),
            materiality_cents=values["materiality_cents"],
            performance_materiality_cents=values["performance_materiality_cents"],
            clearly_trivial_cents=values["clearly_trivial_cents"],
            opinion_route=values["opinion_route"],
        )

    async def _resume_hint(self, eid: str) -> str | None:
        """Store 已写入而检查点没跟上 (决定写入后检查点写失败, 或进程在两者之间中断) 时的提示: 新决定会被拒,
        须先 resume. 没有则为 None.

        后台执行或决定提交进行中 (持有项目锁) 时两者本来就暂时不一致, 不判断. 项目, 检查点与 Store 在这里重读,
        读取期间项目锁被取得过 (有提交开始) 也不判断: 否则可能拿提交前的检查点比提交后的 Store, 误报.
        """
        mark = self.runner.lock_count(eid)
        if self.runner.is_running(eid):
            return None
        engagement = await self.deps.engagement_store.get(eid)
        snapshot = await self.app.aget_state(GraphRunner.config(eid))
        if engagement is None or snapshot.metadata is None:
            return None
        missing = await resume_required(self.deps, engagement, snapshot)
        if missing is None or self.runner.lock_count(eid) != mark:
            return None
        return f"RESUME_REQUIRED: 检查点未跟上 Store ({missing}), 须先 resume"

    async def gate(self, eid: str) -> GateView | None:
        """当前闸门的呈报材料 (登记表的 view); 不在闸门 (执行中, 等复核或已结束) 返回 None.

        PLN-01/03 的材料另列按当前风险等级没有程序覆盖的审计目标 (有则不能通过, 见 DecisionSubmit).
        RPT-01 的材料为当前报告 (修订轮次最大的一版) 与此前的退回决定 (含批注; 信息不足的决定不在其中).

        GateView 的 target_id, target_version 标明材料版本: 审计师提交决定时带回, DecisionSubmit 据此判断是否过期.
        停在闸门而材料缺失 (状态与流程不符) 时抛 LookupError.
        现有材料都不含判断与模型留痕; 以后的闸门材料要含时, 须经 deps.blind_filter 转成视图再呈报 (接口文档第 10 节).
        """
        snapshot = await self.app.aget_state(GraphRunner.config(eid))
        values = fill_defaults(snapshot.values)
        spec = current_spec(snapshot.next, values)
        if spec is None:
            return None
        material, view = await spec.view(self.deps, eid, values)
        return GateView(gate=spec.gate, target_id=material.id, target_version=material.version, material=view)
