"""OutputValidator 四级校验: 语法, 结构, 引用, 语义 (开发接口文档第 5 节)."""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from backend.agents import ingest, review
from backend.contracts import llm_outputs
from backend.contracts.llm_outputs import (
    ConfirmationExtraction,
    ContractExtraction,
    CriticIndependent,
    CriticVerdictOut,
    DocumentClassification,
    ExtractedTerm,
    JudgmentDraft,
    RegistryExtraction,
    ReportDraft,
    SheetRecognition,
)
from backend.contracts.models import (
    AuditException,
    Confirmation,
    Decision,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    Judgment,
    OpinionType,
    ProposedAction,
    ReasoningStep,
    ReceiptChannel,
    Reliability,
    ReviewRecord,
    ReviewStatus,
    StepKind,
    StepStatus,
    TaskKind,
    TermType,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import LLMResponse
from backend.contracts.records import JournalEntry, SalesInvoice
from backend.contracts.services import JudgmentContext, ValidationContext
from backend.core.config import get_settings
from backend.orchestration.decisions import override_effects
from backend.services.judgment_engine import (
    DOCUMENT_EVIDENCE,
    CheckResult,
    JudgmentEngine,
    OutputValidator,
    citation_keys,
    judgment_key,
    output_model,
    structure_only,
    supported_amounts,
)
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_CONTRACTS,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_RECORDS,
    NUMBAT_REPLIES,
    FakeFinancialStore,
    FakeTrail,
    ScriptedLLM,
    make_run,
    xero_document,
)
from tests.xero_fakes import xero_profile

EID = ENGAGEMENT.id
LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
REV02 = LIBRARY.load("REV-02")
CON001 = NUMBAT_CONTRACTS[0]  # CON-001.pdf, 3 页, external_copy
FIXTURES = get_settings().LLM_FIXTURES_ROOT


def make_validator(
    trail: FakeTrail | None = None, documents: list[DocumentRef] | None = None
) -> tuple[OutputValidator, FakeFinancialStore]:
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS) + (documents or []), NUMBAT_RECORDS)
    return OutputValidator(financial, LIBRARY, trail or FakeTrail()), financial


def respond[T: BaseModel](output: T | None, raw: str | None = None, error: str | None = None) -> LLMResponse[T]:
    return LLMResponse(
        output=output,
        raw=raw if raw is not None else (output.model_dump_json() if output is not None else ""),
        model="mock",
        input_tokens=0,
        output_tokens=0,
        error=error,
    )


# 模型填的 type 与 reliability 故意写错, 校验后应按来源改写
INVOICE = EvidencePointer(
    type=EvidenceType.contract,
    source_type="sales_invoice",
    source_id="SI-2026-0612",
    locator="posting_date",
    reliability=Reliability.external_original,
)
DELIVERY = EvidencePointer(
    type=EvidenceType.invoice, source_type="delivery_note", source_id="DN-2026-0612", locator="signed_date"
)
CLAUSE = EvidencePointer(
    type=EvidenceType.external,
    source_type="document",
    source_id=CON001.id,
    locator="CON-001-T1",
    page=2,
    reliability=Reliability.external_original,
)
STANDARD = EvidencePointer(
    type=EvidenceType.voucher,
    source_type="standard",
    source_id="AASB15-P38",
    locator="para 38",
    reliability=Reliability.internal_copy,
)


def cutoff_draft(**changes: Any) -> JudgmentDraft:
    """REV-02 对 SI-2026-0612 的草稿 (合成数据集设计第 7 节第 9 行): 控制权 2026-07-03 转移, 收入高估."""
    draft = JudgmentDraft(
        conclusion="SI-2026-0612 控制权在 2026-07-03 签收时转移, 属下期, 本期收入高估 142,000.00.",
        reasoning_chain=[
            ReasoningStep(kind=StepKind.fact, text="发票 2026-06-28 入账.", evidence=[INVOICE]),
            ReasoningStep(kind=StepKind.fact, text="客户 2026-07-03 签收发运单.", evidence=[DELIVERY]),
            ReasoningStep(kind=StepKind.fact, text="合同第 5.1 条约定签收时转移控制权.", evidence=[CLAUSE]),
            ReasoningStep(kind=StepKind.standard, text="在某一时点转移控制权时确认收入.", evidence=[STANDARD]),
            ReasoningStep(kind=StepKind.inference, text="收入属下期, 本期高估.", from_steps=[0, 1, 2, 3]),
        ],
        evidence=[INVOICE, DELIVERY, CLAUSE, STANDARD],
        counter_argument="若客户在 2026-06-30 前已取得控制权, 结论不成立.",
        proposed_action=ProposedAction.misstatement,
        misstatement_draft=NUMBAT_CUTOFF_DRAFT,
        self_confidence=0.9,
    )
    return draft.model_copy(update=changes)


def with_step(draft: JudgmentDraft, index: int, **changes: Any) -> JudgmentDraft:
    chain = list(draft.reasoning_chain)
    chain[index] = chain[index].model_copy(update=changes)
    return draft.model_copy(update={"reasoning_chain": chain})


async def check(output: Any, raw: str | None = None, ctx: ValidationContext | None = None, **kw: Any) -> Any:
    validator, _ = make_validator(**kw)
    return await validator.validate(EID, REV02, respond(output, raw), ctx or ValidationContext())


# ── 签名与上下文 ─────────────────────────────────────────


def test_validation_context_defaults() -> None:
    ctx = ValidationContext()
    assert ctx.rule_findings == [] and ctx.document is None and ctx.judgments == []


def test_output_model_resolves_every_llm_procedure() -> None:
    models = {t.id: output_model(t) for t in LIBRARY.all() if t.llm is not None}
    assert models == {
        "DOC-04": ContractExtraction,
        "DOC-06": ConfirmationExtraction,
        "REV-03": JudgmentDraft,
        "REV-02": JudgmentDraft,
        "CSH-02": JudgmentDraft,
        "RPT-03": ReportDraft,
        "GEN-09": CriticIndependent,  # 第二轮的 CriticVerdictOut 由 A7 换模板的 llm 配置 (review.VERDICT_SPEC)
    }


def test_output_model_rejects_unknown_name_and_missing_llm() -> None:
    bad = REV02.model_copy(update={"llm": REV02.llm.model_copy(update={"output_model": "Nope"})})  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="Nope"):
        output_model(bad)
    with pytest.raises(ValueError, match="llm"):
        output_model(LIBRARY.load("REV-08"))


# ── 引用与语义的分派表 (R83) ─────────────────────────────────────────


def test_every_referenced_output_model_is_registered() -> None:
    """引用方用到的输出模型都已登记引用与语义校验: 程序库全部模板 (含未启用的) 的 llm.output_model, 加上代码里的两份
    规格 (上传文件分类 ingest.CLASSIFY, 独立复核第二轮 review.VERDICT_SPEC). 新加输出模型而漏登记时在此报出,
    不至于在运行时被兜底转人工 (如上传分类转 needs_manual)."""
    referenced = {output_model(t) for t in LIBRARY.all(include_disabled=True) if t.llm is not None}
    referenced |= {output_model(ingest.CLASSIFY), getattr(llm_outputs, review.VERDICT_SPEC.output_model)}
    assert sorted(m.__name__ for m in referenced if m not in OutputValidator.CHECKS) == []


def test_dispatch_table_covers_existing_models() -> None:
    """现有 9 种输出模型都已登记引用与语义校验 (CriticVerdictOut, DocumentClassification 与 SheetRecognition 只做语法与
    结构; RegistryExtraction 与合同, 回函共用抽取校验, 需求 R290)."""
    assert set(OutputValidator.CHECKS) == {
        JudgmentDraft,
        ContractExtraction,
        ConfirmationExtraction,
        RegistryExtraction,
        ReportDraft,
        CriticIndependent,
        CriticVerdictOut,
        DocumentClassification,
        SheetRecognition,
    }
    assert OutputValidator.CHECKS[DocumentClassification] is structure_only
    assert OutputValidator.CHECKS[SheetRecognition] is structure_only


class ProbeOut(BaseModel):
    """测试用的临时输出模型."""

    value: int


def probe_template() -> Any:
    return REV02.model_copy(update={"llm": REV02.llm.model_copy(update={"output_model": "ProbeOut"})})  # type: ignore[union-attr]


async def test_unregistered_model_is_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    """输出模型没有登记校验时, 通过结构校验后抛 NotImplementedError (消息含模型名)."""
    monkeypatch.setattr(llm_outputs, "ProbeOut", ProbeOut, raising=False)
    validator, _ = make_validator()
    with pytest.raises(NotImplementedError, match="ProbeOut"):
        await validator.validate(EID, probe_template(), respond(ProbeOut(value=1)), ValidationContext())


async def test_registered_check_decides_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """register 登记临时模型的校验后, validate 调用它, 按其返回的级别, 错误, 提示与输出出报告; 语法与结构仍在前,
    结构不过时不调用."""
    monkeypatch.setattr(llm_outputs, "ProbeOut", ProbeOut, raising=False)
    monkeypatch.setattr(OutputValidator, "CHECKS", dict(OutputValidator.CHECKS))  # 登记只在本用例内有效
    seen: list[tuple[Any, ...]] = []

    async def check_probe(validator: OutputValidator, eid: str, output: ProbeOut, ctx: ValidationContext) -> Any:
        seen.append((validator, eid, output.value, ctx))
        if output.value % 2:
            return CheckResult(output, "semantic", errors=["value 须为偶数"], notes=["奇数"])
        return CheckResult(output.model_copy(update={"value": output.value // 2}))

    OutputValidator.register(ProbeOut, check_probe)
    validator, _ = make_validator()
    ctx = ValidationContext()
    odd = await validator.validate(EID, probe_template(), respond(ProbeOut(value=3)), ctx)
    assert (odd.level_failed, odd.retryable, odd.errors, odd.notes, odd.unsupported_steps) == (
        "semantic",
        False,
        ["value 须为偶数"],
        ["奇数"],
        [],
    )
    even = await validator.validate(EID, probe_template(), respond(ProbeOut(value=4)), ctx)
    assert (even.level_failed, even.output) == (None, ProbeOut(value=2))
    assert seen == [(validator, EID, 3, ctx), (validator, EID, 4, ctx)]
    bad = await validator.validate(EID, probe_template(), respond(None, raw='{"value": "x"}'), ctx)
    assert (bad.level_failed, bad.retryable, len(seen)) == ("structure", True, 2)


# ── 语法与结构 ─────────────────────────────────────────


async def test_raw_not_json_is_syntax_and_retryable() -> None:
    report = await check(None, raw="", ctx=None)
    assert report.level_failed == "syntax" and report.retryable
    assert report.output is None and report.unsupported_steps == []
    assert report.errors[0].startswith("Output is not valid JSON")


async def test_syntax_error_keeps_client_error() -> None:
    validator, _ = make_validator()
    report = await validator.validate(EID, REV02, respond(None, raw="", error="fixture_not_found"), ValidationContext())
    assert report.level_failed == "syntax" and report.errors[-1] == "fixture_not_found"


@pytest.mark.parametrize(
    ("field", "value"), [("proposed_action", "reject"), ("self_confidence", 1.2), ("self_confidence", -0.1)]
)
async def test_raw_json_not_matching_model_is_structure(field: str, value: Any) -> None:
    data = cutoff_draft().model_dump(mode="json") | {field: value}
    report = await check(None, raw=json.dumps(data))
    assert report.level_failed == "structure" and report.retryable and report.output is None
    assert field in report.errors[0]


async def test_output_of_wrong_model_is_structure() -> None:
    extraction = ContractExtraction(
        counterparty="Bilby Robotics Pty Ltd",
        counterparty_abn=None,
        amount_cents=None,
        sign_date=None,
        evidence=[],
        terms=[],
    )
    report = await check(extraction)
    assert report.level_failed == "structure" and report.retryable
    assert "expected JudgmentDraft" in report.errors[0]


async def test_valid_raw_is_parsed_when_output_missing() -> None:
    report = await check(None, raw=cutoff_draft().model_dump_json())
    assert report.level_failed is None and isinstance(report.output, JudgmentDraft)


# ── 引用 ─────────────────────────────────────────


async def test_cutoff_draft_passes() -> None:
    report = await check(cutoff_draft())
    assert report.level_failed is None and not report.retryable
    assert report.unsupported_steps == [] and report.errors == []


async def test_pointer_type_and_reliability_come_from_source() -> None:
    report = await check(cutoff_draft())
    expected = [
        (EvidenceType.invoice, Reliability.internal_original),
        (EvidenceType.delivery, Reliability.internal_original),
        (EvidenceType.contract, Reliability.external_copy),
        (EvidenceType.standard, None),
    ]
    chain = report.output.reasoning_chain
    assert [(p.type, p.reliability) for step in chain[:4] for p in step.evidence] == expected
    assert [(p.type, p.reliability) for p in report.output.evidence] == expected
    # 其余字段不动
    assert [p.source_id for p in report.output.evidence] == ["SI-2026-0612", "DN-2026-0612", CON001.id, "AASB15-P38"]
    assert report.output.evidence[2].page == 2 and report.output.misstatement_draft == NUMBAT_CUTOFF_DRAFT


@pytest.mark.parametrize(
    "bad",
    [
        DELIVERY.model_copy(update={"source_id": "DN-9999"}),  # 查无此行
        CLAUSE.model_copy(update={"page": 4}),  # 合同只有 3 页
        CLAUSE.model_copy(update={"page": None}),  # 文件指针须带页码
        CLAUSE.model_copy(update={"source_id": "DOC-NOPE"}),  # 文件未登记
        STANDARD.model_copy(update={"source_id": "ASA999-P1"}),  # 准则片段未注册
        EvidencePointer(type=EvidenceType.external, source_type="trail", source_id="TR-404", locator="status"),
    ],
)
async def test_unresolvable_pointer_marks_its_step(bad: EvidencePointer) -> None:
    report = await check(with_step(cutoff_draft(), 1, evidence=[bad]))
    assert report.level_failed is None and report.unsupported_steps == [1]
    assert len(report.errors) == 1 and report.errors[0].startswith("第 1 步指针")
    # 解析不了的指针原样保留
    assert report.output.reasoning_chain[1].evidence == [bad]


async def test_step_with_one_bad_pointer_among_good_is_unsupported() -> None:
    bad = DELIVERY.model_copy(update={"source_id": "DN-9999"})
    report = await check(with_step(cutoff_draft(), 1, evidence=[DELIVERY, bad]))
    assert report.level_failed is None and report.unsupported_steps == [1]


async def test_bad_pointer_only_in_evidence_list_is_reported_without_step() -> None:
    draft = cutoff_draft()
    bad = DELIVERY.model_copy(update={"source_id": "DN-9999"})
    report = await check(draft.model_copy(update={"evidence": [*draft.evidence, bad]}))
    assert report.level_failed is None and report.unsupported_steps == []
    assert report.errors == ["evidence 中指针 delivery_note DN-9999: 查无此行"]


async def test_no_resolvable_pointer_is_reference_failure() -> None:
    gone = {
        "sales_invoice": "SI-9999",
        "delivery_note": "DN-9999",
        "document": "DOC-NOPE",
        "standard": "ASA999-P1",
    }

    def broken(p: EvidencePointer) -> EvidencePointer:
        return p.model_copy(update={"source_id": gone[p.source_type]})

    draft = cutoff_draft()
    chain = [s.model_copy(update={"evidence": [broken(p) for p in s.evidence]}) for s in draft.reasoning_chain]
    report = await check(
        draft.model_copy(update={"reasoning_chain": chain, "evidence": [broken(p) for p in draft.evidence]})
    )
    assert report.level_failed == "reference" and not report.retryable
    assert report.unsupported_steps == [0, 1, 2, 3]
    assert report.errors[-1] == "没有一个能解析的证据指针"


async def test_trail_pointer_must_be_external_call() -> None:
    """留痕指针只认本项目的外部调用; 可靠性按留痕记下的事实定 (需求 R289): 真查了登记的是外部原件, 记下 "没有查询"
    (payload available false) 的是内部原件, 不采信模型填写的值."""
    trail = FakeTrail()
    lookup = await trail.record(TrailEntry(engagement_id=EID, kind=TrailKind.external_call, actor="A2", summary="ABN"))
    decision = await trail.record(TrailEntry(engagement_id=EID, kind=TrailKind.decision, actor="U-1", summary="决定"))
    other = await trail.record(
        TrailEntry(engagement_id="E-OTHER", kind=TrailKind.external_call, actor="A2", summary="x")
    )
    unqueried = await trail.record(
        TrailEntry(
            engagement_id=EID,
            kind=TrailKind.external_call,
            actor="A2",
            summary="ABN Lookup: not_configured",
            payload={"abn": "68900042015", "available": False, "reason": "not_configured", "result": None},
        )
    )

    def at(trail_id: str) -> EvidencePointer:
        return EvidencePointer(type=EvidenceType.voucher, source_type="trail", source_id=trail_id, locator="status")

    draft = with_step(cutoff_draft(), 0, evidence=[at(lookup)])
    draft = with_step(draft, 1, evidence=[at(decision)])
    draft = with_step(draft, 2, evidence=[at(other)])
    unqueried_pointer = at(unqueried).model_copy(
        update={"type": EvidenceType.external, "reliability": Reliability.external_original}  # 模型填的不采信
    )
    draft = with_step(draft, 3, evidence=[unqueried_pointer])
    report = await check(draft, trail=trail)
    assert report.unsupported_steps == [1, 2]
    [pointer] = report.output.reasoning_chain[0].evidence
    assert (pointer.type, pointer.reliability) == (EvidenceType.external, Reliability.external_original)
    [pointer] = report.output.reasoning_chain[3].evidence
    assert (pointer.type, pointer.reliability) == (EvidenceType.voucher, Reliability.internal_original)
    assert "不是外部调用" in report.errors[0] and "留痕不存在" in report.errors[1]


# ── 客户画像作证据 (需求 R128): 只认上下文中本项目采用版本的画像 ──

PROFILE_POINTER = EvidencePointer(  # 模型填的 type 与 reliability 故意写错
    type=EvidenceType.external,
    source_type="client_profile",
    source_id="CLIENT-XERO-3C1332BB-Q1",
    locator="related_parties",
    reliability=Reliability.external_original,
)


@pytest.mark.external
@pytest.mark.parametrize(
    ("pointer", "with_profile", "reason"),
    [
        (PROFILE_POINTER, True, None),
        (PROFILE_POINTER.model_copy(update={"locator": "related_party_list"}), True, "不是客户画像的字段"),
        (PROFILE_POINTER.model_copy(update={"source_id": "CLIENT-001"}), True, "不是本项目采用的客户画像"),
        (PROFILE_POINTER, False, "校验上下文没有客户画像"),
    ],
)
async def test_client_profile_pointer_resolves_only_against_the_context(
    pointer: EvidencePointer, with_profile: bool, reason: str | None
) -> None:
    """client_profile 指针: 上下文给了本项目采用的画像 (Xero 演示画像 v1) 时, ID 相同且 locator 是画像的顶层字段才
    解析通过, 改写为 voucher, internal_copy (管理层申报, 不采信模型填的); 否则该步 unsupported, 同行指针. 校验器
    没有项目存储 (make_validator), 只能用上下文里的画像. 防管理层申报被当成外部证据, 或引用别的客户或别的字段."""
    ctx = ValidationContext(client_profile=xero_profile() if with_profile else None)
    report = await check(with_step(cutoff_draft(), 1, evidence=[pointer]), ctx=ctx)
    if reason is None:
        assert report.unsupported_steps == [] and report.errors == []
        [resolved] = report.output.reasoning_chain[1].evidence
        assert (resolved.type, resolved.reliability) == (EvidenceType.voucher, Reliability.internal_copy)
    else:
        assert report.unsupported_steps == [1] and report.output.reasoning_chain[1].evidence == [pointer]
        assert len(report.errors) == 1 and reason in report.errors[0]


@pytest.mark.external
async def test_judge_passes_the_client_profile_to_the_validator() -> None:
    """JudgmentEngine.judge 把 JudgmentContext.client_profile 交给校验: 引用画像的步骤 supported; 上下文不给画像时同一
    草稿该步 unsupported. 防判断引擎丢掉画像, 引用管理层申报的判断全都站不住."""
    draft = with_step(cutoff_draft(), 1, evidence=[PROFILE_POINTER])
    statuses = []
    for profile in (xero_profile(), None):
        financial, trail = FakeFinancialStore(list(NUMBAT_CONTRACTS), NUMBAT_RECORDS), FakeTrail()
        llm = ScriptedLLM({"SI-2026-0612": [draft.model_dump_json()]})
        engine = JudgmentEngine(llm, OutputValidator(financial, LIBRARY, trail), trail, LIBRARY)
        invoice = {"source_type": "sales_invoice", **NUMBAT_RECORDS[("sales_invoice", "SI-2026-0612")]}
        data = {
            "invoice": json.loads(json.dumps(invoice, default=str)),
            "delivery_notes": [],
            "journal_lines": [],
            "contract": None,
            "document": None,
            "contract_judgment": None,
        }
        task = {"params": {"period_end": "2026-06-30", "window_days": 20, "threshold_cents": 9000000}, "risks": []}
        jctx = JudgmentContext(
            subject_id="SI-2026-0612", task=task, data=data, fixture_key="SI-2026-0612", client_profile=profile
        )
        outcome = await engine.judge(EID, REV02, make_run("REV-02"), jctx, "U-1")
        assert outcome.judgment is not None
        statuses.append(outcome.judgment.reasoning_chain[1].status)
    assert statuses == [StepStatus.supported, StepStatus.unsupported]


async def test_reply_document_pointer_is_external_and_other_document_is_not_evidence() -> None:
    reply = NUMBAT_REPLIES[0]
    memo = CON001.model_copy(update={"id": "DOC-MEMO", "type": DocumentType.other})
    draft = with_step(cutoff_draft(), 2, evidence=[CLAUSE.model_copy(update={"source_id": reply.id, "page": 1})])
    draft = with_step(draft, 1, evidence=[CLAUSE.model_copy(update={"source_id": memo.id})])
    report = await check(draft, documents=[reply, memo])
    [pointer] = report.output.reasoning_chain[2].evidence
    assert (pointer.type, pointer.reliability) == (EvidenceType.external, Reliability.external_original)
    assert report.unsupported_steps == [1] and "other 类文件不作证据" in report.errors[0]


def test_document_evidence_mapping() -> None:
    """文件类型 → 证据种类 (第 5 节文件指针映射表, 12 类); 待审财报与 other 不作证据, 不在表中."""
    assert {
        DocumentType.contract: EvidenceType.contract,
        DocumentType.invoice: EvidenceType.invoice,
        DocumentType.confirmation: EvidenceType.external,
        DocumentType.bank_statement: EvidenceType.bank,
        DocumentType.minutes: EvidenceType.voucher,
        DocumentType.registry_extract: EvidenceType.external,
        DocumentType.engagement_letter: EvidenceType.contract,
        DocumentType.representation_letter: EvidenceType.voucher,
        DocumentType.correspondence: EvidenceType.voucher,
        DocumentType.governance_communication: EvidenceType.voucher,
        DocumentType.agreement: EvidenceType.contract,
        DocumentType.internal_record: EvidenceType.voucher,
    } == DOCUMENT_EVIDENCE
    assert set(DocumentType) - set(DOCUMENT_EVIDENCE) == {DocumentType.financial_statements, DocumentType.other}


# 设计稿第 4 节 29 份文件表逐类各取一份 (Xero 补造目录 manifest 的登记): 文件, 所引页, 改写后的证据种类与可靠性
XERO_TYPES = [
    ("documents/evidence/director_loan_agreement.pdf", 2, EvidenceType.contract, Reliability.internal_original),  # 20
    (  # 25
        "documents/evidence/smart_agency_invoice_SM0195-70135.pdf",
        1,
        EvidenceType.invoice,
        Reliability.external_copy,
    ),
    ("documents/evidence/fixed_asset_register.pdf", 1, EvidenceType.voucher, Reliability.internal_original),  # 16
    ("documents/evidence/diisr_email.pdf", 1, EvidenceType.voucher, Reliability.external_copy),  # 24
    ("registry/extracts/900043030.pdf", 2, EvidenceType.external, Reliability.external_original),  # 3
    ("documents/engagement/engagement_letter.pdf", 3, EvidenceType.contract, Reliability.external_copy),  # 2
    ("documents/engagement/appointment.pdf", 4, EvidenceType.voucher, Reliability.internal_copy),  # 1
    ("representation/representation_letter.pdf", 1, EvidenceType.voucher, Reliability.internal_original),  # 29
]


@pytest.mark.external
@pytest.mark.parametrize(("path", "page", "evidence_type", "reliability"), XERO_TYPES)
async def test_xero_document_pointer_takes_type_and_reliability_from_registration(
    path: str, page: int, evidence_type: EvidenceType, reliability: Reliability
) -> None:
    document = xero_document(path)
    draft = with_step(cutoff_draft(), 2, evidence=[CLAUSE.model_copy(update={"source_id": document.id, "page": page})])
    report = await check(draft, documents=[document])
    assert report.unsupported_steps == [] and report.errors == []
    [pointer] = report.output.reasoning_chain[2].evidence
    assert (pointer.type, pointer.reliability) == (evidence_type, reliability)


async def test_bank_statement_pointer_is_bank() -> None:
    """补造目录没有对账单 PDF, 用一份合成登记 (bank_statement, external_copy)."""
    statement = CON001.model_copy(
        update={"id": "DOC-STATEMENT", "type": DocumentType.bank_statement, "reliability": Reliability.external_copy}
    )
    draft = with_step(cutoff_draft(), 2, evidence=[CLAUSE.model_copy(update={"source_id": statement.id, "page": 1})])
    report = await check(draft, documents=[statement])
    assert report.errors == []
    [pointer] = report.output.reasoning_chain[2].evidence
    assert (pointer.type, pointer.reliability) == (EvidenceType.bank, Reliability.external_copy)


@pytest.mark.external
def test_xero_types_cover_every_evidence_document_type() -> None:
    """逐类样例, 加上另有用例的收入合同与回函, 没有补造文件的治理层沟通与用合成登记的对账单, 覆盖可作证据的类型."""
    covered = {xero_document(path).type for path, *_ in XERO_TYPES}
    rest = {DocumentType.contract, DocumentType.confirmation, DocumentType.governance_communication}
    assert covered | rest | {DocumentType.bank_statement} == set(DOCUMENT_EVIDENCE)


@pytest.mark.external
async def test_xero_agreement_page_outside_and_statements_not_evidence() -> None:
    loan = xero_document("documents/evidence/director_loan_agreement.pdf")  # 20, 2 页
    statements = xero_document("financial_statements/fs_v1.pdf")  # 28
    assert (loan.type, loan.page_count, statements.type) == (
        DocumentType.agreement,
        2,
        DocumentType.financial_statements,
    )
    draft = with_step(cutoff_draft(), 2, evidence=[CLAUSE.model_copy(update={"source_id": loan.id, "page": 3})])
    draft = with_step(draft, 1, evidence=[CLAUSE.model_copy(update={"source_id": statements.id, "page": 1})])
    report = await check(draft, documents=[loan, statements])
    assert report.unsupported_steps == [1, 2]
    assert "financial_statements 类文件不作证据" in report.errors[0] and "页码 3 不在 1-2 之间" in report.errors[1]


@pytest.mark.parametrize(
    ("source_type", "source_id", "expected"),
    [
        ("journal_entry", "CR-SI-2026-0547-1", (EvidenceType.voucher, Reliability.internal_original)),
        ("sales_invoice", "SI-2026-0612", (EvidenceType.invoice, Reliability.internal_original)),
        ("delivery_note", "DN-2026-0612", (EvidenceType.delivery, Reliability.internal_original)),
        ("bank_transaction", "BT-01-20260630-03", (EvidenceType.bank, Reliability.external_copy)),
        ("bank_account", "BA-01", (EvidenceType.bank, Reliability.external_copy)),
        ("trial_balance", "1010", (EvidenceType.voucher, Reliability.internal_original)),
    ],
)
async def test_every_row_type_maps_per_rule_engine_table(
    source_type: str, source_id: str, expected: tuple[EvidenceType, Reliability]
) -> None:
    validator, financial = make_validator()
    financial.records[("trial_balance", "1010")] = {"account_code": "1010", "balance_cents": 125840000}
    pointer = row(source_type, source_id).model_copy(update={"reliability": Reliability.internal_copy})
    draft = with_step(
        cutoff_draft(proposed_action=ProposedAction.accept, misstatement_draft=None), 0, evidence=[pointer]
    )
    report = await validator.validate(EID, REV02, respond(draft), ValidationContext())
    assert report.level_failed is None
    [resolved] = report.output.reasoning_chain[0].evidence
    assert (resolved.type, resolved.reliability) == expected


async def test_each_source_is_looked_up_once() -> None:
    validator, financial = make_validator()
    report = await validator.validate(EID, REV02, respond(cutoff_draft()), ValidationContext())
    assert report.level_failed is None
    assert financial.record_calls == [("sales_invoice", "SI-2026-0612"), ("delivery_note", "DN-2026-0612")]


# ── 错报金额 ─────────────────────────────────────────


def misstated(amount: int, pointers: list[EvidencePointer] | None = None) -> JudgmentDraft:
    draft = cutoff_draft(misstatement_draft=NUMBAT_CUTOFF_DRAFT.model_copy(update={"amount_cents": amount}))
    if pointers is None:
        return draft
    return with_step(draft, 0, evidence=pointers).model_copy(update={"evidence": [*draft.evidence, *pointers]})


def row(source_type: str, source_id: str) -> EvidencePointer:
    return EvidencePointer(type=EvidenceType.bank, source_type=source_type, source_id=source_id, locator="amount_cents")  # type: ignore[arg-type]


B01 = AuditException(  # CSH-05 的余额不符例外 (合成数据集设计第 7 节第 12 行), 金额带符号
    id="X-CSH-05-1-B01-1",
    procedure_id="CSH-05",
    run_id="RUN-CSH-05-1",
    rule_id="B01",
    severity="high",
    source_ids=["BA-01"],
    amount_cents=-5000000,
    evidence=[],
)


# 需求 R100 验收 3: 含 GST 的合成发票 (测试内联): 发票 715000, 收入行贷 650000, 应收行借 715000, GST 贷 65000
SI_T = SalesInvoice(
    id="SI-T",
    invoice_no="SI-T",
    customer="Test Customer Pty Ltd",
    customer_abn=None,
    contract_id=None,
    invoice_date=date(2026, 6, 29),
    posting_date=date(2026, 6, 29),
    amount_cents=715000,
    journal_entry_id="XJ-T-1",
)
XJ_T = [
    JournalEntry(
        id=f"XJ-T-{n}",
        entry_no="XJ-T",
        posting_date=date(2026, 6, 29),
        created_at=None,
        created_by=None,
        approved_by=None,
        account_code=code,
        debit_cents=debit,
        credit_cents=credit,
        description="Sales invoice SI-T",
        source_doc="SI-T",
    )
    for n, code, debit, credit in ((1, "4100", 0, 650000), (2, "1100", 715000, 0), (3, "2200", 0, 65000))
]


@pytest.mark.parametrize(
    ("amount", "cite_line", "passes"),
    [
        (650000, True, True),  # 不含 GST 的收入行金额, 引了该行
        (650000, False, False),  # 同一金额只引发票: 引用的行里没有 650000
        (715000, False, True),  # 发票金额, 引发票
    ],
)
async def test_misstatement_amount_from_cited_revenue_line(amount: int, cite_line: bool, passes: bool) -> None:
    """REV-02 错报金额取收入行 (不含 GST) 时须引用该分录行 (真实判断引擎与 ScriptedLLM): 被引用的行金额计入可取值,
    不引用不计 (不放宽校验)."""
    records = dict(NUMBAT_RECORDS) | {("sales_invoice", SI_T.id): SI_T.model_dump()}
    records |= {("journal_entry", x.id): x.model_dump() for x in XJ_T}
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS), records)
    trail = FakeTrail()
    invoice = INVOICE.model_copy(update={"source_id": SI_T.id})
    line = EvidencePointer(
        type=EvidenceType.voucher, source_type="journal_entry", source_id="XJ-T-1", locator="credit_cents"
    )
    cited = [invoice, line] if cite_line else [invoice]
    draft = with_step(
        cutoff_draft(misstatement_draft=NUMBAT_CUTOFF_DRAFT.model_copy(update={"amount_cents": amount})),
        0,
        evidence=cited,
    )
    draft = draft.model_copy(update={"evidence": [*cited, DELIVERY, CLAUSE, STANDARD]})
    llm = ScriptedLLM({SI_T.id: [draft.model_dump_json()]})
    engine = JudgmentEngine(llm, OutputValidator(financial, LIBRARY, trail), trail, LIBRARY)
    lines = [
        {"source_type": "journal_entry", **x.model_dump(mode="json"), "account_name": None, "account_category": None}
        for x in XJ_T
    ]
    data = {
        "invoice": {"source_type": "sales_invoice", **SI_T.model_dump(mode="json")},
        "delivery_notes": [],
        "journal_lines": lines,
        "contract": None,
        "document": None,
        "contract_judgment": None,
    }
    task = {"params": {"period_end": "2026-06-30", "window_days": 20, "threshold_cents": 9000000}, "risks": []}
    jctx = JudgmentContext(subject_id=SI_T.id, task=task, data=data, fixture_key=SI_T.id)
    outcome = await engine.judge(EID, REV02, make_run("REV-02"), jctx, "U-1")
    [entry] = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    if passes:
        assert outcome.judgment is not None and entry.payload["level_failed"] is None
    else:
        assert outcome.judgment is None and entry.payload["level_failed"] == "reference"
        assert any("错报金额 650000" in error for error in entry.payload["errors"])


async def test_misstatement_amount_not_traceable_is_reference_failure() -> None:
    report = await check(misstated(14000000))
    assert report.level_failed == "reference" and not report.retryable
    assert report.errors == ["错报金额 14000000 不等于引用行的金额, 两条引用行之差或归因例外的金额"]


async def test_misstatement_amount_may_equal_attributed_exception_by_absolute_value() -> None:
    assert (await check(misstated(5000000))).level_failed == "reference"
    report = await check(misstated(5000000), ctx=ValidationContext(rule_findings=[B01]))
    assert report.level_failed is None


@pytest.mark.parametrize(
    ("amount", "passes"),
    [
        (5000000, True),  # 分录行借方金额
        (1555000, True),  # 流水金额取绝对值
        (3445000, True),  # 两行之差: 5000000 - 1555000
        (6555000, False),  # 带符号相减不算
        (120840000, False),  # 交易后余额不是这一行的金额
    ],
)
async def test_misstatement_amount_from_rows_or_their_difference(amount: int, passes: bool) -> None:
    pointers = [row("journal_entry", "CR-SI-2026-0547-1"), row("bank_transaction", "BT-01-20260630-03")]
    report = await check(misstated(amount, pointers))
    assert (report.level_failed is None) is passes


# ── 独立复核第一轮的错报草稿 (R150, 只记不判失败) ──

GEN09 = LIBRARY.load("GEN-09")
CR0547 = row("journal_entry", "CR-SI-2026-0547-1")  # 借方 5000000
BT0630 = row("bank_transaction", "BT-01-20260630-03")  # 金额 -1555000


def critic_output(
    action: ProposedAction | None, amount: int | None, cited: EvidencePointer = CR0547
) -> CriticIndependent:
    """第一轮输出: 推理链只引用 cited; 给了 amount 时带错报草稿 (Cash, Cutoff, overstated)."""
    draft = NUMBAT_CUTOFF_DRAFT.model_copy(update={"account": "Cash", "amount_cents": amount}) if amount else None
    chain = [
        ReasoningStep(kind=StepKind.fact, text="The receipt was debited on 30 June.", evidence=[cited]),
        ReasoningStep(kind=StepKind.inference, text="So cash is overstated.", from_steps=[0]),
    ]
    return CriticIndependent(
        conclusion="Cash is overstated.", reasoning_chain=chain, proposed_action=action, misstatement_draft=draft
    )


async def critic_check(output: CriticIndependent, findings: Sequence[AuditException] = (), drafts: bool = True) -> Any:
    """第一轮的校验: 给出 CR0547 与 BT0630 两行; drafts 为真 (功能 collab 打开) 时核对草稿."""
    validator, _ = make_validator()
    ctx = ValidationContext(allowed_evidence=[CR0547, BT0630], rule_findings=list(findings), critic_drafts=drafts)
    return await validator.validate(EID, GEN09, respond(output), ctx)


@pytest.mark.parametrize(
    ("action", "amount", "findings", "kept", "error"),
    [
        (ProposedAction.misstatement, 5000000, [], True, None),  # 引用行的借方金额
        (ProposedAction.misstatement, -5000000, [], True, None),  # 草稿金额为负数时按绝对值核对
        (ProposedAction.misstatement, 1555000, [], True, None),  # 第一轮给出而推理链没引用的行也算, 按绝对值
        (ProposedAction.misstatement, 3445000, [], True, None),  # 两行之差
        (ProposedAction.misstatement, 777, [B01.model_copy(update={"amount_cents": -777})], True, None),  # 例外金额
        (ProposedAction.misstatement, 4000000, [B01], False, "独立复核的错报金额 4000000 不等于"),
        (ProposedAction.misstatement, None, [], False, "独立复核建议认定错报, 但没有错报草稿"),
        (ProposedAction.close, 5000000, [], False, "独立复核建议 close, 却给了错报草稿"),
        (None, 5000000, [], False, "独立复核没有给建议动作, 却给了错报草稿"),
        (ProposedAction.close, None, [], False, None),
        (None, None, [], False, None),  # 旧输出: 没有这两项
    ],
)
async def test_critic_draft_checked_only_noted(
    action: ProposedAction | None, amount: int | None, findings: list[AuditException], kept: bool, error: str | None
) -> None:
    """建议 misstatement 而无草稿, 不建议 (或没给建议动作) 却给了, 或金额 (口径同 A6 的草稿, 按绝对值) 对不上第一轮
    给出的行与该判断解释的例外: 草稿不写回, 问题记进 notes, 不进推理链的 errors; 都不判失败. 对上则保留."""
    output = critic_output(action, amount)
    report = await critic_check(output, findings)
    assert report.level_failed is None
    assert report.output.misstatement_draft == (output.misstatement_draft if kept else None)
    assert report.output.proposed_action is action
    assert [n for n in report.notes if n.startswith(error)] if error else report.notes == [], report.notes
    assert not [e for e in report.errors if "草稿" in e], report.errors


async def test_critic_draft_not_checked_when_collab_off() -> None:
    """功能 collab 关闭时不核对草稿 (第 1 轮审核 R150-04): 对不上的金额与不该给的草稿都不记问题."""
    for output in (critic_output(ProposedAction.misstatement, 4000000), critic_output(ProposedAction.close, 5000000)):
        report = await critic_check(output, drafts=False)
        assert (report.level_failed, report.notes) == (None, [])


def test_old_critic_output_without_new_keys_parses() -> None:
    """R150 验收 1: 现有 GEN-09/BA-01-r1 (没有 proposed_action 与 misstatement_draft) 照样通过结构校验, 两项为空;
    输出模型的 JSON schema 可生成且含这两项."""
    raw = (FIXTURES / "GEN-09" / "BA-01-r1.json").read_text(encoding="utf-8")
    parsed = CriticIndependent.model_validate_json(raw)
    assert (parsed.proposed_action, parsed.misstatement_draft) == (None, None)
    assert {"proposed_action", "misstatement_draft"} <= set(CriticIndependent.model_json_schema()["properties"])


def test_supported_amounts_skip_zero_and_bool() -> None:
    rows = [{"amount_cents": 100, "flag_cents": True}, {"amount_cents": -100, "zero_cents": 0}]
    assert supported_amounts(rows, []) == {100}


# ── 语义 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda d: d.model_copy(update={"reasoning_chain": []}), "推理链为空"),
        (lambda d: with_step(d, 0, evidence=[]), "第 0 步 fact 没有证据指针"),
        (lambda d: with_step(d, 3, evidence=[]), "第 3 步 standard 没有证据指针"),
        (lambda d: with_step(d, 3, evidence=[INVOICE]), "第 3 步 standard 没有指向准则片段"),
        (lambda d: with_step(d, 4, from_steps=[0, 4]), "第 4 步 inference 引用了非前序步骤 [4]"),
        (lambda d: with_step(d, 2, kind=StepKind.inference, from_steps=[3]), "第 2 步 inference 引用了非前序步骤 [3]"),
        (lambda d: with_step(d, 4, from_steps=[-1]), "第 4 步 inference 引用了非前序步骤 [-1]"),
        (lambda d: with_step(d, 4, from_steps=[]), "第 4 步 inference 没有引用前序步骤"),
        (lambda d: d.model_copy(update={"misstatement_draft": None}), "建议认定错报, 但没有错报草稿"),
    ],
)
async def test_semantic_failures(change: Any, error: str) -> None:
    report = await check(change(cutoff_draft()))
    assert report.level_failed == "semantic" and not report.retryable
    assert error in report.errors


async def test_semantic_errors_follow_pointer_errors() -> None:
    draft = with_step(cutoff_draft(), 1, evidence=[DELIVERY.model_copy(update={"source_id": "DN-9999"})])
    report = await check(with_step(draft, 4, from_steps=[]))
    assert report.level_failed == "semantic" and report.unsupported_steps == [1]
    assert report.errors == ["第 1 步指针 delivery_note DN-9999: 查无此行", "第 4 步 inference 没有引用前序步骤"]


async def test_other_actions_need_no_misstatement_draft() -> None:
    draft = cutoff_draft(proposed_action=ProposedAction.more_evidence, misstatement_draft=None)
    assert (await check(draft)).level_failed is None


# ── 抽取 ─────────────────────────────────────────


def con001() -> ContractExtraction:
    raw = (FIXTURES / "DOC-04" / "CON-001.json").read_text(encoding="utf-8")
    return ContractExtraction.model_validate_json(raw)


async def extract(output: Any, document: DocumentRef | None = CON001, procedure: str = "DOC-04") -> Any:
    validator, _ = make_validator()
    return await validator.validate(EID, LIBRARY.load(procedure), respond(output), ValidationContext(document=document))


async def test_contract_extraction_passes_and_takes_reliability_from_document() -> None:
    report = await extract(con001())
    assert report.level_failed is None and report.errors == []
    assert {(p.type, p.reliability) for p in report.output.evidence} == {
        (EvidenceType.contract, Reliability.external_copy)
    }
    assert report.output.terms == con001().terms


@pytest.mark.parametrize(
    "change",
    [
        {"page": 4},  # 合同只有 3 页
        {"page": 0},  # 页码从 1 起
        {"page": None},
        {"source_id": NUMBAT_CONTRACTS[1].id},  # 别的合同
        {"source_type": "standard"},
    ],
)
async def test_extraction_pointer_outside_document_fails_whole_output(change: dict[str, Any]) -> None:
    out = con001()
    evidence = [out.evidence[0].model_copy(update=change), *out.evidence[1:]]
    report = await extract(out.model_copy(update={"evidence": evidence}))
    assert report.level_failed == "reference" and not report.retryable
    assert report.unsupported_steps == [] and len(report.errors) == 1


async def test_extraction_term_page_outside_document_fails() -> None:
    out = con001()
    terms = [*out.terms, ExtractedTerm(term_type=TermType.termination, text="x", page=9)]
    report = await extract(out.model_copy(update={"terms": terms}))
    assert report.level_failed == "reference" and report.errors == ["条款 termination 页码 9 不在 1-3 之间"]


async def test_extraction_without_pointers_fails() -> None:
    report = await extract(con001().model_copy(update={"evidence": []}))
    assert report.level_failed == "reference" and report.errors == ["没有证据指针"]


async def test_extraction_needs_document_in_context() -> None:
    with pytest.raises(ValueError, match=r"ValidationContext\.document"):
        await extract(con001(), document=None)


async def test_confirmation_extraction_points_at_reply() -> None:
    reply = NUMBAT_REPLIES[0]
    pointer = EvidencePointer(
        type=EvidenceType.contract,
        source_type="document",
        source_id=reply.id,
        locator="confirmed_balance_cents",
        page=1,
    )
    out = ConfirmationExtraction(
        confirming_party="Harbourline Bank Limited",
        account_no="12345601",
        confirmed_balance_cents=120840000,
        balance_date=date(2026, 6, 30),
        receipt_channel=None,
        evidence=[pointer],
    )
    report = await extract(out, document=reply, procedure="DOC-06")
    assert report.level_failed is None
    assert [(p.type, p.reliability) for p in report.output.evidence] == [
        (EvidenceType.external, Reliability.external_original)
    ]
    bad = await extract(out.model_copy(update={"evidence": [pointer.model_copy(update={"page": 3})]}), reply, "DOC-06")
    assert bad.level_failed == "reference"


def reply_extraction(**changes: Any) -> ConfirmationExtraction:
    reply = NUMBAT_REPLIES[0]
    pointer = EvidencePointer(
        type=EvidenceType.external, source_type="document", source_id=reply.id, locator="account_no", page=1
    )
    base = ConfirmationExtraction(
        confirming_party="Harbourline Bank Limited",
        account_no="12345601",
        confirmed_balance_cents=120840000,
        balance_date=date(2026, 6, 30),
        receipt_channel=ReceiptChannel.direct_email,
        evidence=[pointer],
    )
    return base.model_copy(update=changes)


def sent_confirmation(account: str = "BA-01", channel: ReceiptChannel = ReceiptChannel.direct_email) -> Confirmation:
    return Confirmation(
        id=f"C-{account}",
        bank_account_id=account,
        confirming_party="Harbourline Bank Limited",
        idempotency_key=f"{EID}:CSH-04:{account}",
        sent_at=datetime(2026, 7, 1, tzinfo=UTC),
        reply_document_id=NUMBAT_REPLIES[0].id,
        receipt_channel=channel,
    )


async def check_reply(output: ConfirmationExtraction, confirmation: Confirmation | None) -> Any:
    validator, _ = make_validator(documents=[NUMBAT_REPLIES[0]])
    ctx = ValidationContext(document=NUMBAT_REPLIES[0], confirmation=confirmation)
    return await validator.validate(EID, LIBRARY.load("DOC-06"), respond(output), ctx)


def accrued_pointer(page: int = 1) -> EvidencePointer:
    """回函应计利息的指针 (本文档)."""
    reply = NUMBAT_REPLIES[0]
    return EvidencePointer(
        type=EvidenceType.external,
        source_type="document",
        source_id=reply.id,
        locator="accrued_interest_cents",
        page=page,
    )


@pytest.mark.parametrize(
    ("accrued", "pointers", "passed"),
    [
        (None, [], True),  # 回函没提: 不要指针
        (0, [accrued_pointer()], True),  # 写 nil 为 0, 同样要指针
        (4853, [accrued_pointer()], True),
        (4853, [], False),  # 非空却没有该 locator 的指针
        (0, [], False),
        (4853, [accrued_pointer(page=3)], False),  # 页码超出页数 (2 页)
    ],
)
async def test_reply_accrued_interest_needs_its_own_pointer(
    accrued: int | None, pointers: list[EvidencePointer], passed: bool
) -> None:
    """需求 R104: 回函应计利息非空时须有 locator 为 accrued_interest_cents 的本文档指针 (页码在页数内), 不成立为
    引用失败 (不重试)."""
    base = reply_extraction()
    output = base.model_copy(update={"accrued_interest_cents": accrued, "evidence": [*base.evidence, *pointers]})
    report = await check_reply(output, sent_confirmation())
    if passed:
        assert report.level_failed is None and report.output.accrued_interest_cents == accrued
    else:
        assert (report.level_failed, report.retryable) == ("reference", False)


async def test_reply_account_must_match_confirmed_account() -> None:
    assert (await check_reply(reply_extraction(), sent_confirmation())).level_failed is None
    report = await check_reply(reply_extraction(account_no="23456702"), sent_confirmation())
    assert report.level_failed == "semantic" and not report.retryable
    assert report.errors == ["回函账号 23456702 与账户 BA-01 的账号 12345601 不符"]
    missing = await check_reply(reply_extraction(), sent_confirmation("BA-09"))
    assert missing.level_failed == "semantic" and missing.errors == ["被函证的账户 BA-09 不存在"]
    # 不给函证时只做引用级 (DOC-04 等其他抽取)
    assert (await check_reply(reply_extraction(account_no="x"), None)).level_failed is None


async def test_reply_channel_mismatch_is_only_a_note() -> None:
    report = await check_reply(reply_extraction(receipt_channel=ReceiptChannel.portal_via_client), sent_confirmation())
    assert report.level_failed is None and report.errors == []
    assert report.notes == ["回函写明的渠道 portal_via_client 与回执记录的 direct_email 不符, 以回执为准"]
    assert (await check_reply(reply_extraction(receipt_channel=None), sent_confirmation())).notes == []


# ── 报告草稿 ─────────────────────────────────────────


QUALIFIED_DRAFT = ReportDraft(
    opinion="Qualified Opinion\n\nWe have audited ...",
    basis_for_opinion="Basis for Qualified Opinion\n\nRevenue is overstated by $142,000 ...",
    judgment_keys=["REV-02:SI-2026-0612"],
)


def confirmed_judgment(version: int = 2, status: ReviewStatus = ReviewStatus.confirmed) -> Judgment:
    return Judgment(
        id="J-REV-02-1-SI-2026-0612",
        version=version,
        procedure_id="REV-02",
        run_id="RUN-REV-02-1",
        subject_id="SI-2026-0612",
        conclusion="overstated",
        confidence=0.9,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="none",
        proposed_action=ProposedAction.misstatement,
        autonomy_level="L2",
        review_status=status,
        performed_by="U-1",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


async def check_report(
    draft: ReportDraft, judgments: list[Judgment], opinion: OpinionType | None = OpinionType.qualified
) -> Any:
    validator, _ = make_validator()
    ctx = ValidationContext(judgments=judgments, opinion=opinion)
    return await validator.validate(EID, LIBRARY.load("RPT-03"), respond(draft), ctx)


async def test_report_draft_passes() -> None:
    report = await check_report(QUALIFIED_DRAFT, [confirmed_judgment()])
    assert report.level_failed is None and report.errors == [] and report.output == QUALIFIED_DRAFT
    clean = ReportDraft(opinion="Opinion\n\n...", basis_for_opinion="Basis for Opinion\n\n...", judgment_keys=[])
    assert (await check_report(clean, [], OpinionType.unqualified)).level_failed is None


@pytest.mark.parametrize(
    ("judgments", "error"),
    [
        ([], "引用键 REV-02:SI-2026-0612 不是可引用的判断 (可用: 无)"),
        (
            [confirmed_judgment().model_copy(update={"id": "J-REV-02-1-SI-2026-0618", "subject_id": "SI-2026-0618"})],
            "引用键 REV-02:SI-2026-0612 不是可引用的判断 (可用: REV-02:SI-2026-0618)",
        ),
        (
            [confirmed_judgment(status=ReviewStatus.pending)],
            "引用键 REV-02:SI-2026-0612 的判断 J-REV-02-1-SI-2026-0612 尚未确认 (pending)",
        ),
    ],
)
async def test_report_reference_failures(judgments: list[Judgment], error: str) -> None:
    report = await check_report(QUALIFIED_DRAFT, judgments)
    assert report.level_failed == "reference" and not report.retryable and report.errors == [error]


@pytest.mark.parametrize(
    ("changes", "opinion", "error"),
    [
        ({"opinion": "Opinion\n\n..."}, OpinionType.qualified, "意见段首行应为 Qualified Opinion"),
        (
            {"basis_for_opinion": "Basis for Opinion\n\n..."},
            OpinionType.qualified,
            "基础段首行应为 Basis for Qualified Opinion",
        ),
        ({"judgment_keys": []}, OpinionType.qualified, "保留意见须引用形成错报的已确认判断"),
        ({}, OpinionType.unqualified, "意见段首行应为 Opinion"),
    ],
)
async def test_report_semantic_failures(changes: dict[str, Any], opinion: OpinionType, error: str) -> None:
    report = await check_report(QUALIFIED_DRAFT.model_copy(update=changes), [confirmed_judgment()], opinion)
    assert report.level_failed == "semantic" and not report.retryable and error in report.errors


async def test_report_needs_opinion_type() -> None:
    with pytest.raises(ValueError, match=r"ValidationContext\.opinion"):
        await check_report(QUALIFIED_DRAFT, [confirmed_judgment()], None)


async def test_report_syntax_failure_is_retryable() -> None:
    validator, _ = make_validator()
    report = await validator.validate(EID, LIBRARY.load("RPT-03"), respond(None, raw="{"), ValidationContext())
    assert report.level_failed == "syntax" and report.retryable


async def test_report_headings_tolerate_surrounding_whitespace_and_crlf() -> None:
    draft = QUALIFIED_DRAFT.model_copy(
        update={
            "opinion": " \n Qualified Opinion\r\n\r\nbody\n ",
            "basis_for_opinion": "\n Basis for Qualified Opinion\r\n\r\nbody ",
        }
    )
    assert (await check_report(draft, [confirmed_judgment()])).level_failed is None


async def test_report_reference_failure_reported_before_headings() -> None:
    report = await check_report(QUALIFIED_DRAFT.model_copy(update={"opinion": "wrong"}), [])
    assert report.level_failed == "reference"
    assert report.errors == ["引用键 REV-02:SI-2026-0612 不是可引用的判断 (可用: 无)"]


async def test_report_may_cite_human_replacement_but_not_overridden_original() -> None:
    """审计师 override 后: 人工替代判断 -H1 (confirmed) 可引用, 被推翻的原判断 (overridden) 不可."""
    original = confirmed_judgment(version=1, status=ReviewStatus.pending)
    task = ReviewQueue(LIBRARY).make_task(TaskKind.normal, original, "REV-02", original.run_id, NOW)
    record = ReviewRecord(
        id="D-OVERRIDE",
        task_id=task.id,
        target_version=1,
        decision=Decision.override,
        reason="人工复核",
        override_conclusion="人工替代结论",
        reviewed_by="U-1",
        decided_at=NOW,
    )
    old, human = override_effects(record, task, original, [], {original.id})
    assert isinstance(old, Judgment) and isinstance(human, Judgment) and human.id.endswith("-H1")
    assert judgment_key(human) == "REV-02:SI-2026-0612:H1" and judgment_key(old) == "REV-02:SI-2026-0612"
    cites_human = QUALIFIED_DRAFT.model_copy(update={"judgment_keys": ["REV-02:SI-2026-0612:H1"]})
    assert (await check_report(cites_human, [old, human])).level_failed is None
    report = await check_report(QUALIFIED_DRAFT, [old, human])
    assert report.level_failed == "reference" and "尚未确认 (overridden)" in report.errors[0]


def test_citation_keys_must_be_unique() -> None:
    """引用键的不变式: 可引用的判断里键不能重复 (同一程序同一对象两次执行的 AI 判断); 撞键时抛错, 写明键与判断."""
    first = confirmed_judgment()
    second = first.model_copy(update={"id": "J-REV-02-2-SI-2026-0612", "run_id": "RUN-REV-02-2", "version": 1})
    assert list(citation_keys([first])) == ["REV-02:SI-2026-0612"]
    with pytest.raises(ValueError, match="REV-02:SI-2026-0612: J-REV-02-1-SI-2026-0612, J-REV-02-2-SI-2026-0612"):
        citation_keys([first, second])
    odd = first.model_copy(update={"id": "J-CUSTOM"})
    assert judgment_key(odd) == "REV-02:SI-2026-0612:J-CUSTOM"
