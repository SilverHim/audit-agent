"""A10 的 RPT-03: 起草审计报告初稿, RPT-01 退回后按批注修订. 返回字段: report_versions, review_tasks, procedure_runs.

模型只写意见段与形成意见的基础段, 其余段落取 templates/report.yaml 中该语言 (执行记录的 language, 需求 R341), 该意见
类型的固定文本, 代入占位符; 两段的标题与日期写法同取这一份. 由
ConciergeAgent 按程序编号分派到 draft (执行) 与 report_patch (解读), 见 agents/concierge/agent.py.
"""

from datetime import UTC, date, datetime
from typing import Any, cast

from backend.agents.base import AgentDeps, RunContext, finish_run
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

DATE_EXAMPLE = date(2025, 7, 1)  # 提示词里日期写法的示例 (按报告语言写出, 如 1 July 2025)


async def draft(deps: AgentDeps, ctx: RunContext) -> AuditReportVersion:
    """RPT-03 的执行: 起草下一修订 REPORT-{revision} (revision 为状态中最大修订号 + 1), 草稿第 1 版; 修订时带上一稿
    与退回批注.

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
    previous, annotations, addresses = revision_inputs(ctx, revision)
    engagement = ctx.engagement
    version = state["client_profile_version"]
    profile = await deps.engagement_store.client_profile(engagement.client_id, version)
    if profile is None:
        raise LookupError(f"client profile {engagement.client_id} v{version} not found")
    owner = await deps.users.get(engagement.owner_id)
    if owner is None:
        raise LookupError(f"项目负责人 {engagement.owner_id} 不存在, 报告签名缺审计师姓名")
    language = ctx.run.language  # 报告的语言: 执行记录的 (需求 R341), 不读项目当前的值
    library = deps.library
    opinion_heading, basis_heading = library.report_headings(language)[opinion]
    templates = library.report_sections(opinion, language)
    variables = {
        "client_name": profile.legal_name,
        "task": {"params": ctx.run.model_dump(mode="json")["params_snapshot"]},
        "revision": revision,
        "opinion_type": opinion.value,
        "fin01_reason": fin01_reason(ctx),
        "previous": previous,
        "annotations": annotations,
        "standards": deps.judgments.standards(ctx.template),
        "headings": {"opinion": opinion_heading, "basis_for_opinion": basis_heading},
        # 审计师责任段的标题 (固定段落首行), 基础段里要指向它; 日期写法的示例
        "responsibilities_heading": templates[ReportSection.auditor_responsibilities].splitlines()[0],
        "date_example": library.report_date(DATE_EXAMPLE, language),
        **misstatement_inputs(ctx),
    }
    cited = citable(ctx)
    result = await deps.judgments.call_llm(
        engagement.id,
        ctx.template,
        ctx.run,
        variables,
        # 引用只认提示词列出的键: 当前结果的判断 (过期重跑取代的不算, 同汇总表与 FIN-01 材料)
        ValidationContext(judgments=cited, opinion=opinion, standards_of=None),
        AgentId.A10,
        f"r{revision}",
    )
    report = result.report
    if report.level_failed is not None:
        raise ValueError(f"RPT-03 第 {revision} 稿未通过 {report.level_failed} 校验: {'; '.join(report.errors)}")
    output = cast(ReportDraft, report.output)
    keys = citation_keys(cited)
    refs = [JudgmentRef(id=keys[key].id, version=keys[key].version) for key in output.judgment_keys]
    values = {
        "entity_name": profile.legal_name,
        "period_end": library.report_date(engagement.period_end, language),
        "auditor_name": owner.full_name,
    }
    fixed = {section: text.format(**values) for section, text in templates.items()}
    return AuditReportVersion(
        id=f"REPORT-{revision}",
        revision=revision,
        opinion_type=opinion,
        sections={
            ReportSection.opinion: output.opinion,
            ReportSection.basis_for_opinion: output.basis_for_opinion,
            **fixed,
        },
        judgment_refs=refs,
        addresses_decision_id=addresses,
        run_id=ctx.run.id,
    )


def revision_inputs(ctx: RunContext, revision: int) -> tuple[dict[str, str] | None, list[dict[str, Any]], str | None]:
    """修订的输入: (上一稿的意见段与基础段, 上一稿最近一次被 RPT-01 退回的批注, 该退回决定 ID); 首稿为 (None, [], None).

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


def fin01_reason(ctx: RunContext) -> str:
    """最后一条 FIN-01 通过决定的理由 (FIN-01 理由必填)."""
    approvals = [
        d for d in ctx.state["gate_decisions"] if d.gate is GateId.FIN_01 and d.decision is GateVerdict.approve
    ]
    if not approvals:
        raise LookupError("RPT-03 需要 FIN-01 的通过决定")
    return approvals[-1].reason or ""


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


# 提示词里错报的字段 (需求 R156 第 1 轮审核 R156-01): 同错报生命周期之前, 即 R19 的字段加 status, 功能 misstatement
# 关闭时提示词逐字不变; 生命周期的新字段 (科目代码, 分类, 沟通, 更正) 不进提示词, 要带时由接入按状态累计的需求按功能加
MISSTATEMENT_FIELDS = {
    "id",
    "version",
    "judgment_id",
    "exception_id",
    "review_record_id",
    "type",
    "account",
    "assertion",
    "amount_cents",
    "direction",
    "status",
}


def misstatement_inputs(ctx: RunContext) -> dict[str, Any]:
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
        "misstatements": [m.model_dump(mode="json", include=MISSTATEMENT_FIELDS) for m in listed],
        "judgments": [
            {"key": key, **j.model_dump(mode="json", include=include)} for key, j in citation_keys(citable(ctx)).items()
        ],
    }


def report_patch(ctx: RunContext, report: AuditReportVersion) -> StatePatch:
    """RPT-03 的解读: 报告草稿交节点写入, 不建任务 (由 RPT-01 闸门审阅), 执行记录置成功终态."""
    return {
        "report_versions": [report],
        "review_tasks": [],
        "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
    }
