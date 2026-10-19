"""A10: RPT-03. 返回字段: report_versions, review_tasks, procedure_runs."""

from datetime import UTC, date, datetime
from typing import Any, ClassVar, cast

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.llm_outputs import ReportDraft
from backend.contracts.models import (
    AgentId,
    AuditReportVersion,
    GateId,
    GateVerdict,
    Judgment,
    JudgmentRef,
    OpinionType,
    ReportSection,
    ReviewStatus,
)
from backend.contracts.services import ValidationContext
from backend.contracts.state import StatePatch
from backend.services.judgment_engine import citation_keys
from backend.services.staleness import current_results


def report_date_text(day: date) -> str:
    """报告中的日期写法 (RPT-03 提示词: Australian English, 如 "30 June 2026")."""
    return f"{day.day} {day:%B %Y}"


class ConciergeAgent(ProcedureAgent[AuditReportVersion]):
    """A10: 执行 RPT-03, 生成审计报告初稿, RPT-01 退回后按批注修订.

    模型只写意见段与形成意见的基础段, 其余段落取 templates/report.yaml 中该意见类型的固定文本, 代入占位符.
    """

    agent_id: ClassVar[AgentId] = AgentId.A10  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"RPT-03"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> AuditReportVersion:
        """起草下一修订 REPORT-{revision} (revision 为状态中最大修订号 + 1), 草稿第 1 版; 修订时带上一稿与退回批注.

        意见类型取 FIN-01 选定的 opinion_route; 模型输入见 RPT-03.md.j2 头注释, fixture_key 为 r{revision}.
        模型按提示词列出的引用键引用判断 (judgment_keys), 校验只认这些键 (当前结果中错报所依据的已确认判断, 过期重跑
        取代的不算), 通过后换成当前判断的 ID 与版本记进报告的 judgment_refs. 校验不通过时抛 ValueError (含最后一次
        的错误), 由 A1 按执行失败处理.
        """
        state = ctx.state
        opinion = state["opinion_route"]
        if opinion is None:
            raise ValueError("RPT-03 需要 FIN-01 选定的意见类型 (opinion_route)")
        revision = max((r.revision for r in state["report_versions"]), default=0) + 1
        previous, annotations, addresses = self.revision_inputs(ctx, revision)
        engagement = ctx.engagement
        version = state["client_profile_version"]
        profile = await self.deps.engagement_store.client_profile(engagement.client_id, version)
        if profile is None:
            raise LookupError(f"client profile {engagement.client_id} v{version} not found")
        owner = await self.deps.users.get(engagement.owner_id)
        if owner is None:
            raise LookupError(f"项目负责人 {engagement.owner_id} 不存在, 报告签名缺审计师姓名")
        variables = {
            "client_name": profile.legal_name,
            "task": {"params": ctx.run.model_dump(mode="json")["params_snapshot"]},
            "revision": revision,
            "opinion_type": opinion.value,
            "fin01_reason": self.fin01_reason(ctx),
            "previous": previous,
            "annotations": annotations,
            "standards": self.deps.judgments.standards(ctx.template),
            **self.misstatement_inputs(ctx),
        }
        citable = self.citable(ctx)
        result = await self.deps.judgments.call_llm(
            engagement.id,
            ctx.template,
            ctx.run,
            variables,
            # 引用只认提示词列出的键: 当前结果的判断 (过期重跑取代的不算, 同汇总表与 FIN-01 材料)
            ValidationContext(judgments=citable, opinion=opinion),
            self.agent_id,
            f"r{revision}",
        )
        report = result.report
        if report.level_failed is not None:
            raise ValueError(f"RPT-03 第 {revision} 稿未通过 {report.level_failed} 校验: {'; '.join(report.errors)}")
        draft = cast(ReportDraft, report.output)
        keys = citation_keys(citable)
        refs = [JudgmentRef(id=keys[key].id, version=keys[key].version) for key in draft.judgment_keys]
        values = {
            "entity_name": profile.legal_name,
            "period_end": report_date_text(engagement.period_end),
            "auditor_name": owner.full_name,
        }
        fixed = {section: text.format(**values) for section, text in self.deps.library.report_sections(opinion).items()}
        return AuditReportVersion(
            id=f"REPORT-{revision}",
            revision=revision,
            opinion_type=opinion,
            sections={
                ReportSection.opinion: draft.opinion,
                ReportSection.basis_for_opinion: draft.basis_for_opinion,
                **fixed,
            },
            judgment_refs=refs,
            addresses_decision_id=addresses,
        )

    @staticmethod
    def revision_inputs(
        ctx: RunContext, revision: int
    ) -> tuple[dict[str, str] | None, list[dict[str, Any]], str | None]:
        """修订的输入: (上一稿的意见段与基础段, 上一稿最近一次被 RPT-01 退回的批注, 该退回决定 ID);
        首稿为 (None, [], None).

        上一稿取状态中修订号为 revision - 1 的报告的当前版本; 退回决定须针对上一稿 (target_id), 信息不足的决定不算退回.
        不是退回引起的重拟 (需求 R274: 上一稿之后有过期重跑, 请求的 report_revision 为上一稿的修订号, 重跑完重新经过
        FIN-01) 时为 (上一稿的两段, [], None). 修订时找不到上一稿, 或既没有针对它的退回决定也没有这样的重跑请求,
        抛 LookupError.
        """
        if revision == 1:
            return None, [], None
        state = ctx.state
        earlier = [r for r in state["report_versions"] if r.revision == revision - 1]
        if not earlier:
            raise LookupError(f"RPT-03 第 {revision} 稿找不到第 {revision - 1} 稿")
        last = max(earlier, key=lambda r: r.version)
        rejects = [
            d
            for d in state["gate_decisions"]
            if d.gate is GateId.RPT_01 and d.decision is GateVerdict.reject and d.target_id == last.id
        ]
        previous = {
            "opinion": last.sections[ReportSection.opinion],
            "basis_for_opinion": last.sections[ReportSection.basis_for_opinion],
        }
        if rejects:
            reject = rejects[-1]
            return previous, [a.model_dump(mode="json") for a in reject.annotations], reject.id
        if any(r.report_revision == last.revision for r in state["rerun_requests"]):
            return previous, [], None  # 重跑后重拟: 没有批注, 不针对哪个退回决定
        raise LookupError(f"RPT-03 第 {revision} 稿找不到 RPT-01 对 {last.id} 的退回决定, 也没有其后的过期重跑")

    @staticmethod
    def fin01_reason(ctx: RunContext) -> str:
        """最后一条 FIN-01 通过决定的理由 (FIN-01 理由必填)."""
        approvals = [
            d for d in ctx.state["gate_decisions"] if d.gate is GateId.FIN_01 and d.decision is GateVerdict.approve
        ]
        if not approvals:
            raise LookupError("RPT-03 需要 FIN-01 的通过决定")
        return approvals[-1].reason or ""

    @staticmethod
    def citable(ctx: RunContext) -> list[Judgment]:
        """可引用的判断: 汇总表中各错报 (按表中顺序) 所依据的已确认判断, 取当前结果 (过期重跑取代的不算) 中的当前
        版本, 去重. 无保留意见时为空 (报告不引用判断)."""
        state = ctx.state
        schedule = state["uncorrected_schedule"]
        if schedule is None or state["opinion_route"] is not OpinionType.qualified:
            return []
        misstatements = {m.id: m for m in state["misstatements"]}
        judgments = {j.id: j for j in current_results(state).judgments}
        found: dict[str, Judgment] = {}
        for item in schedule.items:
            source = misstatements[item.misstatement_id].judgment_id
            judgment = judgments.get(source) if source is not None else None
            if judgment is not None and judgment.review_status is ReviewStatus.confirmed:
                found.setdefault(judgment.id, judgment)
        return list(found.values())

    @classmethod
    def misstatement_inputs(cls, ctx: RunContext) -> dict[str, Any]:
        """保留意见的输入: 汇总表, 表中各错报 (按表中顺序) 与可引用的判断 (citable, 各带引用键 key).

        无保留意见时汇总表照给, 错报与判断为空 (提示词只在保留意见时列出).
        """
        state = ctx.state
        schedule = state["uncorrected_schedule"]
        if schedule is None:
            raise LookupError("RPT-03 需要未更正错报汇总表 (FIN-04)")
        if state["opinion_route"] is not OpinionType.qualified:
            return {"schedule": schedule.model_dump(mode="json"), "misstatements": [], "judgments": []}
        misstatements = {m.id: m for m in state["misstatements"]}
        listed = [misstatements[item.misstatement_id] for item in schedule.items]
        include = {"id", "version", "procedure_id", "subject_id", "conclusion", "evidence"}
        return {
            "schedule": schedule.model_dump(mode="json"),
            "misstatements": [m.model_dump(mode="json") for m in listed],
            "judgments": [
                {"key": key, **j.model_dump(mode="json", include=include)}
                for key, j in citation_keys(cls.citable(ctx)).items()
            ],
        }

    async def interpret(self, ctx: RunContext, findings: AuditReportVersion) -> StatePatch:
        """报告草稿交节点写入, 不建任务 (由 RPT-01 闸门审阅), 执行记录置成功终态."""
        return {
            "report_versions": [findings],
            "review_tasks": [],
            "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
        }
