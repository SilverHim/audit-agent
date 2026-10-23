"""决定、计划与报告底稿的材料与历史, 金额计算复用闸门口径."""

from typing import TYPE_CHECKING

from backend.contracts.models import (
    AcceptanceScorecard,
    AuditPlan,
    AuditReportVersion,
    LocalizedText,
    MaterialityCandidates,
    ReportSection,
    RiskItem,
    Workpaper,
    WorkpaperColumn,
    WorkpaperSection,
    WorkpaperTable,
)
from backend.services.currency import format_money
from backend.services.materiality import materiality_amounts
from backend.services.workpaper_labels import ASSERTIONS, NOT_AVAILABLE, REGISTRY_CHECKS, field_label, words
from backend.services.workpaper_tables import column, section, value

if TYPE_CHECKING:
    from backend.services.workpapers import Sources, View

BASES = {
    "revenue": words("收入", "Revenue"),
    "gross_profit": words("毛利", "Gross profit"),
    "total_expenses": words("总费用", "Total expenses"),
    "net_assets": words("净资产", "Net assets"),
    "total_assets": words("总资产", "Total assets"),
    "profit_before_tax": words("税前利润", "Profit before tax"),
}
VERDICTS = {
    "approve": words("通过", "Approved"),
    "reject": words("驳回", "Rejected"),
    "insufficient_info": words("信息不足", "Insufficient information"),
    "rerun": words("重跑", "Rerun"),
}
LEVELS = {
    "low": words("低", "Low"),
    "medium": words("中", "Medium"),
    "high": words("高", "High"),
    "significant": words("特别风险", "Significant risk"),
}
REASONS_MISSING = words("缺: 决定时未填写理由", "Missing: no reason recorded at the decision")


def decision_draft(view: "View") -> LocalizedText | None:
    if not view.decisions:
        return None
    decision = view.decisions[-1]
    verdict = VERDICTS[decision.decision]
    return words(
        f"决定 {decision.id}: {verdict.zh}. {decision.reason or '决定时未填写理由.'}",
        f"Decision {decision.id}: {verdict.en}. {decision.reason or 'No reason recorded at the decision.'}",
    )


def percentage(key: str) -> WorkpaperColumn:
    return column(key).model_copy(update={"format": "percent"})


def decision_table(view: "View", sources: "Sources") -> WorkpaperTable:
    return WorkpaperTable(
        id="decision_history",
        columns=[
            column("id", width=30),
            column("gate"),
            column("material", width=35),
            column("decision"),
            column("decided_by"),
            column("decided_at", width=28),
            column("reason", width=75),
        ],
        rows=[
            {
                "id": d.id,
                "gate": d.gate,
                "material": f"{d.target_id} v{d.target_version}",
                "decision": VERDICTS[d.decision],
                "decided_by": sources.people.get(d.decided_by, d.decided_by),
                "decided_at": d.decided_at.isoformat(),
                "reason": d.reason or REASONS_MISSING,
            }
            for d in view.decisions
        ],
    )


def materiality_tables(view: "View", sources: "Sources", paper: Workpaper) -> list[WorkpaperTable]:
    candidates = [o for o in view.outputs if isinstance(o, MaterialityCandidates)]
    candidate = max(candidates, key=lambda c: c.version, default=None)
    rows = []
    for c in candidate.candidates if candidate else []:
        rows.append(
            {
                "basis": BASES[c.basis],
                "base_amount_cents": c.base_amount_cents,
                "pct_low": c.pct_low,
                "pct_high": c.pct_high,
                "low_cents": c.low_cents,
                "high_cents": c.high_cents,
                "status": words(
                    "本期不适用: 税前亏损" if c.not_applicable == "loss" else "本期不适用: 基准金额不为正",
                    "Not applicable: loss before tax"
                    if c.not_applicable == "loss"
                    else "Not applicable: non-positive benchmark",
                )
                if c.not_applicable
                else words("可选", "Available"),
            }
        )
    choices = []
    for decision in view.decisions:
        if decision.materiality is None:
            continue
        target = sources.materials.get((decision.target_id, decision.target_version))
        if not isinstance(target, MaterialityCandidates):
            # 旧项目没有所选版本的候选时不使用新的候选倒算旧决定.
            continue
        overall, performance, trivial = materiality_amounts(target, decision.materiality)
        choice = decision.materiality
        base = next(c.base_amount_cents for c in target.candidates if c.basis == choice.basis)
        reason = decision.reason or next(
            (n.text for n in reversed(paper.notes) if n.item == "materiality_factors"), None
        )
        choices.append(
            {
                "id": decision.id,
                "basis": BASES[choice.basis],
                "base_amount_cents": base,
                "pct": choice.pct,
                "overall_cents": overall,
                "performance_pct": choice.performance_pct,
                "performance_cents": performance,
                "trivial_pct": choice.clearly_trivial_pct,
                "trivial_cents": trivial,
                "reason": reason or REASONS_MISSING,
                "specific": words("不适用", "Not applicable"),
            }
        )
    return [
        WorkpaperTable(
            id="materiality_candidates",
            columns=[
                column("basis"),
                column("base_amount_cents", money=True),
                percentage("pct_low"),
                percentage("pct_high"),
                column("low_cents", money=True),
                column("high_cents", money=True),
                column("status"),
            ],
            rows=rows,
        ),
        WorkpaperTable(
            id="materiality_calculation",
            columns=[
                column("id"),
                column("basis"),
                column("base_amount_cents", money=True),
                percentage("pct"),
                column("overall_cents", money=True),
                percentage("performance_pct"),
                column("performance_cents", money=True),
                percentage("trivial_pct"),
                column("trivial_cents", money=True),
                column("specific"),
                column("reason", width=85),
            ],
            rows=choices,
        ),
    ]


def risk_tables(view: "View", sources: "Sources") -> list[WorkpaperTable]:
    risks = WorkpaperTable(
        id="risk_register",
        columns=[
            column("id"),
            column("objective_id"),
            column("assertion"),
            column("risk"),
            column("fraud_related"),
            column("description", width=85),
            column("reason", width=70),
        ],
        rows=[
            {
                "id": r.id,
                "objective_id": r.objective_id,
                "assertion": ASSERTIONS[r.assertion],
                "risk": LEVELS[r.level],
                "fraud_related": r.fraud_related,
                "description": r.description,
                "reason": r.revision_reason,
            }
            for r in sources.risks
        ],
    )
    changes = []
    for decision in view.decisions:
        for edit in decision.edits:
            prior = sources.materials.get((edit.target_id, edit.target_version))
            changes.append(
                {
                    "id": decision.id,
                    "source_id": edit.target_id,
                    "before": LEVELS[prior.level] if isinstance(prior, RiskItem) else NOT_AVAILABLE,
                    "after": LEVELS[edit.new_value],
                    "reason": edit.reason or NOT_AVAILABLE,
                }
            )
    return [
        risks,
        WorkpaperTable(
            id="risk_changes",
            columns=[column("id"), column("source_id"), column("before"), column("after"), column("reason", width=80)],
            rows=changes,
        ),
    ]


def plan_metadata(plan: AuditPlan, currency: str) -> WorkpaperTable:
    fields = (
        "revision",
        "status",
        "applicable_framework",
        "materiality_choice",
        "materiality_cents",
        "performance_materiality_cents",
        "clearly_trivial_cents",
        "significant_risk_objective_ids",
        "eqr_recommendation",
        "expert_assessment",
        "standards_cited",
        "eqr_required",
        "eqr_basis",
        "auditor_expert_not_required",
        "approved_by",
        "approved_at",
        "revision_reason",
    )
    data = plan.model_dump(mode="json")
    return WorkpaperTable(
        id="plan_material",
        columns=[column("field", width=35), column("value", width=110)],
        rows=[
            {
                "field": field_label(key),
                "value": format_money(data[key], currency) if key.endswith("_cents") else value(data[key]),
            }
            for key in fields
        ],
    )


def plan_sections(view: "View") -> list[WorkpaperSection]:
    result = []
    for plan in sorted((o for o in view.outputs if isinstance(o, AuditPlan)), key=lambda p: (p.revision, p.version)):
        language = next((r.language for r in view.runs if r.id == plan.run_id), "en")
        result.append(
            WorkpaperSection(
                id=f"plan_{plan.id}_{plan.version}",
                title=words(f"计划修订 {plan.revision}", f"Plan revision {plan.revision}"),
                generated_language=language,
                paragraphs=[
                    words(
                        f"状态: {plan.status}. 修订理由: {plan.revision_reason or '—'}",
                        f"Status: {plan.status}. Revision reason: {plan.revision_reason or '—'}",
                    ),
                    *[part.text for part in plan.narrative],
                ],
                tables=[
                    plan_metadata(plan, view.currency),
                    WorkpaperTable(
                        id="plan_procedures",
                        columns=[column("id"), column("description", width=90), column("parameters", width=50)],
                        rows=[
                            {
                                "id": item.procedure_id,
                                "description": next(
                                    (
                                        n.nature_timing_extent
                                        for n in plan.procedure_notes
                                        if n.procedure_id == item.procedure_id
                                    ),
                                    None,
                                ),
                                "parameters": value(item.params),
                            }
                            for item in plan.items
                        ],
                    ),
                    WorkpaperTable(
                        id="plan_timetable",
                        columns=[column("milestone", width=85), column("due")],
                        rows=[{"milestone": m.milestone, "due": m.due.isoformat()} for m in plan.timetable],
                    ),
                ],
            )
        )
    return result


def report_sections(view: "View") -> list[WorkpaperSection]:
    return [
        WorkpaperSection(
            id=f"report_{report.id}_{report.version}",
            title=words(f"报告修订 {report.revision}", f"Report revision {report.revision}"),
            generated_language=next((r.language for r in view.runs if r.id == report.run_id), None),
            paragraphs=[
                words(
                    f"意见: {report.opinion_type}. 状态: {report.status}",
                    f"Opinion: {report.opinion_type}. Status: {report.status}",
                ),
                *[report.sections[name] for name in ReportSection if name in report.sections],
            ],
            tables=[
                WorkpaperTable(
                    id="report_signing",
                    columns=[column("decided_by"), column("decided_at"), column("report_date")],
                    rows=[
                        {
                            "decided_by": report.approved_by,
                            "decided_at": report.approved_at.isoformat() if report.approved_at else None,
                            "report_date": str(report.report_date) if report.report_date else None,
                        }
                    ],
                )
            ],
        )
        for report in sorted(
            (o for o in view.outputs if isinstance(o, AuditReportVersion)), key=lambda r: (r.revision, r.version)
        )
    ]


def decision_sections(view: "View", sources: "Sources", paper: Workpaper) -> WorkpaperSection | None:
    pid = view.template.id
    if pid not in {"PLN-01", "PLN-03", "PLN-15", "ACC-01", "FIN-01", "RPT-03", "RPT-01"}:
        return None
    tables = [decision_table(view, sources)]
    children = []
    if pid == "PLN-01":
        tables = [*materiality_tables(view, sources, paper), *tables]
    elif pid == "PLN-03":
        tables = [*risk_tables(view, sources), *tables]
    elif pid == "PLN-15":
        children = plan_sections(view)
    elif pid in {"RPT-03", "RPT-01"}:
        children = report_sections(view)
    elif pid == "ACC-01":
        cards = {(o.id, o.version): o for o in view.outputs if isinstance(o, AcceptanceScorecard)}
        tables.insert(
            0,
            WorkpaperTable(
                id="acceptance",
                columns=[
                    column("material"),
                    column("check"),
                    column("status"),
                    column("description", width=85),
                    column("evidence", width=60),
                ],
                rows=[
                    {
                        "material": f"{card.id} v{card.version}",
                        "check": REGISTRY_CHECKS[check.check],
                        "status": check.passed,
                        "description": check.detail,
                        "evidence": f"{check.evidence.source_id} / {check.evidence.locator}",
                    }
                    for card in cards.values()
                    for check in card.registry_checks
                ],
            ),
        )
    elif pid == "FIN-01":
        from backend.contracts.models import UncorrectedSchedule

        tables.insert(
            0,
            WorkpaperTable(
                id="completion_material",
                columns=[
                    column("id"),
                    column("total_cents", money=True),
                    column("overall_cents", money=True),
                    column("opinion"),
                ],
                rows=[
                    {
                        "id": o.id,
                        "total_cents": o.total_cents,
                        "overall_cents": o.materiality_cents,
                        "opinion": o.candidate_opinion,
                    }
                    for o in view.outputs
                    if isinstance(o, UncorrectedSchedule)
                ],
            ),
        )
    return section("details", tables=tables).model_copy(update={"children": children})
