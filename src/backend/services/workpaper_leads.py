"""现金与收入导引表: 试算表余额加已验证调整, 未更正错报单独列示."""

import re
from calendar import monthrange
from datetime import date, timedelta
from typing import TYPE_CHECKING

from backend.contracts.knowledge import SystemWorkpaper
from backend.contracts.models import (
    Workpaper,
    WorkpaperContent,
    WorkpaperHeader,
    WorkpaperTable,
    verified_adjustment_entries,
)
from backend.contracts.records import Account
from backend.services.adjusted_tb import adjusted_balances
from backend.services.staleness import current_misstatements, superseded_runs
from backend.services.workpaper_labels import words
from backend.services.workpaper_tables import column, section

if TYPE_CHECKING:
    from backend.services.workpapers import Sources


def period_label(start: date | None, end: date) -> tuple[str, str, int | None]:
    if start is None:
        return f"截至 {end} (起始日未记录)", f"to {end} (start date not recorded)", None
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    full = start.day == 1 and end.day == monthrange(end.year, end.month)[1]
    length = months if full else None
    zh = f"{months} 个月" if full else f"{(end - start).days + 1} 天"
    en = f"{months} months" if full else f"{(end - start).days + 1} days"
    return f"{start} 至 {end}, {zh}", f"{start} to {end}, {en}", length


def account_code(account: str, explicit: str | None, accounts: dict[str, Account]) -> str | None:
    """旧错报可未另记代码: 只接受已知的显式代码、原文括号代码或唯一完全同名, 不作模糊分摊."""
    if explicit is not None:
        return explicit if explicit in accounts else None
    match = re.fullmatch(r".*\(([^()]+)\)\s*", account)
    if match and match[1] in accounts:
        return match[1]
    matches = [a.code for a in accounts.values() if a.name.strip().casefold() == account.strip().casefold()]
    return matches[0] if len(matches) == 1 else None


def lead_content(
    spec: SystemWorkpaper, sources: "Sources", paper: Workpaper, header: WorkpaperHeader
) -> WorkpaperContent:
    engagement = sources.engagement
    assert engagement is not None
    selected = {a.code: a for a in sources.accounts if a.category in spec.categories}
    current = {r.account_code: r.balance_cents for r in sources.trial_balance if r.period == "current"}
    prior = {r.account_code: r.balance_cents for r in sources.trial_balance if r.period == "prior"}
    active = current_misstatements(
        sources.misstatements, sources.judgments, sources.exceptions, superseded_runs(sources.reruns)
    )
    entries = verified_adjustment_entries(active)
    audited = adjusted_balances(sources.trial_balance, sources.adjustment_lines, entries)
    uncorrected: dict[str, int] = {}
    refs: dict[str, list[str]] = {}
    sources_by_id = {j.id: j.procedure_id for j in sources.judgments} | {
        x.id: x.procedure_id for x in sources.exceptions
    }
    for index, item in enumerate(sorted(active, key=lambda m: m.id), 1):
        code = account_code(item.account, item.account_code, selected)
        if code is None or item.status in {"corrected", "closed", "evaluated"}:
            continue
        account = selected[code]
        remaining = item.residual_cents if item.residual_cents is not None else item.amount_cents - item.corrected_cents
        sign = 1 if (account.type in {"asset", "expense"}) == (item.direction == "understated") else -1
        uncorrected[account.code] = uncorrected.get(account.code, 0) + sign * remaining
        pid = sources_by_id.get(item.judgment_id or item.exception_id or "")
        refs.setdefault(account.code, []).extend([sources.program_refs.get(pid, "") if pid else "", f"X-{index}"])
    area_refs = [ref for ref in sources.program_refs.values() if ref.startswith(spec.ref + "-")]
    rows = []
    for code, account in sorted(selected.items()):
        unaudited = current.get(code, 0)
        final = audited.get(code, unaudited)
        rows.append(
            {
                "account_code": code,
                "account_name": account.name,
                "unaudited_cents": unaudited,
                "adjustments_cents": final - unaudited,
                "audited_cents": final,
                "prior_cents": prior.get(code, 0),
                "change_cents": final - prior.get(code, 0),
                "uncorrected_cents": uncorrected.get(code, 0),
                "source_ref": ", ".join(dict.fromkeys([*area_refs, *[ref for ref in refs.get(code, []) if ref]])),
            }
        )
    table = WorkpaperTable(
        id="lead_schedule",
        columns=[
            column("account_code"),
            column("account_name", width=32),
            *[
                column(key, money=True, total=True)
                for key in (
                    "unaudited_cents",
                    "adjustments_cents",
                    "audited_cents",
                    "prior_cents",
                    "change_cents",
                    "uncorrected_cents",
                )
            ],
            column("source_ref", width=38),
        ],
        rows=rows,
    )
    ledger = sum(r.balance_cents for r in sources.trial_balance if r.period == "current" and r.account_code in selected)
    total = sum(current.get(code, 0) for code in selected)
    difference = total - ledger
    tieout = WorkpaperTable(
        id="lead_tieout",
        columns=[column("check", width=70), column("status"), column("difference_cents", money=True)],
        rows=[
            {
                "check": words(
                    "未审数合计与所选分类的试算表合计一致",
                    "Unaudited total agrees to the trial balance for the selected categories",
                ),
                "status": "✓" if difference == 0 else "✗",
                "difference_cents": difference,
            }
        ],
    )
    current_zh, current_en, current_months = period_label(engagement.period_start, engagement.period_end)
    prior_zh, prior_en, prior_months = period_label(
        sources.traits.prior_period_start, engagement.period_start - timedelta(days=1)
    )
    caution = current_months is None or prior_months is None or current_months != prior_months
    period = words(
        f"本期: {current_zh}. 上期: {prior_zh}. "
        + ("期间长度不同或未记录, 变动不宜直接比较. " if caution else "")
        + "上年数未重述.",
        f"Current: {current_en}. Prior: {prior_en}. "
        + (
            "Periods differ in length or their length is not recorded; movements are not directly comparable. "
            if caution
            else ""
        )
        + "Prior figures are not restated.",
    )
    return WorkpaperContent(
        header=header,
        sections=[
            section(
                "objectives",
                paragraphs=[
                    words(
                        "现金或收入类科目与试算表勾稽 (ASA 330 第 30 段).",
                        "Reconcile cash or revenue accounts to the trial balance (ASA 330 paragraph 30).",
                    )
                ],
            ),
            section(
                "basis",
                paragraphs=[
                    words(
                        "账套为财报依据; 本期与上期期末试算表, 已验证更正分录及当前错报.",
                        "The financial report is prepared from the ledger; current and prior closing trial balances, "
                        "verified adjustment entries and current misstatements.",
                    )
                ],
            ),
            section(
                "procedures",
                paragraphs=[
                    period,
                    words(
                        f"已验证调整凭证: {', '.join(entries) or '—'}",
                        f"Verified adjustment entries: {', '.join(entries) or '—'}",
                    ),
                ],
            ),
            section("checks", tables=[tieout]),
            section("details", tables=[table]),
            section(
                "exceptions",
                paragraphs=[
                    words(
                        "未更正错报单独列示, 不加入审定数.",
                        "Uncorrected misstatements are shown separately and are excluded from audited balances.",
                    )
                ],
            ),
            section(
                "conclusion",
                paragraphs=[
                    paper.conclusion
                    or words(
                        "审定数 = 未审数 + 已验证更正调整; 借方为正, 贷方为负.",
                        "Audited balance = unaudited balance + verified adjustments; "
                        "debits are positive and credits negative.",
                    ),
                    *[n.text for n in paper.notes],
                ],
            ),
            section("cross_references", paragraphs=[", ".join(area_refs)]),
            section(
                "review",
                paragraphs=[
                    paper.signoff.opinion
                    if paper.signoff and paper.signoff.opinion
                    else words("未记录", "Not recorded")
                ],
            ),
        ],
    )
