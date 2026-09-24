"""LLM 结构化输出模型，定义见 开发接口文档.md 第 5 节。"""

from datetime import date

from backend.contracts.models import (
    EvidencePointer,
    JudgmentRef,
    MisstatementDraft,
    Model,
    ProposedAction,
    ReasoningStep,
    ReceiptChannel,
    TermType,
)


class JudgmentDraft(Model):
    """REV-03、REV-02、CSH-02。"""

    conclusion: str
    reasoning_chain: list[ReasoningStep]
    evidence: list[EvidencePointer]
    counter_argument: str
    proposed_action: ProposedAction
    misstatement_draft: MisstatementDraft | None = None
    self_confidence: float


class ExtractedTerm(Model):
    term_type: TermType
    text: str
    page: int


class ContractExtraction(Model):
    """DOC-04。"""

    counterparty: str
    counterparty_abn: str | None
    amount_cents: int | None
    sign_date: date | None
    evidence: list[EvidencePointer]
    terms: list[ExtractedTerm]


class ConfirmationExtraction(Model):
    """DOC-06。"""

    confirming_party: str
    account_no: str
    confirmed_balance_cents: int
    balance_date: date
    receipt_channel: ReceiptChannel | None
    evidence: list[EvidencePointer]


class ReportDraft(Model):
    """RPT-03。"""

    opinion: str
    basis_for_opinion: str
    judgment_refs: list[JudgmentRef]
