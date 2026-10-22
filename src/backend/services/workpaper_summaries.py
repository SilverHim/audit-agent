"""错报汇总、准则对照与总索引. 所有文字与链接随版本冻结."""

from typing import TYPE_CHECKING

from backend.contracts.knowledge import SystemWorkpaper
from backend.contracts.models import (
    GateId,
    MaterialityCandidates,
    Misstatement,
    ObjectRef,
    UncorrectedSchedule,
    Workpaper,
    WorkpaperContent,
    WorkpaperHeader,
    WorkpaperSection,
    WorkpaperTable,
)
from backend.services.materiality import materiality_amounts
from backend.services.staleness import current_misstatements, superseded_runs
from backend.services.workpaper_files import relative_filename
from backend.services.workpaper_labels import ASSERTIONS, CATEGORIES, NOT_AVAILABLE, PROCEDURE_NAMES, SCOPES, words
from backend.services.workpaper_leads import lead_content
from backend.services.workpaper_tables import column, section, unsigned

if TYPE_CHECKING:
    from backend.services.workpapers import Sources, View

STATUSES = {
    key: words(zh, en)
    for key, zh, en in (
        ("draft", "草稿", "Draft"),
        ("signed", "已签署", "Signed"),
        ("needs_rereview", "需重新复核", "Review required"),
        ("final", "最终版", "Final"),
        ("identified", "已认定", "Identified"),
        ("communicated", "已沟通", "Communicated"),
        ("verifying", "待验证更正", "Correction verification pending"),
        ("corrected", "已更正", "Corrected"),
        ("partially_corrected", "部分更正", "Partially corrected"),
        ("uncorrected", "未更正", "Uncorrected"),
        ("evaluated", "已评价", "Evaluated"),
        ("closed", "已结束", "Closed"),
        ("withdrawn", "已撤回", "Withdrawn"),
    )
}


def current_errors(sources: "Sources") -> list[Misstatement]:
    return current_misstatements(
        sources.misstatements, sources.judgments, sources.exceptions, superseded_runs(sources.reruns)
    )


def thresholds(sources: "Sources") -> tuple[int, int, int]:
    approved = [
        d for d in sources.decisions if d.gate is GateId.PLN_01_03 and d.decision == "approve" and d.materiality
    ]
    last = max(approved, key=lambda d: (d.decided_at, d.id), default=None)
    candidate = sources.materials.get((last.target_id, last.target_version)) if last else None
    return (
        materiality_amounts(candidate, last.materiality)
        if last and last.materiality and isinstance(candidate, MaterialityCandidates)
        else (0, 0, 0)
    )


def misstatement_section(view: "View", sources: "Sources") -> WorkpaperSection:
    overall, performance, trivial = thresholds(sources)
    schedules = [s for s in sources.outputs.get("uncorrected_schedule", []) if isinstance(s, UncorrectedSchedule)]
    schedule = max(schedules, key=lambda s: s.version, default=None)
    accumulated = {item.misstatement_id: item.amount_cents for item in schedule.items} if schedule else {}
    reviews = {r.id: r for r in sources.reviews}
    judgments = {j.id: j for j in sources.judgments}
    exceptions = {x.id: x for x in sources.exceptions}
    rows = []
    kept = sorted(current_errors(sources), key=lambda m: m.id)
    for index, item in enumerate(kept, 1):
        source = judgments.get(item.judgment_id or "") or exceptions.get(item.exception_id or "")
        record = reviews.get(item.review_record_id)
        rows.append(
            {
                "ref": f"X-{index}",
                "id": item.id,
                "source_ref": sources.program_refs.get(source.procedure_id, "") if source else "",
                "source_id": item.judgment_id or item.exception_id,
                "account": f"{item.account} ({item.account_code})"
                if item.account_code and f"({item.account_code})" not in item.account
                else item.account,
                "assertion": ASSERTIONS[item.assertion],
                "type": words(
                    {"factual": "事实错报", "judgmental": "判断错报", "projected": "推断错报"}[item.type], item.type
                ),
                "direction": words("高估" if item.direction == "overstated" else "低估", str(item.direction)),
                "amount_cents": item.amount_cents,
                "above_trivial": item.amount_cents > trivial,
                "status": STATUSES[item.status],
                "accumulated_cents": accumulated.get(item.id, 0),
                "decided_by": record.reviewed_by if record else None,
                "decided_at": record.decided_at.isoformat() if record else None,
            }
        )
    table = WorkpaperTable(
        id="misstatements",
        columns=[
            column("ref"),
            column("id", width=35),
            column("source_ref"),
            column("source_id", width=35),
            column("account", width=32),
            column("assertion"),
            column("type"),
            column("direction"),
            column("amount_cents", money=True),
            column("above_trivial"),
            column("status"),
            column("accumulated_cents", money=True),
            column("decided_by"),
            column("decided_at", width=28),
        ],
        rows=rows,
    )
    total = sum(accumulated.get(item.id, 0) for item in kept)
    totals = WorkpaperTable(
        id="misstatement_totals",
        columns=[
            column("total_cents", money=True),
            column("performance_cents", money=True),
            column("overall_cents", money=True),
            column("performance_ratio").model_copy(update={"format": "percent"}),
            column("overall_ratio").model_copy(update={"format": "percent"}),
            column("trivial_cents", money=True),
        ],
        rows=[
            {
                "total_cents": total,
                "performance_cents": performance,
                "overall_cents": overall,
                "performance_ratio": total / performance if performance else None,
                "overall_ratio": total / overall if overall else None,
                "trivial_cents": trivial,
            }
        ],
    )
    last = max(
        (d for d in sources.decisions if d.gate is GateId.FIN_01), key=lambda d: (d.decided_at, d.id), default=None
    )
    return section(
        "details",
        tables=[totals, table],
        paragraphs=[
            last.reason
            if last and last.reason
            else words("结论在完成阶段 (FIN-01) 作出", "The conclusion is reached at completion (FIN-01)")
        ],
    )


def index_table(sources: "Sources", papers: dict[str, Workpaper]) -> WorkpaperTable:
    library = sources.library
    assert library is not None
    templates = {t.workpaper.ref: t for t in library.all(include_disabled=True) if t.workpaper}
    systems = {w.ref: w for w in library.workpapers.system}
    enabled = {t.id for t in library.all()}
    rows = []
    links = {}
    for ref in library.workpaper_refs():
        template, paper = templates.get(ref), papers.get(ref)
        title = words(template.name, PROCEDURE_NAMES.get(template.id, template.id)) if template else systems[ref].name
        pid = template.id if template else None
        status = (
            STATUSES[paper.status]
            if paper
            else words("功能未开启", "Feature disabled")
            if template and pid not in enabled
            else words("尚未执行", "Not yet executed")
        )
        pending = sum(1 for t in sources.tasks if t.procedure_id == pid and t.status == "pending") if pid else 0
        if pending:
            status = words(f"待复核 {pending} 项", f"{pending} pending review")
        header = paper.content.header if paper and paper.content else None
        rows.append(
            {
                "ref": ref,
                "title": title,
                "procedure_id": pid,
                "preparer": header.preparer if header else None,
                "completed_at": header.completed_at.isoformat() if header and header.completed_at else None,
                "status": status,
                "reviewer": header.reviewer if header else None,
                "reviewed_at": header.reviewed_at.isoformat() if header and header.reviewed_at else None,
                "review_scope": SCOPES[header.review_scope] if header and header.review_scope else None,
                "missing_count": len(paper.missing) if paper else None,
            }
        )
        if paper and paper.content:
            links[ref] = words(
                relative_filename(ref, paper.content.header.title, pid, "zh"),
                relative_filename(ref, paper.content.header.title, pid, "en"),
            )
    return WorkpaperTable(
        id="workpaper_index",
        columns=[
            column("ref"),
            column("title", width=35),
            column("procedure_id"),
            column("preparer", width=28),
            column("completed_at", width=28),
            column("status"),
            column("reviewer"),
            column("reviewed_at", width=28),
            column("review_scope"),
            column("missing_count"),
        ],
        rows=rows,
        link_column="ref",
        links=links,
    )


def standards_table(sources: "Sources", papers: dict[str, Workpaper]) -> WorkpaperTable:
    library = sources.library
    assert library is not None
    index = index_table(sources, papers)
    disabled = {row["ref"] for row in index.rows if row["status"] == words("功能未开启", "Feature disabled")}
    rows = []
    for req in library.workpapers.standards:
        refs = library.standard_refs(req)
        found = [papers[ref] for ref in refs if ref in papers]
        if req.not_applicable:
            status = words(f"不适用: {req.not_applicable.zh}", f"Not applicable: {req.not_applicable.en}")
        elif req.standard == "ASA 320" and any("materiality_factors" in paper.missing for paper in found):
            status = words("缺选择理由", "Missing selection rationale")
        elif refs and len(found) == len(refs) and all(not paper.missing for paper in found):
            status = words("完成", "Complete")
        elif found:
            status = words("部分", "Partial")
        elif refs and any(ref in disabled for ref in refs):
            status = words("待补: 功能未开启", "Missing: feature disabled")
        else:
            status = words("待补", "Missing")
        rows.append(
            {
                "standard": req.standard,
                "paragraph": req.paragraphs,
                "description": req.content,
                "source_ref": ", ".join(refs),
                "status": status,
                "remarks": req.note,
            }
        )
    return WorkpaperTable(
        id="standards",
        columns=[
            column("standard"),
            column("paragraph"),
            column("description", width=100),
            column("source_ref", width=40),
            column("status"),
            column("remarks", width=100),
        ],
        rows=rows,
    )


def system_missing(paper: Workpaper) -> list[str]:
    return [] if paper.signoff else ["reviewer", "reviewed_on", "review_scope"]


def system_content(
    spec: SystemWorkpaper, sources: "Sources", papers: dict[str, Workpaper], paper: Workpaper
) -> WorkpaperContent:
    engagement = sources.engagement
    assert engagement is not None and sources.library is not None
    sign = paper.signoff
    latest = max((r.ended_at for r in sources.runs if r.ended_at), default=None)
    header = WorkpaperHeader(
        title=spec.name,
        client_name=sources.client_name,
        engagement_id=engagement.id,
        period_start=engagement.period_start,
        period_end=engagement.period_end,
        currency=sources.currency,
        preparer=words("A8 工作底稿智能体", "A8 Workpaper agent"),
        completed_at=latest,
        reviewer=sources.people.get(sign.signed_by, sign.signed_by) if sign else None,
        reviewed_at=sign.signed_at if sign else None,
        review_scope=sign.scope if sign else None,
    )
    if spec.categories:
        return lead_content(spec, sources, paper, header)
    # 索引自己的行以这一版的状态为准, 不使用旧版签署信息.
    indexed = {
        **papers,
        paper.ref: paper.model_copy(
            update={"content": WorkpaperContent(header=header, sections=[]), "missing": system_missing(paper)}
        ),
    }
    if spec.ref == "S":
        body = section(
            "details",
            tables=[standards_table(sources, indexed)],
            paragraphs=[sources.library.workpapers.standards_note],
        )
    else:
        body = section(
            "details",
            tables=[index_table(sources, indexed)],
            paragraphs=[
                words(
                    "✓ 通过; ✗ 例外; — 不作判断; a, b… 为例外脚注. 原始资料保留原文.",
                    "✓ Agrees; ✗ Exception; — Not assessed; a, b… identify exception footnotes. "
                    "Source documents retain their original text.",
                )
            ],
        )
        covered = {category for system in sources.library.workpapers.system for category in system.categories}
        amounts = {r.account_code: r.balance_cents for r in sources.trial_balance if r.period == "current"}
        body = body.model_copy(
            update={
                "tables": [
                    *body.tables,
                    WorkpaperTable(
                        id="untested_reporting_items",
                        columns=[column("account"), column("category"), column("amount_cents", money=True)],
                        rows=[
                            {
                                "account": f"{a.code} {a.name}",
                                "category": CATEGORIES[a.category] if a.category else NOT_AVAILABLE,
                                "amount_cents": amounts.get(a.code, 0),
                            }
                            for a in sources.accounts
                            if a.code in amounts and a.category not in covered
                        ],
                    ),
                ]
            }
        )
    return WorkpaperContent(
        header=header,
        sections=[
            body,
            section(
                "conclusion",
                paragraphs=[
                    paper.conclusion
                    or words(
                        "系统按已记录材料生成, 缺项见各份底稿.",
                        "Generated from the recorded materials; missing items are shown on each workpaper.",
                    ),
                    *[n.text for n in paper.notes],
                ],
            ),
            section("review", paragraphs=[sign.opinion if sign and sign.opinion else NOT_AVAILABLE]),
        ],
    )


def system_refreshed(
    previous: Workpaper | None, spec: SystemWorkpaper, sources: "Sources", papers: dict[str, Workpaper]
) -> Workpaper | None:
    if previous is not None and previous.status == "final":
        return None
    basis = [
        ObjectRef(kind="Workpaper", id=p.id, version=p.version)
        for ref, p in sorted(papers.items())
        if ref not in {"00", "S"} and (not spec.categories or p.procedure_id is not None)
    ]
    new = previous or Workpaper(id=f"WP-{spec.ref}", ref=spec.ref, area=spec.area or "", basis=basis)
    new = new.model_copy(update={"basis": basis, "missing": system_missing(new)})
    content = system_content(spec, sources, papers, new)
    if previous and (content != previous.content or basis != previous.basis):
        new = new.model_copy(update=unsigned(previous))
        new = new.model_copy(update={"missing": system_missing(new)})
        content = system_content(spec, sources, papers, new)
    new = new.model_copy(update={"content": content})
    if previous:
        if new == previous:
            return None
        new = new.model_copy(update={"version": previous.version + 1})
    return new
