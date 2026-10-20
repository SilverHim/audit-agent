"""复核队列: 按程序 YAML 的 always_review 与 manual_result_allowed 判定任务类型与允许的决定."""

from typing import Any

import pytest

from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    Decision,
    EvidencePointer,
    EvidenceType,
    Judgment,
    ManualRequest,
    ProposedAction,
    ReasoningStep,
    Reliability,
    ReviewTask,
    StepKind,
    StepStatus,
    TargetType,
    TaskKind,
)
from backend.contracts.services import JudgmentOutcome
from backend.core.config import get_settings
from backend.services.features import BASELINE
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from tests.fakes import NOW, make_run

LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
QUEUE = ReviewQueue(LIBRARY)


def make_outcome(procedure_id: str, level: AutonomyLevel, action: ProposedAction, **extra: Any) -> JudgmentOutcome:
    """一个判断对象的成功产出; classify 只读自主度与建议动作 (功能 autonomy 打开时另读推理链, 由 extra 给), 其余字段
    不校验."""
    fields: dict[str, Any] = {
        "id": f"J-{procedure_id}-1-S1",
        "procedure_id": procedure_id,
        "autonomy_level": level,
        "proposed_action": action,
        **extra,
    }
    judgment = Judgment.model_construct(**fields)
    return JudgmentOutcome(judgment=judgment, failure=None, attempts=1, trail_ids=[])


def make_task(procedure_id: str, kind: TaskKind, subject_id: str | None) -> ReviewTask:
    """某条程序的复核任务, 指向该程序第 1 次执行; manual 任务带一份空请求 (模型校验要求)."""
    request = ManualRequest(instructions="", result_model=None, min_evidence=0, material={})
    return ReviewTask(
        id=f"T-RUN-{procedure_id}-1-v1",
        kind=kind,
        procedure_id=procedure_id,
        target_type=TargetType.ProcedureRun,
        target_id=f"RUN-{procedure_id}-1",
        target_version=1,
        run_id=f"RUN-{procedure_id}-1",
        subject_id=subject_id,
        created_at=NOW,
        manual_request=request if kind is TaskKind.manual else None,
    )


def test_yaml_flags_declared_only_where_expected() -> None:
    always = {t.id for t in LIBRARY.all() if t.always_review}
    manual = {t.id for t in LIBRARY.all() if t.manual_result_allowed}
    assert always == {"CSH-02"}
    assert manual == {"REV-03", "REV-02", "CSH-02"}


def test_always_review_sends_l3_judgment_to_human() -> None:
    outcome = make_outcome("CSH-02", AutonomyLevel.L3, ProposedAction.accept)
    assert QUEUE.classify(LIBRARY.load("CSH-02"), outcome, []) is TaskKind.normal


def test_l3_accept_without_flag_closes_automatically() -> None:
    template = LIBRARY.load("CSH-02").model_copy(update={"always_review": False})
    outcome = make_outcome("CSH-02", AutonomyLevel.L3, ProposedAction.accept)
    assert QUEUE.classify(template, outcome, []) is None


def test_failed_task_allows_manual_result_when_template_allows() -> None:
    task = make_task("REV-03", TaskKind.failed, subject_id="C-001")
    assert QUEUE.allowed(task) == frozenset({Decision.rerun, Decision.manual_result})


def test_failed_task_without_flag_or_subject_allows_rerun_only() -> None:
    assert QUEUE.allowed(make_task("DOC-04", TaskKind.failed, subject_id="D-001")) == frozenset({Decision.rerun})
    assert QUEUE.allowed(make_task("REV-03", TaskKind.failed, subject_id=None)) == frozenset({Decision.rerun})


def test_normal_and_unattributed_decisions_unchanged() -> None:
    normal = make_task("REV-02", TaskKind.normal, subject_id=None)
    unattributed = make_task("REV-02", TaskKind.unattributed, subject_id="INV-1")
    assert QUEUE.allowed(normal) == frozenset({Decision.confirm, Decision.override, Decision.misstatement})
    assert QUEUE.allowed(unattributed) == frozenset({Decision.override, Decision.misstatement})


# ── classify 与 make_task: 第 5 节"需要人"表, 第 0 节任务 ID 规则 ──

REV02 = LIBRARY.load("REV-02")  # hybrid, 不带 always_review


def rule_exception() -> AuditException:
    """REV-08 查出的一条例外, 作 unattributed 判定用的规则例外."""
    fields: dict[str, Any] = {"id": "X-REV-08-1-R01-1", "version": 1, "procedure_id": "REV-08"}
    return AuditException.model_construct(**fields)


@pytest.mark.parametrize(
    ("level", "action", "expected"),
    [
        (AutonomyLevel.L1, ProposedAction.accept, TaskKind.normal),
        (AutonomyLevel.L2, ProposedAction.accept, TaskKind.normal),
        (AutonomyLevel.L3, ProposedAction.misstatement, TaskKind.normal),
        (AutonomyLevel.L3, ProposedAction.more_evidence, TaskKind.normal),
        (AutonomyLevel.L3, ProposedAction.accept, None),
    ],
)
def test_classify_success_by_autonomy_and_action(
    level: AutonomyLevel, action: ProposedAction, expected: TaskKind | None
) -> None:
    """L1, L2 一律交人; L3 只在建议认定错报或补充证据时交人, 其余自动关闭."""
    assert QUEUE.classify(REV02, make_outcome("REV-02", level, action), []) is expected


def test_classify_unattributed_depends_on_rule_findings() -> None:
    """无法归因: 有规则例外才成 unattributed 任务 (人要判断例外怎么处理); 没有例外就是 failed."""
    outcome = JudgmentOutcome(judgment=None, failure="unattributed", attempts=3, trail_ids=[])

    assert QUEUE.classify(REV02, outcome, [rule_exception()]) is TaskKind.unattributed
    assert QUEUE.classify(REV02, outcome, []) is TaskKind.failed


@pytest.mark.parametrize("failure", ["failed", None])
def test_classify_without_judgment_is_failed(failure: str | None) -> None:
    """校验失败或没有判断对象 (judgment 为空) 都成 failed 任务, 有没有规则例外都一样."""
    outcome = JudgmentOutcome(judgment=None, failure=failure, attempts=3, trail_ids=[])  # type: ignore[arg-type]

    assert QUEUE.classify(REV02, outcome, [rule_exception()]) is TaskKind.failed


def test_make_task_ids_and_target_types() -> None:
    """ID 为 T-{目标}-v{版本}; 只有带判断对象的 failed 任务在中间加对象 ID. target_type 按目标的类型."""
    judgment = make_outcome("REV-02", AutonomyLevel.L2, ProposedAction.accept).judgment
    assert judgment is not None
    judgment = judgment.model_copy(update={"version": 2})
    run = make_run("REV-02", 1)

    normal = QUEUE.make_task(TaskKind.normal, judgment, "REV-02", run.id, NOW, subject_id="SI-1")
    unattributed = QUEUE.make_task(TaskKind.unattributed, rule_exception(), "REV-02", run.id, NOW, subject_id="SI-1")
    failed = QUEUE.make_task(TaskKind.failed, run, "REV-02", run.id, NOW, subject_id="SI-1")
    failed_whole = QUEUE.make_task(TaskKind.failed, run, "REV-02", run.id, NOW)

    assert (normal.id, normal.target_type, normal.target_version) == ("T-J-REV-02-1-S1-v2", TargetType.Judgment, 2)
    assert (unattributed.id, unattributed.target_type) == ("T-X-REV-08-1-R01-1-v1", TargetType.AuditException)
    assert (failed.id, failed.target_type) == ("T-RUN-REV-02-1-SI-1-v1", TargetType.ProcedureRun)
    assert failed_whole.id == "T-RUN-REV-02-1-v1"
    assert [t.subject_id for t in (normal, unattributed, failed, failed_whole)] == ["SI-1", "SI-1", "SI-1", None]


# ── 需求 R170 验收 7: 关键证据缺失 ──

REPLY = EvidencePointer(
    type=EvidenceType.external,
    source_type="document",
    source_id="DOC-REPLY",
    locator="confirmed_balance_cents",
    page=1,
    reliability=Reliability.external_original,
)
STATEMENT = EvidencePointer(
    type=EvidenceType.bank,
    source_type="bank_transaction",
    source_id="BT-1",
    locator="amount_cents",
    reliability=Reliability.external_original,
)
AUTONOMY = BASELINE | {"autonomy"}


def fact(pointer: EvidencePointer, status: StepStatus = StepStatus.supported) -> ReasoningStep:
    return ReasoningStep(kind=StepKind.fact, text="x", evidence=[pointer], status=status)


@pytest.mark.parametrize(
    ("features", "chain", "expected"),
    [
        (AUTONOMY, [fact(REPLY)], TaskKind.normal),  # 缺 bank: 置信度 0.95 也交人
        (AUTONOMY, [fact(REPLY), fact(STATEMENT)], None),  # 两种都有: 自动关闭
        (AUTONOMY, [fact(REPLY), fact(STATEMENT, StepStatus.unsupported)], TaskKind.normal),  # 所在步骤不受支持, 不算
        (BASELINE, [fact(REPLY)], None),  # 功能关闭: 不看关键证据 (同现状)
    ],
)
def test_missing_key_evidence_goes_to_human(
    features: frozenset[str], chain: list[ReasoningStep], expected: TaskKind | None
) -> None:
    """合成 L3 hybrid 判断 (CSH-02 去掉 always_review, evidence_required [external, bank]), 置信度 0.95, accept:
    功能 autonomy 打开时, 受支持步骤里缺任一种要求的证据就建 normal 任务, 不自动关闭 (规格书第 8.4 节: 不允许加权
    抵消); 补上 bank 的指针后自动关闭."""
    queue = ReviewQueue(ProcedureLibrary(get_settings().KNOWLEDGE_ROOT, features))
    template = queue.library.load("CSH-02").model_copy(update={"always_review": False})
    assert template.engine == "hybrid" and template.evidence_required == [EvidenceType.external, EvidenceType.bank]
    outcome = make_outcome("CSH-02", AutonomyLevel.L3, ProposedAction.accept, confidence=0.95, reasoning_chain=chain)
    assert queue.classify(template, outcome, []) is expected
