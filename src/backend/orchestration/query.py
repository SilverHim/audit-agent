"""StateQuery (A 负责): 项目总览, 闸门材料与审计计划, 供前端读取.

图的位置 (停在哪道闸门, 是否等复核) 与审计计划读检查点; 程序状态与复核任务读 AuditOutputStore; 是否在执行读 runner;
系统正在做的事 (数据作业与程序执行, 含正在运行的程序) 读 deps.activities (需求 R325, R361).
"""

from collections import Counter
from collections.abc import Sequence

from langgraph.graph.state import CompiledStateGraph

from backend.agents.base import AgentDeps
from backend.contracts.models import (
    AuditObjective,
    AuditPlan,
    ClientProfile,
    Engagement,
    GateId,
    Phase,
    PlanItem,
    Procedure,
    ProcedureRun,
    RerunRequest,
    ReviewTask,
    RiskLevel,
    TaskStatus,
)
from backend.contracts.state import EngagementState, fill_defaults
from backend.contracts.views import (
    EngagementOverview,
    GateView,
    PhaseProgress,
    PlanRow,
    PlanView,
    ProcedureSummary,
)
from backend.orchestration.gates import current_gate, current_plan, current_spec
from backend.orchestration.nodes import SETTLED
from backend.orchestration.runner import (
    GraphRunner,
    at_rerun_point,
    at_withdraw_point,
    awaiting_data,
    correction_wait,
    resume_required,
    review_waits,
)
from backend.services.data_quality import missing_tables
from backend.services.features import frozen_features
from backend.services.staleness import rerunnable, stale_procedures, superseded_runs

PHASE_ORDER: list[Phase] = list(Phase)  # 阶段的定义顺序 P0 ... P4, 总览按此排列程序


def plan_row(objective_id: str | None, item: PlanItem) -> PlanRow:
    """程序表的一行. 范围: params 里有整数 sample_size (不含布尔) 为抽样, 否则全量 (现有程序都没有样本量参数)."""
    size = item.params.get("sample_size")
    if isinstance(size, int) and not isinstance(size, bool):
        return PlanRow(objective_id=objective_id, procedure_id=item.procedure_id, extent="sample", sample_size=size)
    return PlanRow(objective_id=objective_id, procedure_id=item.procedure_id, extent="full")


def plan_rows(plan: AuditPlan, objectives: Sequence[AuditObjective]) -> list[PlanRow]:
    """计划的程序表: 针对几个目标的程序每个目标一行; 按目标在 objectives 中的顺序分组 (不在其中的排在它们之后,
    按 ID), 组内按程序编号; 没有目标的程序排最后."""
    order = {o.id: n for n, o in enumerate(objectives)}
    rows = [plan_row(oid, item) for item in plan.items for oid in (item.objective_ids or (None,))]

    def key(row: PlanRow) -> tuple[int, str, str]:
        rank = len(order) + 1 if row.objective_id is None else order.get(row.objective_id, len(order))
        return rank, row.objective_id or "", row.procedure_id

    return sorted(rows, key=key)


def plan_view(values: EngagementState) -> PlanView:
    """审计计划页的数据, 只取状态 (已补缺省值): 当前计划, 各修订的最新版本, PLN-15 决定, 当前计划涉及的审计目标,
    特别风险 (特别风险目标下等级为 significant 的风险项; 同一目标下等级更低的不算, 目标等级取其下最高), 程序表.
    没有计划时 current_plan_id 为空, 各列表为空. analytics 为初步分析结果 (需求 R372): 计划页在 PLN-01/03 闸门通过后
    仍能看推荐重要性与复筛; 还没执行时为空."""
    plan = current_plan(values["audit_plans"])
    if plan is None:
        return PlanView(
            current_plan_id=None,
            plans=[],
            decisions=[],
            objectives=[],
            risks=[],
            rows=[],
            analytics=values["analytics"],
        )
    referenced = {oid for item in plan.items for oid in item.objective_ids}
    significant = set(plan.significant_risk_objective_ids)
    return PlanView(
        current_plan_id=plan.id,
        plans=sorted(values["audit_plans"], key=lambda p: p.revision),
        decisions=sorted((d for d in values["gate_decisions"] if d.gate is GateId.PLN_15), key=lambda d: d.decided_at),
        objectives=[o for o in values["audit_objectives"] if o.id in referenced],
        risks=[
            r for r in values["risk_register"] if r.objective_id in significant and r.level is RiskLevel.significant
        ],
        rows=plan_rows(plan, values["audit_objectives"]),
        analytics=values["analytics"],
    )


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
        if values["analytics"] and values["analytics"].materiality_stale:
            stale.add("PLN-07")
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
        correction = correction_wait(snapshot)
        running = self.runner.is_running(eid)
        current = [p for p in procedures if p.phase is engagement.phase]
        activities = self.deps.activities.activities(eid)
        progress = PhaseProgress(
            phase=engagement.phase,
            done=sum(1 for p in current if p.status in SETTLED),
            total=len(current),
            # 程序执行的活动 (开始了而还没写结束留痕的执行, 后台推进结束时清掉) 所属的程序, 按开始的先后, 去重
            running=list(dict.fromkeys(a.current for a in activities if a.kind == "procedure" and a.current)),
        )
        return EngagementOverview(
            engagement=engagement,
            client_name=profile.legal_name,
            running=running,
            run_error=self.runner.run_error(eid) or await self._resume_hint(eid),
            current_gate=current_gate(snapshot.next, values),
            # 节点内 interrupt() 暂停时, 对应的待执行任务带 interrupts; 闸门是静态中断, 不带. 等数据就绪另计
            awaiting_review=bool(review_waits(snapshot)),
            awaiting_data=waiting_data,
            awaiting_correction=correction is not None,
            awaiting_correction_ids=correction["misstatement_ids"] if correction is not None else [],
            missing_tables=missing_tables(await self.deps.data.stats(eid)) if waiting_data else [],
            procedures=summaries,
            progress=progress,
            stale_procedure_ids=[p.id for p in ordered if p.id in stale],
            # 图停在可重跑的位置且未锁定, 不在执行 (RESUME_REQUIRED 等另见 run_error, 提交时再判断)
            can_rerun=not engagement.locked and not self.runner.is_running(eid) and at_rerun_point(snapshot),
            can_withdraw=not engagement.locked and not running and at_withdraw_point(snapshot),
            pending_tasks=len(pending),
            materiality_cents=values["materiality_cents"],
            performance_materiality_cents=values["performance_materiality_cents"],
            clearly_trivial_cents=values["clearly_trivial_cents"],
            opinion_route=values["opinion_route"],
            features=sorted(frozen_features(engagement.version_snapshot, self.deps.features)),
            run_languages={run.id: run.language for run in runs if run.language != engagement.working_language},
            activities=activities,
            last_failure=self.deps.activities.last_failure(eid),
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
        """
        snapshot = await self.app.aget_state(GraphRunner.config(eid))
        values = fill_defaults(snapshot.values)
        spec = current_spec(snapshot.next, values)
        if spec is None:
            return None
        material, view = await spec.view(self.deps, eid, values)
        return GateView(gate=spec.gate, target_id=material.id, target_version=material.version, material=view)

    async def plan(self, eid: str) -> PlanView:
        """审计计划页 (需求 R314): 读检查点状态, 不另查 Store (见 plan_view); 停在闸门, 执行中与归档后都可读."""
        snapshot = await self.app.aget_state(GraphRunner.config(eid))
        return plan_view(fill_defaults(snapshot.values))
