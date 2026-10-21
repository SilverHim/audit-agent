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
    AuditPlanDraft,
    ConfirmationExtraction,
    ContractExtraction,
    CorrectionVerificationDraft,
    CriticIndependent,
    CriticVerdictOut,
    DocumentClassification,
    ExtractedTerm,
    GoingConcernAdvice,
    JudgmentDraft,
    MisstatementClassificationDraft,
    MisstatementNature,
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
    EqrRecommendation,
    EvidencePointer,
    EvidenceType,
    GoingConcernOutcome,
    Judgment,
    JudgmentRef,
    Milestone,
    MisstatementType,
    OpinionType,
    ProcedureNote,
    ProposedAction,
    ReasoningStep,
    ReceiptChannel,
    Reliability,
    ReviewRecord,
    ReviewStatus,
    StepKind,
    TaskKind,
    TermType,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import LLMResponse
from backend.contracts.records import Account, JournalEntry, SalesInvoice
from backend.contracts.services import JudgmentContext, ValidationContext
from backend.core.config import get_settings
from backend.orchestration.decisions import override_effects
from backend.services.judgment_engine import (
    DOCUMENT_EVIDENCE,
    MAX_ATTEMPTS,
    OUTCOME_STANDARDS,
    PLAN_REQUIRED_SECTIONS,
    CheckResult,
    JudgmentEngine,
    OutputValidator,
    check_plan,
    citation_keys,
    going_concern_semantics,
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
from tests.fixture_sets import XERO
from tests.xero_fakes import xero_adjustments, xero_financial_store, xero_profile

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


def with_step[M: JudgmentDraft | GoingConcernAdvice](draft: M, index: int, **changes: Any) -> M:
    chain = list(draft.reasoning_chain)
    chain[index] = chain[index].model_copy(update=changes)
    return draft.model_copy(update={"reasoning_chain": chain})


async def check(output: Any, raw: str | None = None, ctx: ValidationContext | None = None, **kw: Any) -> Any:
    validator, _ = make_validator(**kw)
    return await validator.validate(EID, REV02, respond(output, raw), ctx or ValidationContext(standards_of=None))


# ── 签名与上下文 ─────────────────────────────────────────


def test_validation_context_defaults() -> None:
    ctx = ValidationContext(standards_of=None)
    assert ctx.rule_findings == [] and ctx.document is None and ctx.judgments == [] and ctx.required_refs == []


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


# 模板已引用而引用与语义校验还没实现的输出模型 → 实现它的需求. 这些模型的程序还没有 Agent 执行 (不在启用集与
# 注册表中); 实现时登记校验并从表里删掉 (test_pending_checks_are_not_registered 守着). AuditPlanDraft 已由需求 R204 登记
PENDING_CHECKS: dict[type[BaseModel], str] = {}


def test_every_referenced_output_model_is_registered() -> None:
    """引用方用到的输出模型都已登记引用与语义校验: 程序库全部模板 (含未启用的) 的 llm.output_model, 加上代码里的两份
    规格 (上传文件分类 ingest.CLASSIFY, 独立复核第二轮 review.VERDICT_SPEC). 新加输出模型而漏登记时在此报出,
    不至于在运行时被兜底转人工 (如上传分类转 needs_manual). 待实现表 PENDING_CHECKS 中的是已知例外."""
    referenced = {output_model(t) for t in LIBRARY.all(include_disabled=True) if t.llm is not None}
    referenced |= {output_model(ingest.CLASSIFY)}
    referenced |= {getattr(llm_outputs, review.VERDICT_SPEC.output_model)}
    missing = [m for m in referenced if m not in OutputValidator.CHECKS and m not in PENDING_CHECKS]
    assert sorted(m.__name__ for m in missing) == []


def test_pending_checks_are_not_registered() -> None:
    """待实现表只放还没登记的模型: 登记了 (需求实现了) 就得从表里删掉, 不留过期的例外; 表里的模型都有模板引用."""
    assert not set(PENDING_CHECKS) & set(OutputValidator.CHECKS)
    referenced = {output_model(t) for t in LIBRARY.all(include_disabled=True) if t.llm is not None}
    assert set(PENDING_CHECKS) <= referenced


def test_dispatch_table_covers_existing_models() -> None:
    """现有 13 种输出模型都已登记引用与语义校验 (CriticVerdictOut, DocumentClassification 与 SheetRecognition 只做
    语法与结构; RegistryExtraction 与合同, 回函共用抽取校验, 需求 R290; AuditPlanDraft 由
    check_plan 校验, 需求 R204; FIN-03 的 MisstatementClassificationDraft 另查错报性质, 需求 R157; FIN-09 的
    CorrectionVerificationDraft 另查是否完整更正与其他科目, 需求 R164; FIN-02 的 GoingConcernAdvice 由
    check_going_concern 校验, 需求 R183)."""
    assert set(OutputValidator.CHECKS) == {
        AuditPlanDraft,
        GoingConcernAdvice,
        JudgmentDraft,
        MisstatementClassificationDraft,
        CorrectionVerificationDraft,
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
    assert OutputValidator.CHECKS[AuditPlanDraft] is check_plan


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
        await validator.validate(
            EID, probe_template(), respond(ProbeOut(value=1)), ValidationContext(standards_of=None)
        )


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
    ctx = ValidationContext(standards_of=None)
    odd = await validator.validate(EID, probe_template(), respond(ProbeOut(value=3)), ctx)
    assert (odd.level_failed, odd.retryable, odd.errors, odd.notes, odd.unsupported_steps) == (
        "semantic",
        True,
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
    report = await validator.validate(
        EID, REV02, respond(None, raw="", error="fixture_not_found"), ValidationContext(standards_of=None)
    )
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
    # 只标 unsupported 是降级, 算通过, 不重试 (需求 R348)
    assert report.level_failed is None and not report.retryable and report.unsupported_steps == [1]
    assert len(report.errors) == 1 and report.errors[0].startswith("第 1 步指针")
    # 解析不了的指针原样保留
    assert report.output.reasoning_chain[1].evidence == [bad]


# 已登记而不在 REV-02 的 standards 里的片段 (在 CSH-02 的里)
OUTSIDE = STANDARD.model_copy(update={"source_id": "ASA450-P05", "locator": "para 5"})


@pytest.mark.parametrize(
    ("standards_of", "cited", "rejected"),
    [
        ("REV-02", STANDARD, False),  # AASB15-P38 在 REV-02 的 standards 内
        ("REV-02", OUTSIDE, True),
        ("CSH-02", OUTSIDE, False),  # 同一片段在 CSH-02 的 standards 内
        (None, OUTSIDE, False),  # 不给程序时只核登记 (如抽取, 分类与 A7 第二轮)
    ],
)
async def test_standard_must_be_in_the_program_standards(
    standards_of: str | None, cited: EvidencePointer, rejected: bool
) -> None:
    """需求 R301: ctx.standards_of 给了时, 准则片段还须在该程序 YAML 的 standards 内 (提示词只列这些); 不在时同未登记的
    片段: 所在步骤 unsupported, 推理链与 evidence 各记一条错误, 写明片段与程序, 指针原样保留; 整条照常通过."""
    draft = with_step(cutoff_draft(), 3, evidence=[cited])
    draft = draft.model_copy(update={"evidence": [INVOICE, DELIVERY, CLAUSE, cited]})
    report = await check(draft, ctx=ValidationContext(standards_of=standards_of))
    assert report.level_failed is None
    reason = f"standard {cited.source_id}: 准则片段 {cited.source_id} 不在 {standards_of} 的 standards 内"
    assert (report.unsupported_steps, report.errors) == (
        ([3], [f"第 3 步指针 {reason}", f"evidence 中指针 {reason}"]) if rejected else ([], [])
    )
    assert report.output.reasoning_chain[3].evidence[0].source_id == cited.source_id


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
    assert report.level_failed == "reference" and report.retryable
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
    ctx = ValidationContext(client_profile=xero_profile() if with_profile else None, standards_of=None)
    report = await check(with_step(cutoff_draft(), 1, evidence=[pointer]), ctx=ctx)
    if reason is None:
        assert report.unsupported_steps == [] and report.errors == []
        [resolved] = report.output.reasoning_chain[1].evidence
        assert (resolved.type, resolved.reliability) == (EvidenceType.voucher, Reliability.internal_copy)
    else:
        assert report.unsupported_steps == [1] and report.output.reasoning_chain[1].evidence == [pointer]
        assert len(report.errors) == 1 and reason in report.errors[0]


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
    """文件类型 → 证据种类 (第 5 节文件指针映射表, 12 类); 财务报表与 other 不作证据, 不在表中."""
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
    # 审计师自己上传的报表 (fs_v1.pdf 不在补造清单的登记里, Issue #46): 登记为 financial_statements
    statements = loan.model_copy(
        update={"id": "DOC-e2670a8e43ac", "type": DocumentType.financial_statements, "file_name": "fs_v1.pdf"}
    )
    assert (loan.type, loan.page_count) == (DocumentType.agreement, 2)
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
        ("supplier_bill", "PB-1", (EvidenceType.invoice, Reliability.internal_original)),  # 应付账单 (需求 R178)
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
    financial.records[("supplier_bill", "PB-1")] = {"id": "PB-1", "outstanding_cents": 100}
    pointer = row(source_type, source_id).model_copy(update={"reliability": Reliability.internal_copy})
    draft = with_step(
        cutoff_draft(proposed_action=ProposedAction.accept, misstatement_draft=None), 0, evidence=[pointer]
    )
    report = await validator.validate(EID, REV02, respond(draft), ValidationContext(standards_of=None))
    assert report.level_failed is None
    [resolved] = report.output.reasoning_chain[0].evidence
    assert (resolved.type, resolved.reliability) == expected


async def test_each_source_is_looked_up_once() -> None:
    validator, financial = make_validator()
    report = await validator.validate(EID, REV02, respond(cutoff_draft()), ValidationContext(standards_of=None))
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
    不引用不计 (不放宽校验). 不过时回传重试, 同一输出共调 MAX_ATTEMPTS 次 (需求 R348)."""
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
    llm = ScriptedLLM({SI_T.id: [draft.model_dump_json()] * MAX_ATTEMPTS})
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
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert len(calls) == (1 if passes else MAX_ATTEMPTS)
    entry = calls[-1]
    if passes:
        assert outcome.judgment is not None and entry.payload["level_failed"] is None
    else:
        assert outcome.judgment is None and entry.payload["level_failed"] == "reference"
        assert any("错报金额 650000" in error for error in entry.payload["errors"])


async def test_misstatement_amount_not_traceable_is_reference_failure() -> None:
    report = await check(misstated(14000000))
    assert report.level_failed == "reference" and report.retryable
    assert report.errors == ["错报金额 14000000 不等于引用行的金额, 两条引用行之差或归因例外的金额"]


async def test_misstatement_amount_may_equal_attributed_exception_by_absolute_value() -> None:
    assert (await check(misstated(5000000))).level_failed == "reference"
    report = await check(misstated(5000000), ctx=ValidationContext(rule_findings=[B01], standards_of=None))
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


SALES = Account(code="200", name="Sales", type="revenue", category="revenue", category_basis="name")  # Xero 科目表一行


@pytest.mark.parametrize(("code", "passes"), [("200", True), (None, True), ("9999", False)])
async def test_misstatement_account_code_must_be_in_the_chart(code: str | None, passes: bool) -> None:
    """R156 验收 9: REV-02 草稿带科目表有的 account_code 200 通过; 9999 → 引用级失败 (可重试, 需求 R348); 不带代码
    不查 (现有 fixture 照旧). GEN-02 XJ-499 的 Xero fixture (710) 经真实科目表通过见 test_fieldwork_gen02."""
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS), NUMBAT_RECORDS, accounts=[SALES])
    validator = OutputValidator(financial, LIBRARY, FakeTrail())
    draft = cutoff_draft(misstatement_draft=NUMBAT_CUTOFF_DRAFT.model_copy(update={"account_code": code}))
    report = await validator.validate(EID, REV02, respond(draft), ValidationContext(standards_of=None))
    if passes:
        assert report.level_failed is None and report.output.misstatement_draft.account_code == code
    else:
        error = f"错报草稿的科目代码 {code} 不在本项目科目表中"
        assert (report.level_failed, report.retryable, report.errors) == ("reference", True, [error])


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
    ctx = ValidationContext(
        allowed_evidence=[CR0547, BT0630], rule_findings=list(findings), critic_drafts=drafts, standards_of=None
    )
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


@pytest.mark.parametrize(("reviewed", "unsupported"), [("REV-02", [0]), ("CSH-02", [])])
async def test_critic_standard_follows_the_reviewed_program(reviewed: str, unsupported: list[int]) -> None:
    """需求 R301: 独立复核第一轮的准则片段按被复核程序的 standards 核对 (ctx.standards_of): ASA450-P05 在给出的证据内,
    被复核的是 REV-02 时该步仍不受支持, 错误写明片段与程序; 是 CSH-02 (standards 含它) 时照常."""
    chain = [
        ReasoningStep(kind=StepKind.standard, text="Misstatements are accumulated.", evidence=[OUTSIDE]),
        ReasoningStep(kind=StepKind.fact, text="The receipt was debited on 30 June.", evidence=[CR0547]),
        ReasoningStep(kind=StepKind.inference, text="So cash is overstated.", from_steps=[0, 1]),
    ]
    output = CriticIndependent(conclusion="Cash is overstated.", reasoning_chain=chain)
    validator, _ = make_validator()
    ctx = ValidationContext(allowed_evidence=[CR0547, OUTSIDE], standards_of=reviewed)
    report = await validator.validate(EID, GEN09, respond(output), ctx)
    assert report.unsupported_steps == unsupported
    assert [e for e in report.errors if "ASA450-P05" in e] == (
        ["第 0 步指针 standard ASA450-P05: 准则片段 ASA450-P05 不在 REV-02 的 standards 内"] if unsupported else []
    )


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
    assert report.level_failed == "semantic" and report.retryable
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
    return await validator.validate(
        EID, LIBRARY.load(procedure), respond(output), ValidationContext(document=document, standards_of=None)
    )


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
    assert report.level_failed == "reference" and report.retryable
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
    ctx = ValidationContext(document=NUMBAT_REPLIES[0], confirmation=confirmation, standards_of=None)
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
    引用失败 (可重试, 需求 R348)."""
    base = reply_extraction()
    output = base.model_copy(update={"accrued_interest_cents": accrued, "evidence": [*base.evidence, *pointers]})
    report = await check_reply(output, sent_confirmation())
    if passed:
        assert report.level_failed is None and report.output.accrued_interest_cents == accrued
    else:
        assert (report.level_failed, report.retryable) == ("reference", True)


async def test_reply_account_must_match_confirmed_account() -> None:
    assert (await check_reply(reply_extraction(), sent_confirmation())).level_failed is None
    report = await check_reply(reply_extraction(account_no="23456702"), sent_confirmation())
    assert report.level_failed == "semantic" and report.retryable
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
    ctx = ValidationContext(judgments=judgments, opinion=opinion, standards_of=None)
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
    assert report.level_failed == "reference" and report.retryable and report.errors == [error]


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
    assert report.level_failed == "semantic" and report.retryable and error in report.errors


async def test_report_needs_opinion_type() -> None:
    with pytest.raises(ValueError, match=r"ValidationContext\.opinion"):
        await check_report(QUALIFIED_DRAFT, [confirmed_judgment()], None)


async def test_report_syntax_failure_is_retryable() -> None:
    validator, _ = make_validator()
    report = await validator.validate(
        EID, LIBRARY.load("RPT-03"), respond(None, raw="{"), ValidationContext(standards_of=None)
    )
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


# ── 计划叙述 (PLN-15, 需求 R204) ─────────────────────────────────────────

PLN15 = LIBRARY.load("PLN-15")  # 功能 planning, 程序库缺省不启用; load 照样取得
PLAN_PROCEDURES = ["CSH-02", "CSH-04", "CSH-05", "DOC-06", "REV-02", "REV-03", "REV-08"]  # Xero 装配后的 P2 程序
PLAN_END = date(2026, 9, 30)
PLAN_SECTIONS = {name: f"The {name} of the audit." for name in PLAN_REQUIRED_SECTIONS}


def plan_notes(*ids: str) -> list[ProcedureNote]:
    return [ProcedureNote(procedure_id=p, nature_timing_extent=f"{p} at fieldwork.") for p in ids]


def plan_draft(**changes: Any) -> AuditPlanDraft:
    """合格的计划叙述: 四个必备段, 7 条程序各一条说明, 时间表最早一项恰为期末 (不早于期末即可)."""
    draft = AuditPlanDraft(
        overall_strategy="A substantive audit of the shortened financial year.",
        sections=PLAN_SECTIONS,
        procedure_notes=plan_notes(*PLAN_PROCEDURES),
        timetable=[
            Milestone(milestone="Period end", due=PLAN_END),
            Milestone(milestone="Auditor's report dated", due=date(2026, 10, 23)),
        ],
        eqr_recommendation=EqrRecommendation(required=False, basis="Not listed and not required by law."),
        expert_assessment="No auditor's expert is needed.",
        standards_cited=["ASA300-P07", "ASQM1-P34f"],
    )
    return draft.model_copy(update=changes)


async def check_plan_draft(draft: AuditPlanDraft, ctx: ValidationContext | None = None) -> Any:
    validator, _ = make_validator()
    context = ctx or ValidationContext(
        standards_of="PLN-15", expected_procedure_ids=PLAN_PROCEDURES, period_end=PLAN_END
    )
    return await validator.validate(EID, PLN15, respond(draft), context)


@pytest.mark.parametrize("extra", [[], ["REV-05"]])
async def test_plan_draft_passes(extra: list[str]) -> None:
    """合格的叙述通过 (时间表等于期末的一项不算早于期末); 说明多出计划以外的程序 (REV-05) 照样通过, 输出原样 (A4
    合成计划时不收, 需求 R316, 见 test_risk_assessor.test_pln15_xero_fixture_passes_once). Xero 的固定输出正本经 A4
    校验通过, 见同一用例."""
    draft = plan_draft(procedure_notes=plan_notes(*PLAN_PROCEDURES, *extra))
    report = await check_plan_draft(draft)
    assert (report.level_failed, report.errors, report.output) == (None, [], draft)


PLAN_MISSING = "procedure_notes 缺计划的程序: CSH-02"
PLAN_OUTSIDE = "standards_cited 含模板 standards 以外的片段: ASA315-P12"
PLAN_NO_BASIS = "eqr_recommendation.basis 为空, 须写依据 (ASQM 1 第 34(f) 段)"
PLAN_EARLY = [Milestone(milestone="Planning meeting", due=date(2026, 9, 29))]


@pytest.mark.parametrize(
    ("changes", "level", "errors"),
    [
        ({"procedure_notes": plan_notes(*PLAN_PROCEDURES[1:])}, "semantic", [PLAN_MISSING]),
        (
            {"procedure_notes": plan_notes(*PLAN_PROCEDURES, "REV-02")},
            "semantic",
            ["procedure_notes 的程序重复: REV-02"],
        ),
        ({"standards_cited": ["ASA300-P07", "ASA315-P12"]}, "reference", [PLAN_OUTSIDE]),
        ({"timetable": PLAN_EARLY}, "semantic", ["时间表早于期末 2026-09-30: Planning meeting (2026-09-29)"]),
        *[
            (
                {"sections": {k: v for k, v in PLAN_SECTIONS.items() if k != name}},
                "semantic",
                [f"sections 缺必备的段: {name}"],
            )
            for name in PLAN_REQUIRED_SECTIONS
        ],
        ({"sections": PLAN_SECTIONS | {"timing": "  "}}, "semantic", ["sections 缺必备的段: timing"]),
        (
            {"sections": PLAN_SECTIONS | {"overall_strategy": "again"}},
            "semantic",
            ["sections 不能含 overall_strategy (总体策略另有字段)"],
        ),
        ({"eqr_recommendation": EqrRecommendation(required=False, basis=" ")}, "semantic", [PLAN_NO_BASIS]),
        # 语义级的问题一并列出; 引用失败即返回, 不再查语义
        (
            {
                "procedure_notes": plan_notes(*PLAN_PROCEDURES[1:]),
                "eqr_recommendation": EqrRecommendation(required=False, basis=""),
            },
            "semantic",
            [PLAN_MISSING, PLAN_NO_BASIS],
        ),
        ({"standards_cited": ["ASA315-P12"], "timetable": PLAN_EARLY}, "reference", [PLAN_OUTSIDE]),
    ],
)
async def test_plan_draft_failures(changes: dict[str, Any], level: str, errors: list[str]) -> None:
    """验收 4: 程序说明缺计划的程序或重复 (多出的不算, 需求 R316), 时间表早于期末, 缺必备段 (或只有空白), 段里重复
    总体策略, 项目质量复核建议没写依据 → semantic, 几项一并列出; 引用模板 standards 以外的片段 → reference, 不再查
    语义. 都可重试 (需求 R348)."""
    report = await check_plan_draft(plan_draft(**changes))
    assert (report.level_failed, report.errors, report.retryable) == (level, errors, True)


@pytest.mark.parametrize(
    "ctx",
    [
        ValidationContext(standards_of="PLN-15"),
        ValidationContext(standards_of="PLN-15", expected_procedure_ids=PLAN_PROCEDURES),
        ValidationContext(standards_of="PLN-15", period_end=PLAN_END),
        ValidationContext(standards_of=None, expected_procedure_ids=PLAN_PROCEDURES, period_end=PLAN_END),
    ],
)
async def test_plan_check_requires_its_context(ctx: ValidationContext) -> None:
    """验收 7: 计划叙述的校验须有模板 (standards_of), 计划的程序 (expected_procedure_ids) 与期末 (period_end), 缺一
    即 ValueError, 不静默放过."""
    with pytest.raises(ValueError):
        await check_plan_draft(plan_draft(), ctx)


# ── FIN-03 错报分类 (需求 R157) ──

FIN03 = LIBRARY.load("FIN-03")
REGISTER = "documents/evidence/fixed_asset_register.pdf"
PAYROLL = "documents/evidence/payroll_register_terms.pdf"
BA091_REPLY = "confirmations/12345679.pdf"
STANDARDS_ANY = ValidationContext(standards_of=None)


def classified(type_: MisstatementType, fraud: bool, reason: str | None) -> MisstatementClassificationDraft:
    """REV-02 跨期草稿 (Numbat 的指针) 加错报性质, 只用来看性质的语义级."""
    nature = MisstatementNature(type=type_, fraud_indicative=fraud, fraud_reason=reason)
    return MisstatementClassificationDraft(**cutoff_draft().model_dump(), nature=nature)


async def check_fin03(output: Any) -> Any:
    """FIN-03 模板下校验 (Numbat 的假财务存储, 准则片段不按程序核对)."""
    validator, _ = make_validator()
    return await validator.validate(EID, FIN03, respond(output), STANDARDS_ANY)


@pytest.mark.parametrize(
    ("type_", "fraud", "reason", "error"),
    [
        (MisstatementType.factual, False, None, None),
        (MisstatementType.judgmental, True, "Entry reverses a recorded sale.", None),
        (MisstatementType.projected, False, None, "错报类型不能为 projected: 本系统不做审计抽样, 没有推断错报"),
        (MisstatementType.factual, True, None, "有舞弊迹象 (fraud_indicative 为真) 却没有理由 (fraud_reason)"),
        (MisstatementType.factual, True, "  ", "有舞弊迹象 (fraud_indicative 为真) 却没有理由 (fraud_reason)"),
    ],
)
async def test_classification_nature_semantics(
    type_: MisstatementType, fraud: bool, reason: str | None, error: str | None
) -> None:
    """R157 验收 3: 分类草稿同判断草稿各级之外, 语义级另查性质: 类型为 projected (不做审计抽样), 或有舞弊迹象而理由
    为空 (含全空白), 判 semantic (可重试, 需求 R348); 其余通过, 输出仍是分类草稿."""
    report = await check_fin03(classified(type_, fraud, reason))
    if error is None:
        assert report.level_failed is None and isinstance(report.output, MisstatementClassificationDraft)
    else:
        assert (report.level_failed, report.retryable, report.errors) == ("semantic", True, [error])


async def test_classification_reference_failure_comes_before_nature() -> None:
    """引用级不过 (没有能解析的指针) 时报引用级, 不再看性质."""
    bad = EvidencePointer(type=EvidenceType.voucher, source_type="sales_invoice", source_id="SI-NONE", locator="x")
    draft = classified(MisstatementType.projected, False, None)
    steps = [step.model_copy(update={"evidence": [bad] if step.evidence else []}) for step in draft.reasoning_chain]
    draft = draft.model_copy(update={"reasoning_chain": steps, "evidence": [bad]})
    report = await check_fin03(draft)
    assert report.level_failed == "reference" and not any("projected" in e for e in report.errors)


def fin03_fixture(name: str) -> str:
    return (XERO.fixtures / "FIN-03" / f"{name}.json").read_text(encoding="utf-8")


def xero_validator() -> OutputValidator:
    """Xero 演示导入之后的假财务存储 (行与 fieldwork 阶段上传的文件), 另登记 BA-091 的回函 (CSH-04 存档)."""
    financial = xero_financial_store()
    financial.document_refs.append(xero_document(BA091_REPLY))
    return OutputValidator(financial, LIBRARY, FakeTrail())


@pytest.mark.external
@pytest.mark.parametrize(
    ("name", "type_", "facts", "keyword"),
    [
        (
            "XJ-499",
            MisstatementType.factual,
            {("journal_entry", f"XJ-499-{n}", None) for n in (1, 2, 3)} | {("document", REGISTER, 1)},
            "manual journal posted without independent review: control deficiency lead",
        ),
        (
            "BA-091",
            MisstatementType.factual,
            {("trial_balance", "091", None), ("document", BA091_REPLY, 1)},
            "AUD 48.53",
        ),
        ("826", MisstatementType.factual, {("trial_balance", "477", None), ("document", PAYROLL, 1)}, "AUD 4,261.20"),
        ("804", MisstatementType.factual, {("trial_balance", "477", None), ("document", PAYROLL, 1)}, "AUD 2,010.00"),
        (
            "805",
            MisstatementType.judgmental,
            {("trial_balance", "477", None), ("document", PAYROLL, 1)},
            "AUD 2,687.36",
        ),
    ],
)
async def test_fin03_xero_fixtures_pass_four_levels(
    name: str, type_: MisstatementType, facts: set[tuple[str, str, int | None]], keyword: str
) -> None:
    """R157 验收 5, 6: Xero 5 份 fixture (fixture_key 为来源判断的对象) 经四级校验 (假财务存储的 Xero 行与登记的文件,
    准则片段按 FIN-03 的 standards) 一次通过: 都是 accept, 置信 0.85, 没有舞弊迹象, 类型与 fact 指针如表 (文件按补造路径
    对应文件 ID), standard 指针为 ASA450-A6 与 ASA240-P36, 结论含该错报的要点."""
    report = await xero_validator().validate(
        EID, FIN03, respond(None, fin03_fixture(name)), ValidationContext(standards_of="FIN-03")
    )
    assert (report.level_failed, report.errors, report.unsupported_steps) == (None, [], [])
    draft = report.output
    assert isinstance(draft, MisstatementClassificationDraft)
    assert (draft.proposed_action, draft.self_confidence) == (ProposedAction.accept, 0.85)
    assert draft.misstatement_draft is None
    assert draft.nature == MisstatementNature(type=type_, fraud_indicative=False, fraud_reason=None)
    ids = {xero_document(path).id: path for path in (REGISTER, PAYROLL, BA091_REPLY)}
    cited = {
        (p.source_type, ids.get(p.source_id, p.source_id), p.page)
        for step in draft.reasoning_chain
        if step.kind is StepKind.fact
        for p in step.evidence
    }
    assert cited == facts
    standards = {p.source_id for step in draft.reasoning_chain if step.kind is StepKind.standard for p in step.evidence}
    assert standards == {"ASA450-A6", "ASA240-P36"}
    assert keyword in draft.conclusion and "current period" in draft.conclusion


@pytest.mark.external
async def test_fin03_judge_returns_the_classification_draft() -> None:
    """R157 验收 4, 6: judge 带回通过校验的草稿 (JudgmentOutcome.draft, 即分类草稿, 判断之外的性质由调用方取, 序列化
    时也在); 提示词按 judge 给的顶层变量渲染 (data 的各列表为空; 按头注释给齐时的渲染见 test_knowledge). XJ-499 改为
    有舞弊迹象而理由为空: 判 semantic, 回传重试仍不过, 共调 MAX_ATTEMPTS 次 (需求 R348), judgment 与 draft 都为空,
    失败类型 unattributed (混合程序)."""
    good = fin03_fixture("XJ-499")
    fraud = json.loads(good)
    fraud["nature"]["fraud_indicative"] = True
    llm = ScriptedLLM({"XJ-499": [good, *[json.dumps(fraud)] * MAX_ATTEMPTS]})
    trail = FakeTrail()
    engine = JudgmentEngine(llm, xero_validator(), trail, LIBRARY)
    misstatement = {"id": "M-D-XJ499", "version": 1, "judgment_id": "J-GEN-02-1-XJ-499", "type": "factual"}
    source = {"id": "J-GEN-02-1-XJ-499", "procedure_id": "GEN-02", "subject_id": "XJ-499", "conclusion": "No basis."}
    data = {
        "misstatement": misstatement,
        "source_judgment": source,
        "journal_lines": [],
        "cited_rows": [],
        "documents": [],
        "fraud_risks": [],
    }
    jctx = JudgmentContext(subject_id="M-D-XJ499", task={"params": {}, "risks": []}, data=data, fixture_key="XJ-499")
    passed = await engine.judge(EID, FIN03, make_run("FIN-03"), jctx, "U-1")
    assert passed.judgment is not None and passed.judgment.id == "J-FIN-03-1-M-D-XJ499"
    assert isinstance(passed.draft, MisstatementClassificationDraft)
    assert passed.draft.nature.type is MisstatementType.factual
    assert passed.model_dump(mode="json")["draft"]["nature"]["type"] == "factual"  # 序列化不丢性质 (审核 R157-05)
    assert passed.draft.evidence == passed.judgment.evidence  # 指针已按来源改写, 同判断
    failed = await engine.judge(EID, FIN03, make_run("FIN-03"), jctx, "U-1")
    assert (failed.judgment, failed.draft, failed.failure, failed.attempts) == (
        None,
        None,
        "unattributed",
        MAX_ATTEMPTS,
    )
    assert trail.entries[-1].payload["level_failed"] == "semantic"


# ── 持续经营结论建议 (FIN-02, 需求 R183) ──

GEN04_ID = "J-GEN-04-1-CLIENT-XERO-3C1332BB-Q1"  # Xero 的持续经营评价判断
SUPPORT_LETTER = "documents/evidence/director_support_letter.pdf"  # 董事支持函, 1 页, correspondence, internal_original
# FIN-02.yaml 由需求 R185 加; 校验只按模板取输出模型, 这里借 REV-02 的模板换成 GoingConcernAdvice
FIN02 = REV02.model_copy(update={"llm": REV02.llm.model_copy(update={"output_model": "GoingConcernAdvice"})})  # type: ignore[union-attr]


def gen04(version: int = 2, status: ReviewStatus = ReviewStatus.confirmed) -> Judgment:
    """状态中的持续经营评价判断 (其余字段同 confirmed_judgment)."""
    subject = "CLIENT-XERO-3C1332BB-Q1"
    update = {"id": GEN04_ID, "procedure_id": "GEN-04", "run_id": "RUN-GEN-04-1", "subject_id": subject}
    return confirmed_judgment(version, status).model_copy(update=update)


def gc_pointer(source_type: str, source_id: str, page: int | None = None) -> EvidencePointer:
    """模型填的 type 与 reliability 故意写错, 校验后应按来源改写."""
    wrong = {"type": EvidenceType.external, "reliability": Reliability.external_original}
    return EvidencePointer(source_type=source_type, source_id=source_id, locator="", page=page, **wrong)


def gc_advice(**changes: Any) -> GoingConcernAdvice:
    """Xero 的建议 (验收 2): 存在重大不确定性, 披露充分; 引用 GEN-04 判断第 2 版, 指针为试算表 840 (董事借款), 期后
    流水 BT-090-0107, 董事支持函第 1 页与 ASA570-P18, P19, P22."""
    loan, receipt = gc_pointer("trial_balance", "840"), gc_pointer("bank_transaction", "BT-090-0107")
    letter = gc_pointer("document", xero_document(SUPPORT_LETTER).id, page=1)
    p18, p19, p22 = (gc_pointer("standard", f"ASA570-P{n}") for n in (18, 19, 22))
    fact, standard = StepKind.fact, StepKind.standard
    advice = GoingConcernAdvice(
        proposed_outcome=GoingConcernOutcome.material_uncertainty_disclosed,
        conclusion="A material uncertainty exists; the going concern basis is appropriate and Note 2 discloses it.",
        reasoning_chain=[
            ReasoningStep(kind=fact, text="The director has lent 19,212.21 (account 840).", evidence=[loan]),
            ReasoningStep(kind=fact, text="A deposit of 1,000.00 was received after year end.", evidence=[receipt]),
            ReasoningStep(kind=fact, text="The director undertakes not to demand repayment.", evidence=[letter]),
            ReasoningStep(kind=standard, text="Conclude on material uncertainty and disclosure.", evidence=[p18, p19]),
            ReasoningStep(kind=standard, text="Adequate disclosure: unmodified opinion.", evidence=[p22]),
            ReasoningStep(kind=StepKind.inference, text="Material uncertainty, disclosed.", from_steps=[0, 1, 2, 3, 4]),
        ],
        evidence=[loan, receipt, letter],
        counter_argument="If the director cannot fund the shortfall, the going concern basis is inappropriate.",
        judgment_refs=[JudgmentRef(id=GEN04_ID, version=2)],
        self_confidence=0.8,
    )
    return advice.model_copy(update=changes)


async def check_gc(
    advice: GoingConcernAdvice | None, judgments: Sequence[Judgment] | None = None, raw: str | None = None
) -> Any:
    """FIN-02 的校验: Xero 演示的假财务存储, 状态中的判断缺省为 GEN-04 第 2 版已确认, 须引用它."""
    ctx = ValidationContext(
        judgments=[gen04()] if judgments is None else list(judgments), required_refs=[GEN04_ID], standards_of=None
    )
    return await xero_validator().validate(EID, FIN02, respond(advice, raw), ctx)


@pytest.mark.external
async def test_going_concern_advice_passes_and_rewrites_pointers() -> None:
    """验收 2: 合法输出四级通过, 推理链与 evidence 列表的指针 type 与 reliability 都按来源改写 (模型填的不采信)."""
    report = await check_gc(gc_advice())
    assert (report.level_failed, report.errors, report.unsupported_steps) == (None, [], [])
    assert isinstance(report.output, GoingConcernAdvice)
    pointers = [*report.output.evidence, *(p for step in report.output.reasoning_chain for p in step.evidence)]
    rewritten = {(p.source_type, p.type, p.reliability) for p in pointers}
    assert rewritten == {
        ("trial_balance", EvidenceType.voucher, Reliability.internal_original),
        ("bank_transaction", EvidenceType.bank, Reliability.external_copy),
        ("document", EvidenceType.voucher, Reliability.internal_original),  # 支持函登记为 correspondence
        ("standard", EvidenceType.standard, None),
    }


def unresolvable(advice: GoingConcernAdvice) -> GoingConcernAdvice:
    """把全部指针换成查不到的行."""
    bad = gc_pointer("bank_transaction", "BT-NONE")
    steps = [step.model_copy(update={"evidence": [bad] if step.evidence else []}) for step in advice.reasoning_chain]
    return advice.model_copy(update={"reasoning_chain": steps, "evidence": [bad]})


NOT_FOUND = "指针 bank_transaction BT-NONE: 查无此行"


@pytest.mark.external
@pytest.mark.parametrize(
    ("change", "judgments", "errors"),
    [
        (lambda a: a, [], [f"引用的判断 {GEN04_ID} 不在状态中"]),
        (
            lambda a: a.model_copy(update={"judgment_refs": [JudgmentRef(id=GEN04_ID, version=1)]}),
            None,
            [f"引用的判断 {GEN04_ID} 第 1 版不是当前版本 (第 2 版)"],
        ),
        (lambda a: a, [gen04(status=ReviewStatus.pending)], [f"引用的判断 {GEN04_ID} 尚未确认 (pending)"]),
        (lambda a: a.model_copy(update={"judgment_refs": []}), None, [f"没有引用必须引用的判断 {GEN04_ID}"]),
        (
            unresolvable,
            None,
            [*(f"第 {i} 步{NOT_FOUND}" for i in range(5)), f"evidence 中{NOT_FOUND}", "没有一个能解析的证据指针"],
        ),
    ],
)
async def test_going_concern_reference_failures(
    change: Any, judgments: list[Judgment] | None, errors: list[str]
) -> None:
    """验收 3: 引用的判断不在状态中, 不是当前版本, 未确认, 没引用 required_refs 中的判断, 或指针全部解析不了
    → reference (可重试, 需求 R348), errors 恰为引用级的问题. 底稿另带一处语义缺陷 (第 3 步只引 P19, 语义级照查
    会报 P18), 引用级不过时不查语义."""
    advice = change(with_step(gc_advice(), 3, evidence=gc_advice().reasoning_chain[3].evidence[1:]))
    assert going_concern_semantics(advice)  # 底稿确有语义缺陷
    report = await check_gc(advice, judgments)
    assert (report.level_failed, report.retryable, report.errors) == ("reference", True, errors)


@pytest.mark.external
@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda a: a.model_copy(update={"reasoning_chain": []}), "推理链为空"),
        (lambda a: with_step(a, 0, evidence=[]), "第 0 步 fact 没有证据指针"),
        (lambda a: with_step(a, 4, evidence=a.evidence[:1]), "第 4 步 standard 没有指向准则片段"),
        (lambda a: with_step(a, 5, from_steps=[0, 5]), "第 5 步 inference 引用了非前序步骤 [5]"),
    ],
)
async def test_going_concern_chain_semantic_failures(change: Any, error: str) -> None:
    """验收 4 (推理链): 规则同判断草稿 (chain_semantics) → semantic (可重试, 需求 R348)."""
    report = await check_gc(change(gc_advice()))
    assert (report.level_failed, report.retryable) == ("semantic", True)
    assert error in report.errors


@pytest.mark.external
@pytest.mark.parametrize(
    ("outcome", "required", "wrong"),
    [
        (GoingConcernOutcome.no_material_uncertainty, None, "ASA570-P17"),
        (GoingConcernOutcome.material_uncertainty_disclosed, "ASA570-P18", "ASA570-P17"),
        (GoingConcernOutcome.material_uncertainty_not_disclosed, "ASA570-P18", "ASA570-P17"),
        (GoingConcernOutcome.basis_inappropriate, "ASA570-P17", "ASA570-P18"),
    ],
)
async def test_going_concern_outcome_needs_its_standard_step(
    outcome: GoingConcernOutcome, required: str | None, wrong: str
) -> None:
    """验收 4 (协调者 10-20 裁定, 审核 R183-03): 须引的准则按建议取值区分 (OUTCOME_STANDARDS 与本表一致):
    basis_inappropriate 须有引用 ASA570-P17 的 standard 步骤, 两种存在重大不确定性的须引 ASA570-P18,
    no_material_uncertainty 不另要求; 在 fact 步骤或 evidence 列表里引用不算. 第 3 步只引另一段 (wrong), 须引的片段挪进
    第 2 步 (fact) 与 evidence: 除 no_material_uncertainty 外都判 semantic; 第 3 步改引须引的片段后都通过."""
    assert OUTCOME_STANDARDS.get(outcome) == required
    advice = gc_advice(proposed_outcome=outcome)
    needed = gc_pointer("standard", required or "ASA570-P18")
    [letter] = advice.reasoning_chain[2].evidence
    misplaced = with_step(with_step(advice, 3, evidence=[gc_pointer("standard", wrong)]), 2, evidence=[letter, needed])
    report = await check_gc(misplaced.model_copy(update={"evidence": [*advice.evidence, needed]}))
    if required is None:
        assert (report.level_failed, report.errors) == (None, [])
    else:
        error = f"建议 {outcome} 而推理链没有引用 {required} 的 standard 步骤"
        assert (report.level_failed, report.errors) == ("semantic", [error])
    assert (await check_gc(with_step(advice, 3, evidence=[needed]))).level_failed is None


@pytest.mark.external
@pytest.mark.parametrize(("bad_outcome", "level"), [(False, "syntax"), (True, "structure")])
async def test_going_concern_syntax_and_structure_are_retryable(bad_outcome: bool, level: str) -> None:
    """验收 5: raw 不是 JSON → syntax; proposed_outcome 为 "ok" → structure; 两者都可重试 (call_llm 按 R34 最多
    重试 2 次)."""
    raw = json.dumps({**gc_advice().model_dump(mode="json"), "proposed_outcome": "ok"}) if bad_outcome else "not json"
    report = await check_gc(None, raw=raw)
    assert (report.level_failed, report.retryable, report.output) == (level, True, None)


# ── FIN-09 更正验证 (需求 R164) ──

FIN09 = LIBRARY.load("FIN-09")
# fixture_key (错报来源判断的对象) → 更正凭证, 错报科目, 更正分录影响的其他科目, 结论的要点 (设计稿第 2.8, 6.5 节)
CORRECTIONS: dict[str, tuple[str, str, list[str], list[str]]] = {
    "XJ-499": (
        "AJE-X01",
        "710",
        ["711", "820"],
        ["credits 710 Office Equipment AUD 750.00", "AUD 825.00", "AUD 75.00"],
    ),
    "BA-091": ("AJE-X05", "270", ["620"], ["credits 270 Interest Income AUD 48.53"]),
    "826": ("AJE-X02", "826", ["478"], ["credits 826 Superannuation Payable AUD 4,261.20"]),
    "804": ("AJE-X03", "804", ["477"], ["credits 804 Wages Payable - Payroll AUD 2,010.00"]),
    "805": ("AJE-X04", "805", ["477"], ["credits 805 Accrued Liabilities AUD 2,687.36"]),
}


def fin09_fixture(name: str) -> str:
    return (XERO.fixtures / "FIN-09" / f"{name}.json").read_text(encoding="utf-8")


def entry_accounts(entry_no: str, own: str) -> list[str]:
    """一笔更正凭证各行的科目中错报科目 own 以外的 (校验上下文的 entry_account_codes)."""
    return sorted({line.account_code for line in xero_adjustments([entry_no])} - {own})


XJ499_CODES = entry_accounts("AJE-X01", "710")  # M-1 (710) 的更正凭证 AJE-X01 的其他科目


def adjusted_validator() -> OutputValidator:
    """Xero 演示导入之后的假财务存储, 另放补造的 11 行更正分录 (按行 ID 取得到, 同载入更正通知之后)."""
    financial = xero_financial_store()
    financial.records |= {("journal_entry", line.id): line.model_dump() for line in xero_adjustments()}
    return OutputValidator(financial, LIBRARY, FakeTrail())


async def check_fin09(raw: str, residual: int, codes: list[str]) -> Any:
    ctx = ValidationContext(standards_of="FIN-09", expected_residual_cents=residual, entry_account_codes=codes)
    return await adjusted_validator().validate(EID, FIN09, respond(None, raw), ctx)


@pytest.mark.external
@pytest.mark.parametrize("name", CORRECTIONS)
async def test_fin09_xero_fixtures_pass_four_levels(name: str) -> None:
    """R164 验收 6, 7: 5 份 fixture 在剩余额 0, 匹配凭证为对应更正凭证时一次通过四级校验 (准则片段按 FIN-09 的
    standards): accept, 置信 0.9, 完整更正, 其他科目如表; fact 指针恰为该更正凭证的各行, standard 指针为 ASA450-P07,
    结论写明更正的科目与金额且没有剩余错报."""
    entry_no, own, other_accounts, keywords = CORRECTIONS[name]
    report = await check_fin09(fin09_fixture(name), 0, entry_accounts(entry_no, own))
    assert (report.level_failed, report.errors, report.unsupported_steps) == (None, [], [])
    draft = report.output
    assert isinstance(draft, CorrectionVerificationDraft)
    assert (draft.proposed_action, draft.self_confidence, draft.misstatement_draft) == (
        ProposedAction.accept,
        0.9,
        None,
    )
    assert (draft.fully_corrected, draft.other_accounts) == (True, other_accounts)
    cited = {(p.source_type, p.source_id, step.kind) for step in draft.reasoning_chain for p in step.evidence}
    lines = {("journal_entry", line.id, StepKind.fact) for line in xero_adjustments([entry_no])}
    assert cited == lines | {("standard", "ASA450-P07", StepKind.standard)}
    assert all(k in draft.conclusion for k in [*keywords, "no misstatement remains"]), draft.conclusion


OTHER_ONLY = "other_accounts 只能列匹配凭证中错报科目以外的科目, 多了"


@pytest.mark.external
@pytest.mark.parametrize(
    ("residual", "changes", "error"),
    [
        (25000, {}, "fully_corrected 为 True, 与规则算出的剩余额 25000 不符"),
        (0, {"fully_corrected": False}, "fully_corrected 为 False, 与规则算出的剩余额 0 不符"),
        (25000, {"fully_corrected": False}, None),  # 部分更正: 模型也判没有完整更正
        (0, {"other_accounts": ["711", "820", "720"]}, f"{OTHER_ONLY}: 720"),  # 匹配凭证以外的科目
        (0, {"other_accounts": ["710", "711", "820"]}, f"{OTHER_ONLY}: 710"),  # 错报科目本身 (审核 R164-03)
        (0, {"other_accounts": ["711", "820", "711"]}, "other_accounts 的科目重复: 711"),
    ],
)
async def test_fin09_verification_semantics(residual: int, changes: dict[str, Any], error: str | None) -> None:
    """R164 验收 5, 7 (XJ-499, 匹配凭证 AJE-X01, 错报科目 710): fully_corrected 与 "规则算出的剩余额为 0" 不一致,
    other_accounts 含匹配凭证以外的科目 (720) 或错报科目本身 (710), 或有重复时判 semantic (可重试, 需求 R348);
    一致时通过."""
    raw = json.dumps({**json.loads(fin09_fixture("XJ-499")), **changes})
    report = await check_fin09(raw, residual, XJ499_CODES)
    if error is None:
        assert (report.level_failed, report.errors) == (None, [])
    else:
        assert (report.level_failed, report.retryable, report.errors) == ("semantic", True, [error])


@pytest.mark.external
async def test_fin09_reference_failure_comes_before_verification() -> None:
    """审核 R164-01 (同 FIN-03 的 test_classification_reference_failure_comes_before_nature): 指针全部解析不了时报
    reference, 不再查更正验证 (fully_corrected 与剩余额 0 不符的问题不报)."""
    bad = EvidencePointer(type=EvidenceType.voucher, source_type="journal_entry", source_id="AJE-NONE-1", locator="x")
    draft = CorrectionVerificationDraft.model_validate_json(fin09_fixture("XJ-499"))
    steps = [step.model_copy(update={"evidence": [bad] if step.evidence else []}) for step in draft.reasoning_chain]
    draft = draft.model_copy(update={"reasoning_chain": steps, "evidence": [bad], "fully_corrected": False})
    report = await check_fin09(draft.model_dump_json(), 0, XJ499_CODES)
    assert report.level_failed == "reference" and not any("fully_corrected" in e for e in report.errors)


def fin09_judge_context(residual: int | None, codes: list[str] | None) -> JudgmentContext:
    """A8 交给 judge 的 XJ-499 上下文 (按提示词头注释给齐 data), 规则结果为 residual 与 codes."""
    lines = [{"source_type": "journal_entry", **line.model_dump(mode="json")} for line in xero_adjustments(["AJE-X01"])]
    source = {"id": "J-GEN-02-1-XJ-499", "procedure_id": "GEN-02", "subject_id": "XJ-499", "conclusion": "No basis."}
    data = {
        "misstatement": {"id": "M-D-XJ499", "amount_cents": 75000},
        "source_judgment": source,
        "matched_line_ids": ["AJE-X01-2"],
        "entry_lines": lines,
        "corrected_cents": 75000 - (residual or 0),
        "residual_cents": residual,
    }
    return JudgmentContext(
        subject_id="M-D-XJ499",
        task={"params": {"adjustment_prefix": "AJE-"}, "risks": []},
        data=data,
        fixture_key="XJ-499",
        expected_residual_cents=residual,
        entry_account_codes=codes,
    )


@pytest.mark.external
async def test_fin09_judge_forwards_rule_results_to_validation() -> None:
    """R164: judge 把 JudgmentContext 的剩余额与匹配凭证的科目原样交给校验. 剩余额 0 时通过, 带回更正验证草稿
    (JudgmentOutcome.draft); 剩余额 25000 时判 semantic, 回传重试仍不过, 共调 MAX_ATTEMPTS 次 (需求 R348), 失败
    类型 unattributed (混合程序)."""
    raw = fin09_fixture("XJ-499")
    engine = JudgmentEngine(
        ScriptedLLM({"XJ-499": [raw] * (1 + MAX_ATTEMPTS)}), adjusted_validator(), FakeTrail(), LIBRARY
    )
    passed = await engine.judge(EID, FIN09, make_run("FIN-09"), fin09_judge_context(0, XJ499_CODES), "U-1")
    assert passed.judgment is not None and passed.judgment.id == "J-FIN-09-1-M-D-XJ499"
    assert isinstance(passed.draft, CorrectionVerificationDraft) and passed.draft.other_accounts == ["711", "820"]
    failed = await engine.judge(EID, FIN09, make_run("FIN-09"), fin09_judge_context(25000, XJ499_CODES), "U-1")
    assert (failed.judgment, failed.failure, failed.attempts) == (None, "unattributed", MAX_ATTEMPTS)


@pytest.mark.external
@pytest.mark.parametrize(("residual", "codes"), [(None, ["711", "820"]), (0, None)])
async def test_fin09_missing_context_fails_before_calling_the_model(
    residual: int | None, codes: list[str] | None
) -> None:
    """审核 R164-05: 调用方漏传剩余额或匹配凭证的科目时, judge 在调用大模型之前抛 ValueError (调用方的错, 不当成
    大模型失败, 不花调用)."""
    llm = ScriptedLLM({"XJ-499": [fin09_fixture("XJ-499")]})
    engine = JudgmentEngine(llm, adjusted_validator(), FakeTrail(), LIBRARY)
    with pytest.raises(ValueError, match="expected_residual_cents, entry_account_codes"):
        await engine.judge(EID, FIN09, make_run("FIN-09"), fin09_judge_context(residual, codes), "U-1")
    assert llm.requests == []


# ── R340: 中文正文与其中的英文引文 ──

# 各程序一份固定输出改成中文的结论, 推理与反方论证 (步骤数与指针不变), 引号里是原件的英文原文 (需求 R340 验收 2)
CON002_QUOTE = (
    "“Title to and risk in the Goods pass to the Customer when the Customer's authorised representative signs the "
    "delivery note at the Delivery Address.”"
)  # CON-002 第 5.1 条
BA01_QUOTE = "“Balance at 30 June 2026: AUD 1,208,400.00 (credit)”"  # 12345601.pdf 第 1 页
CONX02_QUOTE = (
    "“The Supplier will issue a tax invoice for the Monthly Fee of each calendar month on or about the 17th day of "
    "that month.”"
)  # CON-X02 第 5.1 条
IN_CHINESE: dict[str, tuple[str, list[str], str]] = {  # 程序 → (结论, 各步, 反方论证)
    "REV-03": (
        f"合同 CON-002 第 5.1 条约定 {CON002_QUOTE} 货物的控制权在 Quoll Automation Pty Ltd 的授权代表于交货地址签收"
        "发运单时转移 (CON-002-T1); 付款期为签收后 30 天 (CON-002-T2), 合同没有退货权与返利条款.",
        [
            f"第 5.1 条 (CON-002-T1): {CON002_QUOTE} 签收之前卖方无权收款.",
            "客户取得所承诺商品的控制权时确认收入 (AASB 15 第 31 段).",
            "在某一时点履行的履约义务, 控制权转移的迹象包括现时收款权, 法定所有权与所有权上的主要风险和报酬"
            " (AASB 15 第 38 段).",
            "法定所有权, 风险与收款权都在签收发运单时转移, 所以控制权在这一时点转移, 收入在这一时点确认.",
        ],
        "若另有安排让客户在签收前取得控制权 (如 bill-and-hold 安排), 转移时点会不同.",
    ),
    "CSH-02": (
        f"银行回函写明 {BA01_QUOTE}, 账面余额为 AUD 1,258,400.00. 差额 AUD 50,000.00 是在途存款: 收款 CR-SI-2026-0547 "
        "于 2026-06-30 入账, 银行于 2026-07-02 入账 (BT-01-20260702-01). 属时间性差异, 不是错报.",
        [
            f"回函确认账户 12345601 截至 2026-06-30 的余额: {BA01_QUOTE}.",
            "总账科目 1010 截至 2026-06-30 的余额为 AUD 1,258,400.00, 回函减账面为 -AUD 50,000.00.",
            "来自 Echidna Controls 的收款 CR-SI-2026-0547 计 AUD 50,000.00, 于 2026-06-30 借记科目 1010.",
            "银行于 2026-07-02 入账来自 Echidna Controls 的 AUD 50,000.00, 附言 SI-2026-0547.",
            "审计师应调查函证中的不符事项, 判断是否表明存在错报 (ASA 505 第 14 段).",
            "这笔收款在年末前入账, 银行在年末后入账, 金额与附言相同, 是在途存款.",
            "在途存款恰好解释了全部差额, 所以这一例外是时间性差异, 不是错报.",
        ],
        "若 7 月 2 日的入账对应另一笔收款, 或银行没有 6 月这笔汇款的记录, 差额就没有得到解释.",
    ),
    "GEN-02": (
        "发票 ORC1003 (分录 XJ-405) 与 ORC1008 (XJ-408) 都在 2026-07-17 开给 Hamilton Smith Pty, 金额都是 AUD 550.00; "
        f"CON-X02 约定 {CONX02_QUOTE} 所以 ORC1003 与 ORC1008 重复. ORC1003 没有服务完成记录, ORC1008 有 7 月的服务"
        "记录 SR-ORC1008. 重复的发票已由贷项通知单 CR1003 (XJ-430) 于 2026-07-26 全额冲销, 这笔分录没有净影响.",
        [
            "XJ-405 记录 2026-07-17 开给 Hamilton Smith Pty 的发票 ORC1003: 收入 AUD 500.00 (XJ-405-1), GST AUD 50.00 "
            "(XJ-405-3), 应收 AUD 550.00 (XJ-405-2).",
            "XJ-408 记录同一天开给同一客户的发票 ORC1008, 金额同为 AUD 550.00.",
            f"CON-X02 第 5.1 条: {CONX02_QUOTE}",
            "ORC1008 有 2026-07-01 至 2026-07-31 的服务记录 SR-ORC1008; ORC1003 没有服务完成记录.",
            "贷项通知单 CR1003 (XJ-430) 于 2026-07-26 冲回 Hamilton Smith Pty 的收入 AUD 500.00, GST AUD 50.00 与应收 "
            "AUD 550.00.",
            "非正常业务过程中处理的分录, 以及没有或几乎没有说明的分录, 应检查其支持性证据 (ASA 240 第 A44 段).",
            "同一月的服务费开了两张 7 月发票, 只有一张有服务记录, 说明 ORC1003 与 ORC1008 重复.",
            "重复的 ORC1003 已由 CR1003 全额冲销, 所以 XJ-405 对收入, GST 与应收没有净影响, 不产生错报.",
        ],
        "若 7 月另有一份服务记录, 或合同变更约定了两份月费, ORC1003 就是有效发票而被错误冲销.",
    ),
}
QUOTES = {"REV-03": CON002_QUOTE, "CSH-02": BA01_QUOTE, "GEN-02": CONX02_QUOTE}


def in_chinese(procedure: str, raw: str) -> str:
    """固定输出 raw 换成 IN_CHINESE 中该程序的中文正文 (步骤数须相同), 其余照旧."""
    conclusion, texts, counter = IN_CHINESE[procedure]
    value = json.loads(raw)
    assert len(texts) == len(value["reasoning_chain"]), procedure
    steps = [{**step, "text": text} for step, text in zip(value["reasoning_chain"], texts, strict=True)]
    return json.dumps({**value, "conclusion": conclusion, "reasoning_chain": steps, "counter_argument": counter})


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize(
    ("procedure", "path", "validator"),
    [
        ("REV-03", FIXTURES / "REV-03" / "CON-002.json", lambda: make_validator()[0]),
        ("CSH-02", FIXTURES / "CSH-02" / "BA-01.json", lambda: make_validator(documents=list(NUMBAT_REPLIES))[0]),
        pytest.param(
            "GEN-02", XERO.fixtures / "GEN-02" / "XJ-405.json", xero_validator, marks=pytest.mark.external, id="GEN-02"
        ),
    ],
    ids=["REV-03", "CSH-02", "GEN-02"],
)
async def test_chinese_text_with_english_quotes_passes_four_levels(
    procedure: str, path: Any, validator: Any, language: str
) -> None:
    """R340 验收 2: 结论, 推理与反方论证为中文, 引号里是合同或回函英文原文的构造输出 (REV-03, CSH-02, GEN-02 各一份,
    改自固定输出) 四级校验一次通过, 正文原样留在输出里; 原来的英文固定输出照旧通过. 语义级不按英文词判断."""
    raw = path.read_text(encoding="utf-8")
    raw = in_chinese(procedure, raw) if language == "zh" else raw
    report = await validator().validate(
        EID, LIBRARY.load(procedure), respond(None, raw), ValidationContext(standards_of=procedure)
    )
    assert (report.level_failed, report.errors, report.unsupported_steps) == (None, [], [])
    draft = report.output
    assert isinstance(draft, JudgmentDraft)
    if language == "zh":
        conclusion, texts, counter = IN_CHINESE[procedure]
        assert (draft.conclusion, [s.text for s in draft.reasoning_chain], draft.counter_argument) == (
            conclusion,
            texts,
            counter,
        )
        assert QUOTES[procedure] in draft.conclusion
