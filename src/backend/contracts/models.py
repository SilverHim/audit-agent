"""领域对象，定义见 开发接口文档.md 第 2 节。其他包只引用，不另行定义。"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── 2.1 枚举 ──────────────────────────────────────────────


class Phase(StrEnum):
    P0_Acceptance = "P0_Acceptance"
    P1_Planning = "P1_Planning"
    P2_Executing = "P2_Executing"
    P3_Completion = "P3_Completion"
    P4_Reporting = "P4_Reporting"
    Archived = "Archived"
    Declined = "Declined"


class Assertion(StrEnum):
    Occurrence = "Occurrence"
    Completeness = "Completeness"
    Accuracy = "Accuracy"
    Cutoff = "Cutoff"
    Existence = "Existence"
    Valuation = "Valuation"
    Rights = "Rights"


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    significant = "significant"


class Engine(StrEnum):
    rule = "rule"
    llm = "llm"
    hybrid = "hybrid"
    manual = "manual"


class AutonomyLevel(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class ProcedureStatus(StrEnum):
    planned = "planned"
    executing = "executing"
    awaiting_human = "awaiting_human"
    completed = "completed"


class RunTrigger(StrEnum):
    initial = "initial"
    rerun = "rerun"


class RunStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class EvidenceType(StrEnum):
    voucher = "voucher"
    invoice = "invoice"
    delivery = "delivery"
    bank = "bank"
    contract = "contract"
    external = "external"
    standard = "standard"


class Reliability(StrEnum):
    external_original = "external_original"
    external_copy = "external_copy"
    internal_original = "internal_original"
    internal_copy = "internal_copy"


class StepKind(StrEnum):
    fact = "fact"
    standard = "standard"
    inference = "inference"
    assumption = "assumption"
    limitation = "limitation"


class StepStatus(StrEnum):
    supported = "supported"
    unsupported = "unsupported"


class ProposedAction(StrEnum):
    accept = "accept"
    close = "close"
    misstatement = "misstatement"
    more_evidence = "more_evidence"


class ReviewStatus(StrEnum):
    pending = "pending"
    confirmed = "confirmed"
    overridden = "overridden"


class ExceptionDisposition(StrEnum):
    open = "open"
    attributed = "attributed"
    unattributed = "unattributed"


class TaskKind(StrEnum):
    normal = "normal"
    unattributed = "unattributed"
    failed = "failed"


class TaskStatus(StrEnum):
    pending = "pending"
    decided = "decided"


class TargetType(StrEnum):
    Judgment = "Judgment"
    AuditException = "AuditException"
    ProcedureRun = "ProcedureRun"


class Decision(StrEnum):
    confirm = "confirm"
    override = "override"
    misstatement = "misstatement"
    rerun = "rerun"
    manual_result = "manual_result"


class GateId(StrEnum):
    ACC_01 = "ACC-01"
    PLN_01_03 = "PLN-01/03"
    FIN_01 = "FIN-01"
    RPT_01 = "RPT-01"


class GateVerdict(StrEnum):
    approve = "approve"
    reject = "reject"
    insufficient_info = "insufficient_info"


class OpinionType(StrEnum):
    unqualified = "unqualified"
    qualified = "qualified"
    adverse = "adverse"
    disclaimer = "disclaimer"


class MisstatementType(StrEnum):
    factual = "factual"
    judgmental = "judgmental"
    projected = "projected"


class Direction(StrEnum):
    overstated = "overstated"
    understated = "understated"


class ConfirmationResult(StrEnum):
    pending = "pending"
    agreed = "agreed"
    difference = "difference"


class ReceiptChannel(StrEnum):
    direct_mail = "direct_mail"
    direct_email = "direct_email"
    portal_via_client = "portal_via_client"
    oral = "oral"


class DocumentType(StrEnum):
    contract = "contract"
    confirmation = "confirmation"
    other = "other"


class TermType(StrEnum):
    payment = "payment"
    control_transfer = "control_transfer"
    return_right = "return_right"
    rebate = "rebate"
    termination = "termination"
    guarantee = "guarantee"
    related_party = "related_party"


class ReportSection(StrEnum):
    opinion = "opinion"
    basis_for_opinion = "basis_for_opinion"
    directors_responsibilities = "directors_responsibilities"
    auditor_responsibilities = "auditor_responsibilities"
    other_legal_and_regulatory = "other_legal_and_regulatory"


class ReportStatus(StrEnum):
    draft = "draft"
    signed = "signed"


class TrailKind(StrEnum):
    llm_call = "llm_call"
    external_call = "external_call"
    decision = "decision"
    transition = "transition"


class AgentId(StrEnum):
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    A4 = "A4"
    A5 = "A5"
    A6 = "A6"
    A9 = "A9"
    A10 = "A10"


PointerSource = Literal[
    "journal_entry",
    "sales_invoice",
    "delivery_note",
    "bank_transaction",
    "bank_account",
    "trial_balance",
    "document",
    "standard",
    "trail",
]
SourceType = Literal[
    "journal_entry", "sales_invoice", "delivery_note", "bank_transaction", "bank_account", "trial_balance"
]
MaterialityBasis = Literal["revenue", "total_assets", "profit_before_tax"]
DocumentStatus = Literal["received", "extracted", "needs_manual"]


# ── 2.2 证据与判断 ─────────────────────────────────────────


class Versioned(Model):
    id: str
    version: int = 1


class EvidencePointer(Model):
    type: EvidenceType
    source_type: PointerSource
    source_id: str
    locator: str
    page: int | None = None
    reliability: Reliability | None = None


class ReasoningStep(Model):
    kind: StepKind
    text: str
    evidence: list[EvidencePointer] = []
    from_steps: list[int] = []
    status: StepStatus = StepStatus.supported


class MisstatementDraft(Model):
    type: MisstatementType
    account: str
    assertion: Assertion
    amount_cents: int
    direction: Direction


class Judgment(Versioned):
    procedure_id: str
    run_id: str
    subject_id: str
    conclusion: str
    confidence: float
    confidence_parts: dict[str, float]
    reasoning_chain: list[ReasoningStep]
    evidence: list[EvidencePointer]
    counter_argument: str
    proposed_action: ProposedAction
    misstatement_draft: MisstatementDraft | None = None
    exception_ids: list[str] = []
    autonomy_level: AutonomyLevel
    review_status: ReviewStatus = ReviewStatus.pending
    superseded_by: str | None = None
    performed_by: str
    created_at: datetime
    trail_ref: str | None = None


class AuditException(Versioned):
    """规格书中的 Exception，改名避开 Python 内置名。"""

    procedure_id: str
    run_id: str
    rule_id: str
    severity: Literal["low", "medium", "high"]
    source_ids: list[str]
    amount_cents: int | None = None
    detail: dict[str, Any] = {}
    evidence: list[EvidencePointer]
    disposition: ExceptionDisposition = ExceptionDisposition.open
    attributed_by: str | None = None


# ── 2.3 计划与程序 ─────────────────────────────────────────


class ClientProfile(Versioned):
    legal_name: str
    abn: str
    acn: str | None
    entity_type: str
    industry_anzsic: str
    listed: bool
    directors: list[str]
    notes: str = ""


class Engagement(Model):
    id: str
    client_id: str
    client_name: str
    abn: str
    period_start: date
    period_end: date
    dataset: str
    owner_id: str
    phase: Phase
    locked: bool = False
    version_snapshot: dict[str, str] = {}
    created_at: datetime
    updated_at: datetime


class AuditObjective(Versioned):
    account: str
    assertion: Assertion
    risk_level: RiskLevel
    is_material: bool


class RiskItem(Versioned):
    objective_id: str
    assertion: Assertion
    level: RiskLevel
    fraud_related: bool
    description: str
    revision_reason: str | None = None
    approved_by: str | None = None


class Procedure(Versioned):
    phase: Phase
    template_commit: str
    objective_ids: list[str] = []
    params: dict[str, Any] = {}
    autonomy_level: AutonomyLevel
    status: ProcedureStatus = ProcedureStatus.planned
    rerun_subject_ids: list[str] = []


class ProcedureRun(Versioned):
    procedure_id: str
    run_no: int
    trigger: RunTrigger
    instance: str | None = None
    params_snapshot: dict[str, Any]
    started_at: datetime
    ended_at: datetime | None = None
    status: RunStatus
    failure_reason: str | None = None


class ScheduleRow(Model):
    layer: int
    instance: str
    procedure_ids: list[str]


# ── 2.4 闸门材料 ───────────────────────────────────────────


class RegistryCheck(Model):
    check: Literal["abn_active", "name_match", "company_registered"]
    passed: bool
    detail: str
    evidence: EvidencePointer


class AcceptanceScorecard(Versioned):
    registry_checks: list[RegistryCheck]
    exception_ids: list[str]
    client_profile_version: int


class AnalyticsResult(Versioned):
    ratios: dict[str, float]
    exception_ids: list[str]


class MaterialityCandidate(Model):
    basis: MaterialityBasis
    base_amount_cents: int
    pct_low: float
    pct_high: float
    low_cents: int
    high_cents: int


class MaterialityCandidates(Versioned):
    candidates: list[MaterialityCandidate]
    performance_pct_range: tuple[float, float] = (0.5, 0.75)
    clearly_trivial_pct: float = 0.05


class UncorrectedItem(Model):
    misstatement_id: str
    amount_cents: int
    direction: Direction


class UncorrectedSchedule(Versioned):
    """规格书中的 UncorrectedMisstatementSchedule。"""

    items: list[UncorrectedItem]
    excluded_trivial_ids: list[str]
    total_cents: int
    materiality_cents: int
    candidate_opinion: OpinionType


# ── 2.5 复核与决定 ─────────────────────────────────────────


class ReviewTask(Versioned):
    kind: TaskKind
    procedure_id: str
    target_type: TargetType
    target_id: str
    target_version: int
    run_id: str
    subject_id: str | None = None
    status: TaskStatus = TaskStatus.pending
    created_at: datetime


class ManualResult(Model):
    conclusion: str
    evidence: list[EvidencePointer] = []


class ReviewRecord(Versioned):
    task_id: str
    target_version: int
    decision: Decision
    reason: str | None = None
    override_conclusion: str | None = None
    misstatement_draft: MisstatementDraft | None = None
    rerun_params: dict[str, Any] | None = None
    manual_result: ManualResult | None = None
    reviewed_by: str
    decided_at: datetime


class GateEdit(Model):
    target_id: str
    target_version: int
    field: Literal["level"]
    new_value: RiskLevel
    reason: str


class MaterialityChoice(Model):
    basis: MaterialityBasis
    pct: float
    performance_pct: float
    clearly_trivial_pct: float


class Annotation(Model):
    section: ReportSection
    quote: str | None = None
    comment: str


class GateDecision(Versioned):
    gate: GateId
    target_id: str
    target_version: int
    decision: GateVerdict
    materiality: MaterialityChoice | None = None
    edits: list[GateEdit] = []
    disposition: OpinionType | None = None
    annotations: list[Annotation] = []
    reason: str | None = None
    decided_by: str
    decided_at: datetime


class Misstatement(Versioned):
    judgment_id: str | None
    exception_id: str | None
    review_record_id: str
    type: MisstatementType
    account: str
    assertion: Assertion
    amount_cents: int
    direction: Direction
    status: Literal["uncorrected"] = "uncorrected"


# ── 2.6 函证、文件与报告 ───────────────────────────────────


class Confirmation(Versioned):
    type: Literal["bank"] = "bank"
    bank_account_id: str
    confirming_party: str
    idempotency_key: str
    sent_at: datetime
    reply_document_id: str | None = None
    receipt_channel: ReceiptChannel | None = None
    replied_at: date | None = None
    balance_date: date | None = None
    confirmed_balance_cents: int | None = None
    book_balance_cents: int | None = None
    difference_cents: int | None = None
    result: ConfirmationResult = ConfirmationResult.pending
    evidence: list[EvidencePointer] = []


class DocumentRef(Model):
    id: str
    type: DocumentType
    file_name: str
    sha256: str
    page_count: int
    reliability: Reliability
    status: DocumentStatus
    uploaded_at: datetime


class ContractTerm(Model):
    id: str
    term_type: TermType
    text: str
    page: int


class Contract(Model):
    id: str
    document_id: str
    counterparty: str
    counterparty_abn: str | None
    type: Literal["sales"] = "sales"
    amount_cents: int | None
    sign_date: date | None
    terms: list[ContractTerm]
    evidence: list[EvidencePointer] = []
    extracted_by_run: str


class JudgmentRef(Model):
    id: str
    version: int


class AuditReportVersion(Versioned):
    revision: int
    opinion_type: OpinionType
    sections: dict[ReportSection, str]
    judgment_refs: list[JudgmentRef]
    addresses_decision_id: str | None = None
    status: ReportStatus = ReportStatus.draft
    report_date: date | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None


# ── 2.7 其他 ───────────────────────────────────────────────


class User(Model):
    id: str
    email: str
    full_name: str


class AbnRecord(Model):
    abn: str
    acn: str | None
    entity_name: str
    status: Literal["Active", "Cancelled"]
    entity_type: str


class AsicCompanyRecord(Model):
    acn: str
    name: str
    status: Literal["Registered", "Deregistered"]
    registered_date: date


class TrailEntry(Model):
    id: str | None = None
    engagement_id: str
    kind: TrailKind
    actor: str
    procedure_id: str | None = None
    run_id: str | None = None
    summary: str
    model: str | None = None
    prompt_hash: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    idempotency_key: str | None = None
    payload: dict[str, Any] = {}
    created_at: datetime | None = None
