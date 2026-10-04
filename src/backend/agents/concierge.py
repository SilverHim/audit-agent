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
    OpinionType,
    ReportSection,
    ReviewStatus,
)
from backend.contracts.services import ValidationContext
from backend.contracts.state import StatePatch


def report_date_text(day: date) -> str:
    """报告中的日期写法 (RPT-03 提示词: Australian English, 如 "30 June 2026")."""
    return f"{day.day} {day:%B %Y}"


class ConciergeAgent(ProcedureAgent[AuditReportVersion]):
    """A10: 执行 RPT-03, 生成审计报告初稿 (按批注修订尚未接入).

    模型只写意见段与形成意见的基础段, 其余段落取 templates/report.yaml 中该意见类型的固定文本, 代入占位符.
    """

    agent_id: ClassVar[AgentId] = AgentId.A10  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"RPT-03"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> AuditReportVersion:
        """起草下一修订 REPORT-{revision} (revision 为状态中最大修订号 + 1), 草稿第 1 版.

        意见类型取 FIN-01 选定的 opinion_route; 模型输入见 RPT-03.md.j2 头注释, fixture_key 为 r{revision}.
        校验不通过时抛 ValueError (含最后一次的错误), 由 A1 按执行失败处理.
        """
        state = ctx.state
        opinion = state["opinion_route"]
        if opinion is None:
            raise ValueError("RPT-03 需要 FIN-01 选定的意见类型 (opinion_route)")
        revision = max((r.revision for r in state["report_versions"]), default=0) + 1
        if revision > 1:
            raise NotImplementedError("RPT-03 按 RPT-01 批注修订")
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
            "previous": None,
            "annotations": [],
            "standards": self.deps.judgments.standards(ctx.template),
            **self.misstatement_inputs(ctx),
        }
        result = await self.deps.judgments.call_llm(
            engagement.id,
            ctx.template,
            ctx.run,
            variables,
            ValidationContext(judgments=state["judgments"], opinion=opinion),
            self.agent_id,
            f"r{revision}",
        )
        report = result.report
        if report.level_failed is not None:
            raise ValueError(f"RPT-03 第 {revision} 稿未通过 {report.level_failed} 校验: {'; '.join(report.errors)}")
        draft = cast(ReportDraft, report.output)
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
            judgment_refs=draft.judgment_refs,
        )

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
    def misstatement_inputs(ctx: RunContext) -> dict[str, Any]:
        """保留意见的输入: 汇总表, 表中各错报 (按表中顺序) 与其来源的已确认判断 (各取状态中的当前版本).

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
        judgments = {j.id: j for j in state["judgments"]}
        sources = [
            judgments[m.judgment_id]
            for m in listed
            if m.judgment_id is not None
            and m.judgment_id in judgments
            and judgments[m.judgment_id].review_status is ReviewStatus.confirmed
        ]
        return {
            "schedule": schedule.model_dump(mode="json"),
            "misstatements": [m.model_dump(mode="json") for m in listed],
            "judgments": [
                j.model_dump(
                    mode="json", include={"id", "version", "procedure_id", "subject_id", "conclusion", "evidence"}
                )
                for j in sources
            ],
        }

    async def interpret(self, ctx: RunContext, findings: AuditReportVersion) -> StatePatch:
        """报告草稿交节点写入, 不建任务 (由 RPT-01 闸门审阅), 执行记录置成功终态."""
        return {
            "report_versions": [findings],
            "review_tasks": [],
            "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
        }
