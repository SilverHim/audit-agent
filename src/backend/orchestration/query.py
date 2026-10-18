"""StateQuery (A 负责): 项目总览与闸门材料, 供前端读取.

图的位置 (停在哪道闸门, 是否等复核) 读检查点; 程序状态与复核任务读 AuditOutputStore; 是否在执行读 runner.
"""

from collections import Counter

from langgraph.graph.state import CompiledStateGraph

from backend.agents.base import AgentDeps
from backend.contracts.models import (
    AuditException,
    ClientProfile,
    Engagement,
    GateId,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureRun,
    ReviewTask,
    TaskStatus,
)
from backend.contracts.state import EngagementState
from backend.contracts.views import (
    AcceptanceMaterial,
    CompletionMaterial,
    EngagementOverview,
    GateView,
    PlanningMaterial,
    ProcedureSummary,
    ReportMaterial,
)
from backend.orchestration.graph import GATE_NODES
from backend.orchestration.nodes import uncovered_objectives
from backend.orchestration.runner import GraphRunner, awaiting_data, resume_required, review_waits
from backend.services.data_quality import missing_tables

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


def stale(run: ProcedureRun | None, current_version: int) -> bool:
    """程序是否过期 (需求 R16): 最近一次执行记了数据版本, 且比当前数据版本旧."""
    return run is not None and run.data_version is not None and run.data_version < current_version


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
        # 过期 (需求 R16): 最近一次执行基于的数据版本比当前旧; 完整的失效传播以后再做
        current_version = (await self.deps.data.readiness(eid)).data_version
        last_runs: dict[str, ProcedureRun] = {}
        for run in await self.deps.output_store.latest(eid, ProcedureRun):
            if run.procedure_id not in last_runs or run.run_no > last_runs[run.procedure_id].run_no:
                last_runs[run.procedure_id] = run

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
                    stale=stale(last_runs.get(p.id), current_version),
                )
            )

        waiting_data = awaiting_data(snapshot)
        return EngagementOverview(
            engagement=engagement,
            client_name=profile.legal_name,
            running=self.runner.is_running(eid),
            run_error=self.runner.run_error(eid) or await self._resume_hint(eid),
            current_gate=current_gate(snapshot.next),
            # 节点内 interrupt() 暂停时, 对应的待执行任务带 interrupts; 闸门是静态中断, 不带. 等数据就绪另计
            awaiting_review=bool(review_waits(snapshot)),
            awaiting_data=waiting_data,
            missing_tables=missing_tables(await self.deps.data.stats(eid)) if waiting_data else [],
            procedures=summaries,
            pending_tasks=len(pending),
            materiality_cents=values.get("materiality_cents"),
            performance_materiality_cents=values.get("performance_materiality_cents"),
            clearly_trivial_cents=values.get("clearly_trivial_cents"),
            opinion_route=values.get("opinion_route"),
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
        """当前闸门的呈报材料; 不在闸门 (执行中, 等复核或已结束) 返回 None.

        PLN-01/03 的材料另列按当前风险等级没有程序覆盖的审计目标 (有则不能通过, 见 DecisionSubmit).
        RPT-01 的材料为当前报告 (修订轮次最大的一版) 与此前的退回决定 (含批注; 信息不足的决定不在其中).

        GateView 的 target_id, target_version 标明材料版本: 审计师提交决定时带回, DecisionSubmit 据此判断是否过期.
        停在闸门而材料缺失 (状态与流程不符) 时抛 LookupError.
        """
        snapshot = await self.app.aget_state(GraphRunner.config(eid))
        gate = current_gate(snapshot.next)
        if gate is None:
            return None
        values = snapshot.values
        if gate is GateId.ACC_01:
            scorecard = values["acceptance_scorecard"]
            if scorecard is None:
                raise LookupError(f"{eid} 停在 ACC-01 闸门, 但没有承接评分卡")
            _, profile = await self._engagement_and_profile(eid)
            material = AcceptanceMaterial(
                scorecard=scorecard,
                exceptions=referenced(values["exceptions"], scorecard.exception_ids),
                client_profile=profile,
            )
            return GateView(gate=gate, target_id=scorecard.id, target_version=scorecard.version, material=material)
        if gate is GateId.PLN_01_03:
            analytics, candidates = values["analytics"], values["materiality_candidates"]
            if analytics is None or candidates is None:
                raise LookupError(f"{eid} 停在 PLN-01/03 闸门, 但缺初步分析结果或重要性候选")
            planning = PlanningMaterial(
                analytics=analytics,
                analytics_exceptions=referenced(values["exceptions"], analytics.exception_ids),
                candidates=candidates,
                objectives=values["audit_objectives"],
                risks=values["risk_register"],
                uncovered_objective_ids=uncovered_objectives(self.deps.library, values["audit_objectives"]),
            )
            return GateView(gate=gate, target_id=candidates.id, target_version=candidates.version, material=planning)
        if gate is GateId.FIN_01:
            schedule = values["uncorrected_schedule"]
            if schedule is None:
                raise LookupError(f"{eid} 停在 FIN-01 闸门, 但没有未更正错报汇总表")
            completion = CompletionMaterial(schedule=schedule, misstatements=values["misstatements"])
            return GateView(gate=gate, target_id=schedule.id, target_version=schedule.version, material=completion)
        reports = values["report_versions"]
        if not reports:
            raise LookupError(f"{eid} 停在 RPT-01 闸门, 但没有报告")
        report = max(reports, key=lambda r: r.revision)  # 当前报告: 修订轮次最大的一版
        rejected = [d for d in values["gate_decisions"] if d.gate is GateId.RPT_01 and d.decision is GateVerdict.reject]
        reporting = ReportMaterial(report=report, previous_decisions=rejected)
        return GateView(gate=gate, target_id=report.id, target_version=report.version, material=reporting)


def referenced(exceptions: list[AuditException], ids: list[str]) -> list[AuditException]:
    """状态中的例外含历次执行的 (upsert_by_id 只增不删), 只取材料引用的那些, 按引用顺序."""
    by_id = {x.id: x for x in exceptions}
    return [by_id[i] for i in ids if i in by_id]
