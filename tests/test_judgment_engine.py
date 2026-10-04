"""JudgmentEngine.judge: 草稿补全为 Judgment, 置信度, 失败映射 (接口文档第 5 节, 合成数据集设计第 7 节第 8, 9 行)."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from backend.contracts.llm_outputs import JudgmentDraft
from backend.contracts.models import (
    AutonomyLevel,
    EvidencePointer,
    EvidenceType,
    ProposedAction,
    ReasoningStep,
    ReviewStatus,
    StepKind,
    StepStatus,
)
from backend.contracts.services import JudgmentContext
from backend.core.config import get_settings
from backend.main import build_deps
from backend.services.judgment_engine import JudgmentEngine, OutputValidator, evidence_coverage, rule_consistency
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CONTRACTS,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_RECORDS,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    ScriptedLLM,
    make_deps,
    make_run,
    numbat_rev08_exception,
)
from tests.test_output_validator import CLAUSE, DELIVERY, INVOICE, STANDARD, cutoff_draft, with_step

EID = ENGAGEMENT.id
SETTINGS = get_settings()
CON001 = NUMBAT_CONTRACTS[0]
X_REV08 = numbat_rev08_exception(make_run("REV-08"))  # X-REV-08-1-R01-1


def setup(replies: dict[str, list[str]], library: ProcedureLibrary | None = None) -> tuple[JudgmentEngine, Any]:
    """判断引擎: 假大模型按脚本回答, 财务数据存储有 Numbat 的合同与数据行; library 为改过 rules.json 的副本时用它."""
    llm = ScriptedLLM(replies)
    trail = FakeTrail()
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS), NUMBAT_RECORDS)
    if library is None:
        return make_deps(FakeRegistry(), trail, financial_store=financial, llm=llm).judgments, llm
    return JudgmentEngine(llm, OutputValidator(financial, library, trail), trail, library), llm


def rev02_context(findings: bool = True) -> JudgmentContext:
    """REV-02 对 SI-2026-0612 的上下文 (字段按 REV-02.md.j2 头注释), 不带合同."""
    invoice = {"source_type": "sales_invoice", **NUMBAT_RECORDS[("sales_invoice", "SI-2026-0612")]}
    return JudgmentContext(
        subject_id="SI-2026-0612",
        task={"params": {"period_end": "2026-06-30", "window_days": 20, "threshold_cents": 9000000}, "risks": []},
        data={
            "invoice": json.loads(json.dumps(invoice, default=str)),
            "delivery_notes": [],
            "contract": None,
            "document": None,
            "contract_judgment": None,
        },
        rule_findings=[X_REV08] if findings else [],
        fixture_key="SI-2026-0612",
    )


def rev03_context() -> JudgmentContext:
    """REV-03 对 CON-001 的上下文 (字段按 REV-03.md.j2 头注释)."""
    contract = {
        "id": "CON-001",
        "document_id": CON001.id,
        "counterparty": "Bilby Robotics Pty Ltd",
        "counterparty_abn": "11901043383",
        "amount_cents": 14200000,
        "sign_date": "2026-05-20",
        "terms": [],
        "evidence": [],
    }
    return JudgmentContext(
        subject_id="CON-001",
        task={"params": {"period_end": "2026-06-30"}, "risks": []},
        data={"contract": contract, "document": CON001.model_dump(mode="json")},
        fixture_key="CON-001",
    )


def rev03_draft(**changes: Any) -> JudgmentDraft:
    """REV-03 对 CON-001 的草稿: 签收时转移控制权, accept, 只引合同条款与准则."""
    draft = JudgmentDraft(
        conclusion="控制权在客户签收发运单时转移 (第 5.1 条).",
        reasoning_chain=[
            ReasoningStep(kind=StepKind.fact, text="第 5.1 条: 签收时转移.", evidence=[CLAUSE]),
            ReasoningStep(kind=StepKind.standard, text="某一时点转移控制权.", evidence=[STANDARD]),
            ReasoningStep(kind=StepKind.inference, text="签收时确认收入.", from_steps=[0, 1]),
        ],
        evidence=[CLAUSE, STANDARD],
        counter_argument="若另有验收条款, 结论不成立.",
        proposed_action=ProposedAction.accept,
        self_confidence=0.9,
    )
    return draft.model_copy(update=changes)


async def judge(
    procedure_id: str, ctx: JudgmentContext, raws: list[str], library: ProcedureLibrary | None = None
) -> tuple[Any, ScriptedLLM]:
    engine, llm = setup({ctx.fixture_key: raws}, library)
    template = (library or engine.library).load(procedure_id)
    outcome = await engine.judge(EID, template, make_run(procedure_id), ctx, performed_by="U-1")
    return outcome, llm


# ── 补全 ─────────────────────────────────────────


async def test_rev02_judgment_fields() -> None:
    outcome, llm = await judge("REV-02", rev02_context(), [cutoff_draft().model_dump_json()])
    j = outcome.judgment
    assert (outcome.attempts, outcome.failure, len(outcome.trail_ids)) == (1, None, 1)
    assert (j.id, j.procedure_id, j.run_id, j.subject_id) == (
        "J-REV-02-1-SI-2026-0612",
        "REV-02",
        "RUN-REV-02-1",
        "SI-2026-0612",
    )
    assert j.exception_ids == ["X-REV-08-1-R01-1"] and j.autonomy_level is AutonomyLevel.L2
    assert j.review_status is ReviewStatus.pending and j.performed_by == "U-1" and j.version == 1
    assert j.trail_ref == outcome.trail_ids[-1]
    assert j.proposed_action is ProposedAction.misstatement and j.misstatement_draft == NUMBAT_CUTOFF_DRAFT
    assert [p.reliability for p in j.evidence][:2] == ["internal_original", "internal_original"]  # 已按来源改写
    prompt = llm.requests[0].messages[0].content
    assert "X-REV-08-1-R01-1: rule R01" in prompt and "AASB15-P38 (AASB 15 para 38)" in prompt


async def test_trail_ref_is_the_passing_attempt() -> None:
    outcome, _ = await judge("REV-02", rev02_context(), ["not json", cutoff_draft().model_dump_json()])
    assert outcome.attempts == 2 and outcome.judgment.trail_ref == outcome.trail_ids[1]


# ── 置信度 ─────────────────────────────────────────


async def test_confidence_rev02() -> None:
    outcome, _ = await judge("REV-02", rev02_context(), [cutoff_draft().model_dump_json()])
    j = outcome.judgment
    assert j.confidence_parts == {"evidence": 0.7333, "rule": 1.0, "llm": 0.9}
    assert j.confidence == 0.875


async def test_confidence_without_delivery_pointer() -> None:
    draft = with_step(cutoff_draft(), 1, evidence=[INVOICE])  # 第 1 步改引发票, 不再有发运单指针
    draft = draft.model_copy(update={"evidence": [p for p in draft.evidence if p != DELIVERY]})
    outcome, _ = await judge("REV-02", rev02_context(), [draft.model_dump_json()])
    assert outcome.judgment.confidence_parts["evidence"] == 0.5


async def test_confidence_rev03_has_no_rule_part() -> None:
    outcome, _ = await judge("REV-03", rev03_context(), [rev03_draft().model_dump_json()])
    j = outcome.judgment
    assert j.confidence_parts == {"evidence": 0.8, "llm": 0.9}
    assert j.confidence == 0.84 and j.exception_ids == []


@pytest.mark.parametrize(
    ("action", "has_findings", "expected"),
    [
        (ProposedAction.misstatement, True, 1.0),
        (ProposedAction.more_evidence, True, 1.0),
        (ProposedAction.accept, True, 0.0),
        (ProposedAction.close, True, 0.0),
        (ProposedAction.accept, False, 1.0),
        (ProposedAction.close, False, 1.0),
        (ProposedAction.misstatement, False, 0.0),
        (ProposedAction.more_evidence, False, 0.0),
    ],
)
def test_rule_consistency(action: ProposedAction, has_findings: bool, expected: float) -> None:
    assert rule_consistency(action, has_findings) == expected


async def test_rule_part_follows_findings() -> None:
    accept = cutoff_draft(proposed_action=ProposedAction.accept, misstatement_draft=None)
    outcome, _ = await judge("REV-02", rev02_context(findings=False), [accept.model_dump_json()])
    assert outcome.judgment.confidence_parts["rule"] == 1.0 and outcome.judgment.exception_ids == []
    outcome, _ = await judge("REV-02", rev02_context(findings=True), [accept.model_dump_json()])
    assert outcome.judgment.confidence_parts["rule"] == 0.0


# ── 不受支持的步骤 ─────────────────────────────────────────


async def test_unsupported_step_marked_and_excluded_from_coverage() -> None:
    # 第 1 步一条发运单指针有效, 一条无效: 整步无支撑, 有效的那条也不计入覆盖率
    draft = with_step(cutoff_draft(), 1, evidence=[DELIVERY, DELIVERY.model_copy(update={"source_id": "DN-9999"})])
    draft = with_step(draft, 0, status=StepStatus.unsupported)  # 模型自填的状态不采信
    outcome, _ = await judge("REV-02", rev02_context(), [draft.model_dump_json()])
    j = outcome.judgment
    assert [s.status for s in j.reasoning_chain] == [
        StepStatus.supported,
        StepStatus.unsupported,
        StepStatus.supported,
        StepStatus.supported,
        StepStatus.supported,
    ]
    assert j.confidence_parts["evidence"] == 0.5


def test_evidence_coverage_takes_best_reliability_per_type() -> None:
    weights = {"external_original": 1.0, "external_copy": 0.8, "internal_original": 0.7, "internal_copy": 0.5}
    copy = EvidencePointer(
        type=EvidenceType.contract,
        source_type="document",
        source_id="D",
        locator="x",
        page=1,
        reliability="internal_copy",
    )
    steps = [
        ReasoningStep(kind=StepKind.fact, text="a", evidence=[copy]),
        ReasoningStep(
            kind=StepKind.fact, text="b", evidence=[copy.model_copy(update={"reliability": "external_copy"})]
        ),
        ReasoningStep(kind=StepKind.standard, text="c", evidence=[STANDARD]),
    ]
    assert evidence_coverage([EvidenceType.contract, EvidenceType.invoice], steps, weights) == 0.4


# ── 失败 ─────────────────────────────────────────


async def test_hybrid_failure_is_unattributed() -> None:
    outcome, _ = await judge("REV-02", rev02_context(), ["x", "y", "z"])
    assert (outcome.judgment, outcome.failure, outcome.attempts, len(outcome.trail_ids)) == (None, "unattributed", 3, 3)


async def test_hybrid_reference_failure_is_unattributed_without_retry() -> None:
    untraceable = cutoff_draft(misstatement_draft=NUMBAT_CUTOFF_DRAFT.model_copy(update={"amount_cents": 1}))
    outcome, _ = await judge("REV-02", rev02_context(), [untraceable.model_dump_json()])
    assert (outcome.judgment, outcome.failure, outcome.attempts, len(outcome.trail_ids)) == (None, "unattributed", 1, 1)


async def test_llm_failure_is_failed() -> None:
    bad = rev03_draft().model_copy(update={"reasoning_chain": []})
    outcome, _ = await judge("REV-03", rev03_context(), [bad.model_dump_json()])
    assert (outcome.judgment, outcome.failure, outcome.attempts, len(outcome.trail_ids)) == (None, "failed", 1, 1)


# ── 权重与组装 ─────────────────────────────────────────


async def test_weights_read_from_rules_json(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    shutil.copytree(SETTINGS.KNOWLEDGE_ROOT, root)
    rules = json.loads((root / "rules.json").read_text())
    rules["confidence_weights"] = {"evidence": 1.0, "rule": 0.0, "llm": 1.0}
    (root / "rules.json").write_text(json.dumps(rules))
    outcome, _ = await judge("REV-02", rev02_context(), [cutoff_draft().model_dump_json()], ProcedureLibrary(root))
    assert outcome.judgment.confidence == round((0.7333333 + 0.9) / 2, 4)


async def test_reliability_weights_read_from_rules_json(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    shutil.copytree(SETTINGS.KNOWLEDGE_ROOT, root)
    rules = json.loads((root / "rules.json").read_text())
    rules["reliability_weights"] |= {"internal_original": 0.2, "external_copy": 0.9}
    (root / "rules.json").write_text(json.dumps(rules))
    outcome, _ = await judge("REV-02", rev02_context(), [cutoff_draft().model_dump_json()], ProcedureLibrary(root))
    assert outcome.judgment.confidence_parts["evidence"] == 0.4333  # (0.2 + 0.2 + 0.9) / 3


async def test_no_evidence_required_omits_evidence_part() -> None:
    engine, _ = setup({"SI-2026-0612": [cutoff_draft().model_dump_json()]})
    template = engine.library.load("REV-02").model_copy(update={"evidence_required": []})
    outcome = await engine.judge(EID, template, make_run("REV-02"), rev02_context(), "U-1")
    assert outcome.judgment.confidence_parts == {"rule": 1.0, "llm": 0.9}
    assert outcome.judgment.confidence == 0.96  # (0.3 × 1 + 0.2 × 0.9) / 0.5


async def test_unregistered_standard_raises_before_llm_call() -> None:
    engine, llm = setup({"SI-2026-0612": [cutoff_draft().model_dump_json()]})
    template = engine.library.load("REV-02").model_copy(update={"standards": ["NOT-REGISTERED"]})
    with pytest.raises(LookupError, match="NOT-REGISTERED"):
        await engine.judge(EID, template, make_run("REV-02"), rev02_context(), "U-1")
    assert llm.requests == []


def test_build_deps_constructs_engine() -> None:
    engine = build_deps(SETTINGS).judgments
    assert engine.confidence_weights == {"evidence": 0.3, "rule": 0.3, "llm": 0.2}
    assert engine.reliability_weights["external_copy"] == 0.8
