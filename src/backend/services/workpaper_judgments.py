"""判断与抽取底稿: 按业务对象保存原文、推理、证据与复核决定, 不重新调用模型."""

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from backend.contracts.models import (
    Confirmation,
    Contract,
    EvidencePointer,
    Judgment,
    LocalizedText,
    ProcedureRun,
    ReviewRecord,
    WorkpaperSection,
    WorkpaperTable,
    WorkpaperValue,
)
from backend.services.currency import format_money
from backend.services.workpaper_labels import STATES, words
from backend.services.workpaper_tables import column, section, value

if TYPE_CHECKING:
    from backend.services.workpapers import Sources, View


def model_judgments(judgments: Sequence[Judgment], runs: Sequence[ProcedureRun]) -> list[Judgment]:
    """各对象模型给出的最后一次判断 (人工判断 -H 不算; 重跑过的取最后一次执行的), 按对象排序."""
    latest: dict[str, Judgment] = {}
    run_numbers = {r.id: r.run_no for r in runs}
    for j in sorted(judgments, key=lambda j: (run_numbers.get(j.run_id, 0), j.created_at, j.version, j.id)):
        if "-H" not in j.id:
            latest[j.subject_id] = j
    return [latest[s] for s in sorted(latest)]


STEPS = {
    key: words(zh, en)
    for key, zh, en in (
        ("fact", "事实", "Fact"),
        ("standard", "准则", "Standard"),
        ("inference", "推断", "Inference"),
        ("assumption", "假设", "Assumption"),
        ("limitation", "局限", "Limitation"),
    )
}
TERMS = {
    key: words(zh, en)
    for key, zh, en in (
        ("payment", "付款", "Payment"),
        ("control_transfer", "控制权转移", "Control transfer"),
        ("return_right", "退货权", "Return right"),
        ("rebate", "返利", "Rebate"),
        ("termination", "终止", "Termination"),
        ("guarantee", "担保", "Guarantee"),
        ("related_party", "关联方", "Related party"),
    )
}
ACTIONS = {
    key: words(zh, en)
    for key, zh, en in (
        ("accept", "接受", "Accept"),
        ("close", "关闭事项", "Close"),
        ("misstatement", "认定错报", "Misstatement"),
        ("more_evidence", "补充证据", "More evidence"),
    )
}
AWAITING = words("待审计师决定", "Awaiting auditor decision")


def pointers(items: Sequence[EvidencePointer]) -> str:
    return "\n".join(
        f"{p.source_type}:{p.source_id} / {p.locator}"
        + (f" / p.{p.page}" if p.page else "")
        + (f" / {p.reliability}" if p.reliability else "")
        for p in items
    )


def contract_tables(contract: Contract) -> list[WorkpaperTable]:
    return [
        WorkpaperTable(
            id=f"contract_{contract.id}",
            columns=[
                column("contract_id"),
                column("document_id", width=30),
                column("counterparty", width=30),
                column("amount_cents", money=True),
                column("sign_date"),
                column("version"),
            ],
            rows=[
                {
                    "contract_id": contract.id,
                    "document_id": contract.document_id,
                    "counterparty": contract.counterparty,
                    "amount_cents": contract.amount_cents,
                    "sign_date": str(contract.sign_date) if contract.sign_date else None,
                    "version": contract.version,
                }
            ],
        ),
        WorkpaperTable(
            id=f"terms_{contract.id}",
            columns=[column("id"), column("term_type"), column("quote", width=95), column("page", width=10)],
            rows=[
                {"id": term.id, "term_type": TERMS[term.term_type], "quote": term.text, "page": term.page}
                for term in contract.terms
            ],
        ),
    ]


def edit_value(data: dict[str, Any], field: str, currency: str) -> WorkpaperValue:
    """抽取修改记录仅取修改的字段, 金额用项目币种, 条款按既有的下标规则."""
    match = re.fullmatch(r"terms\[(\d+)\]\.(text|term_type)", field)
    if match:
        terms = data.get("terms", [])
        raw = terms[int(match[1])].get(match[2]) if int(match[1]) < len(terms) else None
    else:
        raw = data.get(field)
    return format_money(raw, currency) if field.endswith("_cents") and isinstance(raw, int) else value(raw)


def edits(sources: "Sources", ids: set[str], target_types: set[str]) -> WorkpaperTable:
    rows = []
    for record in sources.extractions:
        if record.target_id not in ids or record.target_type not in target_types or record.field is None:
            continue
        prior = max(
            (
                r
                for r in sources.extractions
                if r.target_id == record.target_id
                and r.target_type == record.target_type
                and r.version < record.version
            ),
            key=lambda r: r.version,
            default=None,
        )
        rows.append(
            {
                "source_id": record.target_id,
                "version": record.version,
                "field": record.field,
                "before": edit_value(prior.data, record.field, sources.currency) if prior else None,
                "after": edit_value(record.data, record.field, sources.currency),
                "changed_by": record.changed_by,
                "changed_at": record.changed_at.isoformat(),
                "reason": record.reason,
            }
        )
    return WorkpaperTable(
        id="extraction_changes",
        columns=[
            column("source_id"),
            column("version"),
            column("field"),
            column("before", width=60),
            column("after", width=60),
            column("changed_by"),
            column("changed_at", width=28),
            column("reason", width=70),
        ],
        rows=rows,
    )


def reviews_table(judgment: Judgment, sources: "Sources") -> WorkpaperTable:
    tasks = [t for t in sources.tasks if t.target_id == judgment.id]
    records = {
        t.id: sorted((r for r in sources.reviews if r.task_id == t.id), key=lambda r: (r.decided_at, r.id))
        for t in tasks
    }
    rows = []
    for task in tasks:
        entries: list[ReviewRecord | None] = list(records[task.id]) or [None]
        for record in entries:
            withdrawn = next((w for w in sources.withdrawals if record and w.record_id == record.id), None)
            rows.append(
                {
                    "id": task.id,
                    "status": words("已撤回", "Withdrawn") if withdrawn else STATES[task.status],
                    "withdrawn_at": withdrawn.withdrawn_at.isoformat() if withdrawn else None,
                    "decision": STATES[record.decision] if record else AWAITING,
                    "conclusion": record.override_conclusion
                    or (record.manual_result.conclusion if record.manual_result else None)
                    if record
                    else None,
                    "reviewer": record.reviewed_by if record else None,
                    "reviewed_at": record.decided_at.isoformat() if record else None,
                    "reason": record.reason if record else None,
                }
            )
    if not rows:
        rows = [
            {
                "status": STATES[judgment.review_status],
                "decision": AWAITING if judgment.review_status == "pending" else STATES[judgment.review_status],
            }
        ]
    return WorkpaperTable(
        id=f"reviews_{judgment.id}",
        columns=[
            column("id", width=32),
            column("status"),
            column("decision"),
            column("conclusion", width=75),
            column("reviewer"),
            column("reviewed_at", width=28),
            column("withdrawn_at", width=28),
            column("reason", width=70),
        ],
        rows=rows,
    )


def judgment_section(judgment: Judgment, view: "View", sources: "Sources") -> WorkpaperSection:
    run = next((r for r in sources.runs if r.id == judgment.run_id), None)
    contract = next((c for c in sources.contracts if c.id == judgment.subject_id), None)
    confirmation = next(
        (
            c
            for c in sources.outputs.get("confirmations", [])
            if isinstance(c, Confirmation) and c.bank_account_id == judgment.subject_id
        ),
        None,
    )
    tables = contract_tables(contract) if contract else ([confirmation_table([confirmation])] if confirmation else [])
    tables.extend(
        [
            WorkpaperTable(
                id=f"reasoning_{judgment.id}",
                columns=[
                    column("step", width=10),
                    column("kind"),
                    column("description", width=95),
                    column("evidence", width=65),
                    column("from_steps"),
                ],
                rows=[
                    {
                        "step": n,
                        "kind": STEPS[step.kind],
                        "description": step.text,
                        "evidence": pointers(step.evidence),
                        "from_steps": ", ".join(str(i + 1) for i in step.from_steps),
                    }
                    for n, step in enumerate(judgment.reasoning_chain, 1)
                ],
            ),
            WorkpaperTable(
                id=f"evidence_{judgment.id}",
                columns=[column("evidence", width=100)],
                rows=[{"evidence": pointers([p])} for p in judgment.evidence],
            ),
            WorkpaperTable(
                id=f"confidence_{judgment.id}",
                columns=[column("kind"), column("confidence")],
                rows=[
                    {"kind": words("总体置信度", "Overall confidence"), "confidence": judgment.confidence},
                    *[
                        {
                            "kind": words({"evidence": "证据", "rule": "规则", "llm": "模型"}.get(k, k), k),
                            "confidence": v,
                        }
                        for k, v in judgment.confidence_parts.items()
                    ],
                ],
            ),
            WorkpaperTable(
                id=f"critic_{judgment.id}",
                columns=[column("status"), column("conclusion", width=95), column("reason", width=75)],
                rows=[
                    {
                        "status": STATES[judgment.critic_verdict],
                        "conclusion": judgment.critic_conclusion,
                        "reason": judgment.critic_reason,
                    }
                ],
            ),
            reviews_table(judgment, sources),
        ]
    )
    paragraphs: list[str | LocalizedText] = [
        words(
            f"判断 {judgment.id} (v{judgment.version}): {judgment.conclusion}",
            f"Judgment {judgment.id} (v{judgment.version}): {judgment.conclusion}",
        ),
        words(f"反方论证: {judgment.counter_argument}", f"Counter-argument: {judgment.counter_argument}"),
        words(
            f"建议: {ACTIONS[judgment.proposed_action].zh}", f"Recommendation: {ACTIONS[judgment.proposed_action].en}"
        ),
    ]
    if not judgment.reasoning_chain:
        paragraphs.append(words("缺: 推理链未记录", "Missing: reasoning chain not recorded"))
    if not judgment.evidence:
        paragraphs.append(words("缺: 证据指针未记录", "Missing: evidence pointers not recorded"))
    for index, step in enumerate(judgment.reasoning_chain, 1):
        if step.kind in {"fact", "standard"} and not step.evidence:
            paragraphs.append(
                words(f"缺: 步骤 {index} 的证据指针未记录", f"Missing: evidence pointers for step {index}")
            )
    return WorkpaperSection(
        id=f"judgment_{judgment.id}",
        title=words(judgment.subject_id, judgment.subject_id),
        generated_language=run.language if run else "en",
        paragraphs=paragraphs,
        tables=tables,
    )


def confirmation_table(confirmations: Sequence[Confirmation]) -> WorkpaperTable:
    return WorkpaperTable(
        id="confirmation_fields",
        columns=[
            column("id"),
            column("document_id", width=30),
            column("bank_account_id"),
            column("balance_date"),
            column("confirmed_balance_cents", money=True),
            column("accrued_interest_cents", money=True),
            column("evidence", width=70),
        ],
        rows=[
            {
                "id": c.id,
                "document_id": c.reply_document_id,
                "bank_account_id": c.bank_account_id,
                "balance_date": str(c.balance_date) if c.balance_date else None,
                "confirmed_balance_cents": c.confirmed_balance_cents,
                "accrued_interest_cents": c.accrued_interest_cents,
                "evidence": pointers(c.evidence),
            }
            for c in confirmations
        ],
    )


def detail_sections(view: "View", sources: "Sources") -> WorkpaperSection | None:
    """返回完整的测试明细区块, 其他程序继续使用通用规则表."""
    if view.template.id == "DOC-04":
        contracts = sources.contracts
        return section("details").model_copy(
            update={
                "children": [
                    WorkpaperSection(
                        id=f"extracted_{c.id}",
                        title=words(c.id, c.id),
                        tables=[*contract_tables(c), edits(sources, {c.id}, {"contract"})],
                    )
                    for c in contracts
                ]
            }
        )
    if view.template.id == "DOC-06":
        confirmations = [c for c in view.outputs if isinstance(c, Confirmation)]
        return section(
            "details",
            tables=[
                confirmation_table(confirmations),
                edits(
                    sources,
                    {c.id for c in confirmations} | {c.reply_document_id for c in confirmations if c.reply_document_id},
                    {"reply", "confirmation"},
                ),
            ],
        )
    judged = model_judgments(view.judgments, view.runs)
    if not judged:
        return None
    summary = WorkpaperTable(
        id="judgment_summary",
        columns=[
            column("source_id"),
            column("id", width=35),
            column("conclusion", width=90),
            column("confidence"),
            column("recommendation"),
            column("status"),
        ],
        rows=[
            {
                "source_id": j.subject_id,
                "id": j.id,
                "conclusion": j.conclusion,
                "confidence": j.confidence,
                "recommendation": ACTIONS[j.proposed_action],
                "status": AWAITING if j.review_status == "pending" else STATES[j.review_status],
            }
            for j in judged
        ],
    )
    return section("details", tables=[summary]).model_copy(
        update={"children": [judgment_section(j, view, sources) for j in judged]}
    )
