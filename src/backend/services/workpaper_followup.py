"""推荐分析、更正建议与补证链路的底稿快照. 只读本次刷新资料, 不调模型."""

from typing import TYPE_CHECKING

from backend.contracts.models import AnalyticsResult, CorrectionEntry, WorkpaperSection, WorkpaperTable
from backend.services.workpaper_decisions import BASES
from backend.services.workpaper_labels import ASSERTIONS, words
from backend.services.workpaper_tables import column, section

if TYPE_CHECKING:
    from backend.services.workpapers import Sources, View


def corrections(entries: list[CorrectionEntry], id: str) -> WorkpaperTable:
    return WorkpaperTable(
        id=f"correction_{id}",
        columns=[
            column("account_code"),
            column("account_name"),
            column("debit_cents", money=True),
            column("credit_cents", money=True),
            column("description", width=80),
        ],
        rows=[e.model_dump() for e in entries],
    )


def evidence_tables(
    sources: "Sources", *, procedure_id: str | None = None, judgment_id: str | None = None
) -> list[WorkpaperTable]:
    requests = [
        r
        for r in sources.evidence_requests
        if (procedure_id is None or r.procedure_id == procedure_id)
        and (judgment_id is None or judgment_id in {r.judgment_id, r.rejudged_judgment_id})
    ]
    limitations = [
        limitation
        for limitation in sources.limitations
        if (procedure_id is None or limitation.procedure_id == procedure_id)
        and (judgment_id is None or limitation.judgment_id == judgment_id)
    ]
    tables = []
    if requests:
        tables.append(
            WorkpaperTable(
                id="evidence_requests",
                columns=[
                    column(k, width=65 if k == "description" else 28)
                    for k in (
                        "id",
                        "source_id",
                        "description",
                        "status",
                        "requested_from",
                        "document_ids",
                        "judgment_id",
                        "rejudged_judgment_id",
                    )
                ],
                rows=[
                    {
                        "id": r.id,
                        "source_id": r.subject_id,
                        "description": r.description,
                        "status": words(
                            {"open": "等待证据", "received": "已收到", "unavailable": "无法取得"}[r.status], r.status
                        ),
                        "requested_from": {
                            "bank": words("银行", "Bank"),
                            "management": words("管理层", "Management"),
                            "other": words("第三方", "Third party"),
                            "client": words("客户", "Client"),
                        }[r.requested_from],
                        "document_ids": ", ".join(r.document_ids),
                        "judgment_id": r.judgment_id,
                        "rejudged_judgment_id": r.rejudged_judgment_id,
                    }
                    for r in requests
                ],
            )
        )
    if limitations:
        tables.append(
            WorkpaperTable(
                id="scope_limitations",
                columns=[
                    column("source_id"),
                    column("amount_cents", money=True),
                    column("assertion"),
                    column("reason", width=80),
                    column("recorded_by"),
                    column("recorded_at", width=28),
                ],
                rows=[
                    {
                        "source_id": limitation.subject_id,
                        "amount_cents": limitation.amount_cents,
                        "assertion": ASSERTIONS[limitation.assertion] if limitation.assertion else None,
                        "reason": limitation.reason,
                        "recorded_by": limitation.recorded_by,
                        "recorded_at": limitation.recorded_at.isoformat(),
                    }
                    for limitation in limitations
                ],
            )
        )
    return tables


def analytics_details(view: "View", tested: WorkpaperTable) -> WorkpaperSection | None:
    result = max((o for o in view.outputs if isinstance(o, AnalyticsResult)), key=lambda a: a.version, default=None)
    if view.template.id != "PLN-07" or result is None:
        return None
    tables = [tested]
    paragraphs = []
    if result.materiality_stale:
        paragraphs.append(
            words(
                "重要性改选后, 原分析已过期, 需重新评估",
                "The analysis is stale after a materiality change and requires reassessment",
            )
        )
    if result.recommendation:
        r = result.recommendation
        tables.append(
            WorkpaperTable(
                id="analytics_recommendation",
                columns=[
                    column("basis"),
                    column("base_amount_cents", money=True),
                    column("percentage").model_copy(update={"format": "percent"}),
                    column("threshold_cents", money=True),
                    column("reason", width=90),
                    column("counter_argument", width=80),
                    column("evidence", width=70),
                ],
                rows=[
                    {
                        "basis": BASES[r.basis],
                        "base_amount_cents": r.base_amount_cents,
                        "percentage": r.percentage,
                        "threshold_cents": r.threshold_cents,
                        "reason": r.rationale,
                        "counter_argument": r.counter_argument,
                        "evidence": "\n".join(f"{p.source_type}:{p.source_id} / {p.locator}" for p in r.evidence),
                    }
                ],
            )
        )
    for key, items in (
        ("focus", result.screening.focus),
        ("additions", result.screening.additions),
        ("excluded", result.screening.excluded),
    ):
        if items:
            tables.append(
                WorkpaperTable(
                    id=f"analytics_{key}",
                    columns=[
                        column("account_code"),
                        column("account_name"),
                        column("reason", width=90),
                        column("risk_or_assertion", width=70),
                    ],
                    rows=[
                        {
                            "account_code": i.account_code,
                            "account_name": i.account_name,
                            "reason": i.reason,
                            "risk_or_assertion": i.risk_or_assertion,
                        }
                        for i in items
                    ],
                )
            )
    if result.screening.observations:
        tables.append(
            WorkpaperTable(
                id="analytics_observations",
                columns=[column("account_code"), column("reason", width=90), column("risk_or_assertion", width=70)],
                rows=[
                    {
                        "account_code": ", ".join(i.account_codes),
                        "reason": i.reason,
                        "risk_or_assertion": i.risk_or_assertion,
                    }
                    for i in result.screening.observations
                ],
            )
        )
    if result.fallback:
        paragraphs.append(
            words(f"退回规则结果: {result.fallback_reason}", f"Fallback to rule results: {result.fallback_reason}")
        )
    return section("details", tables=tables, paragraphs=paragraphs)
