"""从一次读取的资料生成语言无关的底稿快照. 纯函数, 不查库、不调用模型、不渲染 Excel."""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from backend.contracts.models import (
    AcceptanceScorecard,
    AuditException,
    LocalizedText,
    PopulationItem,
    RulePopulation,
    Workpaper,
    WorkpaperColumn,
    WorkpaperContent,
    WorkpaperHeader,
    WorkpaperTable,
    WorkpaperValue,
)
from backend.services.procedure_library import render_text
from backend.services.workpaper_decisions import decision_sections
from backend.services.workpaper_judgments import detail_sections
from backend.services.workpaper_labels import (
    AGENTS,
    ASSERTIONS,
    DISPOSITIONS,
    NOT_AVAILABLE,
    NOT_RECORDED,
    PROCEDURE_NAMES,
    REGISTRY_CHECKS,
    ROW_CHECKS,
    words,
)
from backend.services.workpaper_tables import column, section, value

if TYPE_CHECKING:
    from backend.services.workpapers import Sources, View

# Xero 原始录入/审批人员不复制进快照; 服务完成记录中的合成签收人照原文保留.
PRIVATE_FIELDS = frozenset({"created_by", "approved_by", "posted_by", "email"})


def marker(index: int) -> str:
    """例外脚注的字母 (a..z, aa..), 与规则内部代号分开."""
    text = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        text = chr(97 + remainder) + text
    return text


def matches(exception: AuditException, population: RulePopulation, item: PopulationItem) -> bool:
    """例外与被测项目的对应: 同源 ID, 或 GEN-05 发票检查所记的 invoice_id."""
    return (item.source_id in exception.source_ids) or (
        population.source_type == "sales_invoice" and exception.detail.get("invoice_id") == item.source_id
    )


def checks_table(view: "View") -> WorkpaperTable:
    specs = {rule.id: rule for rule in view.template.rules}
    rows = []
    for run in view.current:
        for population in (p for p in view.populations if p.run_id == run.id):
            for rule in population.rules:
                spec = specs[rule.rule_id]
                rows.append(
                    {
                        "check": spec.check
                        or words(spec.name, ROW_CHECKS.get((view.template.id, rule.rule_id), spec.name)),
                        "scope": render_text(spec.population.description, run.params_snapshot)
                        if spec.population
                        else (render_text(view.template.scope, run.params_snapshot) if view.template.scope else None),
                        "count": rule.count,
                        "amount_cents": rule.amount_cents,
                    }
                )
    return WorkpaperTable(
        id="checks",
        columns=[
            column("check", width=28),
            column("scope", width=65),
            column("count"),
            column("amount_cents", money=True),
        ],
        rows=rows,
    )


def rule_details(view: "View") -> tuple[WorkpaperTable, list[LocalizedText]]:
    """总体按 (对象类型, ID) 合并, 保留各规则的识别字段与独立检查结果; 总体外的对象写 —, 不冒充检查通过."""
    specs = {rule.id: rule for rule in view.template.rules}
    grouped: dict[tuple[str | None, str], dict[str, WorkpaperValue]] = {}
    order: list[str] = []
    footnotes: dict[tuple[str | None, str], list[str]] = {}
    missing: list[LocalizedText] = []
    exceptions = {x.id: marker(n) for n, x in enumerate(view.exceptions, 1)}
    checks: dict[str, WorkpaperColumn] = {}
    for run in view.current:
        for population in (p for p in view.populations if p.run_id == run.id):
            for rule in population.rules:
                spec = specs[rule.rule_id]
                if rule.count is None:
                    missing.append(
                        words(f"{rule.rule_id}: 被测项目未记录", f"{rule.rule_id}: Items tested not recorded")
                    )
                    continue
                check_key = f"check_{run.id}_{rule.rule_id}"
                checks[check_key] = WorkpaperColumn(
                    key=check_key,
                    label=spec.check or words(spec.name, ROW_CHECKS.get((view.template.id, rule.rule_id), spec.name)),
                    width=24,
                )
                for n, item in enumerate(rule.items):
                    key = (rule.source_type, item.source_id or f"{run.id}:{rule.rule_id}:{n}")
                    row = grouped.setdefault(key, {"source_id": item.source_id, "amount_cents": item.amount_cents})
                    for name, raw in item.fields.items():
                        if name not in PRIVATE_FIELDS:
                            row[name] = value(raw)
                            if name not in order:
                                order.append(name)
                    hits = [
                        x
                        for x in view.exceptions
                        if x.run_id == run.id and x.rule_id == rule.rule_id and matches(x, rule, item)
                    ]
                    row[check_key] = "✗" if hits else "✓"
                    if hits:
                        footnotes.setdefault(key, []).extend(exceptions[x.id] for x in hits)
    for item_key, letters in footnotes.items():
        refs = ", ".join(dict.fromkeys(letters))
        grouped[item_key]["remarks"] = words(f"例外 {refs}", f"Exception {refs}")
    for row in grouped.values():
        for check_key in checks:
            row.setdefault(check_key, "—")
    if view.template.id == "REV-08":
        return three_way_table(view, list(grouped.values()), checks), missing
    keys = [
        "source_id",
        "amount_cents",
        *[key for key in order if key not in {"source_id", "amount_cents"}],
        *checks,
        "remarks",
    ]
    cols = [
        checks[key] if key in checks else column(key, money=key.endswith("_cents"), total=key == "amount_cents")
        for key in keys
    ]
    rows = sorted(
        grouped.values(), key=lambda r: (str(r.get("posting_date", r.get("txn_date", ""))), str(r.get("source_id", "")))
    )
    return WorkpaperTable(id="tested_items", columns=cols, rows=rows), missing


def three_way_table(
    view: "View", rows: list[dict[str, WorkpaperValue]], checks: Mapping[str, WorkpaperColumn]
) -> WorkpaperTable:
    """D-3 的识别特征与三项独立结果. 服务记录多条时按原总体顺序换行, 分录显示行 ID."""
    keys = [
        "invoice_no",
        "customer",
        "contract_id",
        "posting_date",
        "amount_cents",
        "journal_entry_id",
        "delivery_no",
        "signed_date",
        "signed_by",
        "bank_transaction_ids",
        "received_dates",
        "received_cents",
        *checks,
        "remarks",
    ]
    for row in rows:
        records = json.loads(str(row.get("records") or "[]"))
        for key in ("delivery_no", "signed_date", "signed_by"):
            row[key] = "\n".join(str(record.get(key) or "—") for record in records)
        receipt_checks = [key for key in checks if key.endswith("_R03")]
        if receipt_checks and all(row.get(key) == "—" for key in receipt_checks):
            row["remarks"] = words(
                "关注: 未找到收款, 收款金额未作判断", "Attention: no receipt found; receipt amount not assessed"
            )
        row.setdefault("remarks", None)
    cols = [
        checks[key]
        if key in checks
        else column(
            key,
            money=key.endswith("_cents"),
            total=key in {"amount_cents", "received_cents"},
            width=26 if key in {"customer", "remarks"} else 18,
        )
        for key in keys
    ]
    return WorkpaperTable(
        id="tested_items",
        columns=cols,
        rows=sorted(rows, key=lambda r: (str(r.get("posting_date")), str(r.get("invoice_no")))),
    )


def exceptions_table(view: "View") -> WorkpaperTable:
    specs = {rule.id: rule for rule in view.template.rules}
    rows: list[dict[str, WorkpaperValue]] = []
    for n, exception in enumerate(view.exceptions, 1):
        spec = specs.get(exception.rule_id)
        check = spec.check if spec else None
        subject = str(exception.detail.get("invoice_no") or ", ".join(exception.source_ids))
        difference = exception.detail.get("difference_cents", exception.amount_cents)
        rows.append(
            {
                "marker": marker(n),
                "id": exception.id,
                "check": check or words(exception.rule_id, exception.rule_id),
                "source_id": subject,
                "difference_cents": value(difference),
                "status": DISPOSITIONS.get(
                    exception.disposition, words(str(exception.disposition), str(exception.disposition))
                ),
                "judgment": exception.attributed_by,
            }
        )
    return WorkpaperTable(
        id="exceptions",
        columns=[
            column("marker", width=8),
            column("id", width=32),
            column("check", width=30),
            column("source_id", width=25),
            column("difference_cents", money=True),
            column("status"),
            column("judgment", width=32),
        ],
        rows=rows,
    )


def build_content(view: "View", sources: "Sources", workpaper: Workpaper) -> WorkpaperContent:
    """记录当时的表头、程序依据、实际测试明细、例外处理、结论与签署. 不从当前资料重建旧版本."""
    template, engagement = view.template, sources.engagement
    signed = workpaper.signoff
    agent = str(template.owner)
    titles = AGENTS.get(agent, (agent, agent))
    latest = view.runs[-1] if view.runs else None
    decided = view.decisions[-1] if view.decisions else None
    header = WorkpaperHeader(
        title=words(template.name, PROCEDURE_NAMES.get(template.id, template.id)),
        client_name=sources.client_name,
        engagement_id=engagement.id if engagement else "",
        period_start=engagement.period_start if engagement else None,
        period_end=engagement.period_end if engagement else None,
        currency=sources.currency,
        procedure_id=template.id,
        preparer=words(f"{agent} {titles[0]}", f"{agent} {titles[1]}"),
        completed_at=latest.ended_at if latest else (decided.decided_at if decided else None),
        reviewer=sources.people.get(signed.signed_by, signed.signed_by) if signed else None,
        reviewed_at=signed.signed_at if signed else None,
        review_scope=signed.scope if signed else None,
    )
    procedure = next((p for p in sources.procedures if p.id == template.id), None)
    objective_ids = set(procedure.objective_ids if procedure else [])
    objectives = [o for o in sources.objectives if o.id in objective_ids]
    risk_rows = [
        {
            "id": risk.id,
            "description": risk.description,
            "risk": words(
                {"low": "低", "medium": "中", "high": "高", "significant": "特别风险"}[risk.level], risk.level
            ),
        }
        for risk in sources.risks
        if risk.objective_id in objective_ids
    ]
    objective_text = [
        words(f"{o.account}: {ASSERTIONS[o.assertion].zh}", f"{o.account}: {o.assertion}") for o in objectives
    ]
    if not objective_text:
        objective_text = [ASSERTIONS[assertion] for assertion in template.assertions]
    basis = WorkpaperTable(
        id="basis",
        columns=[column("kind"), column("id", width=38), column("version")],
        rows=[{"kind": ref.kind, "id": ref.id, "version": ref.version} for ref in workpaper.basis],
    )
    execution = [
        words(
            f"执行方式: {template.engine}. 完成时间: {header.completed_at.isoformat() if header.completed_at else '—'}",
            f"Nature: {template.engine}. Completed: {header.completed_at.isoformat() if header.completed_at else '—'}",
        )
    ]
    for run in view.current:
        if template.scope:
            execution.append(render_text(template.scope, run.params_snapshot))
    table, missing = rule_details(view)
    if template.id == "ACC-04":
        table = WorkpaperTable(
            id="tested_items",
            columns=[
                column("check", width=30),
                column("status"),
                column("description", width=70),
                column("evidence", width=45),
            ],
            rows=[
                {
                    "check": REGISTRY_CHECKS[check.check],
                    "status": "—" if check.passed is None else ("✓" if check.passed else "✗"),
                    "description": check.detail,
                    "evidence": f"{check.evidence.source_type}:{check.evidence.source_id} / {check.evidence.locator}",
                }
                for card in view.outputs
                if isinstance(card, AcceptanceScorecard)
                for check in card.registry_checks
            ],
        )
    checks = checks_table(view)
    conclusion = workpaper.conclusion or workpaper.conclusion_draft or NOT_AVAILABLE
    notes = [
        words(
            f"补充说明 ({note.added_by}, {note.added_at.isoformat()}): {note.text}",
            f"Supplement ({note.added_by}, {note.added_at.isoformat()}): {note.text}",
        )
        for note in workpaper.notes
    ]
    linked = [
        ref
        for pid, ref in sources.program_refs.items()
        if pid in template.depends_on or (ref.startswith(workpaper.area + "-") and ref != workpaper.ref)
    ]
    if template.id == "REV-03" and "GEN-02" in sources.program_refs:
        linked.append(sources.program_refs["GEN-02"])
    specialized = decision_sections(view, sources, workpaper) or detail_sections(view, sources)
    details = section(
        "details",
        tables=[table],
        paragraphs=missing
        or ([NOT_RECORDED] if template.engine == "rule" and not view.populations and template.rules else []),
    )
    if specialized is not None:
        details = specialized
    return WorkpaperContent(
        header=header,
        sections=[
            section(
                "objectives",
                paragraphs=objective_text,
                tables=[
                    WorkpaperTable(
                        id="risks",
                        columns=[column("id"), column("description", width=60), column("risk")],
                        rows=risk_rows,
                    )
                ],
            ),
            section("basis", tables=[basis]),
            section("procedures", paragraphs=execution),
            section("checks", tables=[checks]),
            details,
            section("exceptions", tables=[exceptions_table(view)]),
            section("conclusion", paragraphs=[conclusion, *notes]),
            section("cross_references", paragraphs=[", ".join(linked)] if linked else []),
            section("review", paragraphs=[signed.opinion if signed and signed.opinion else NOT_AVAILABLE]),
        ],
    )
