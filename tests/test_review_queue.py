"""复核队列: 按程序 YAML 的 always_review 与 manual_result_allowed 判定任务类型与允许的决定."""

from typing import Any

from backend.contracts.models import (
    AutonomyLevel,
    Decision,
    Judgment,
    ProposedAction,
    ReviewTask,
    TargetType,
    TaskKind,
)
from backend.contracts.services import JudgmentOutcome
from backend.core.config import get_settings
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from tests.fakes import NOW

LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
QUEUE = ReviewQueue(LIBRARY)


def make_outcome(procedure_id: str, level: AutonomyLevel, action: ProposedAction) -> JudgmentOutcome:
    """一个判断对象的成功产出; classify 只读自主度与建议动作, 其余字段不校验."""
    fields: dict[str, Any] = {
        "id": f"J-{procedure_id}-1-S1",
        "procedure_id": procedure_id,
        "autonomy_level": level,
        "proposed_action": action,
    }
    judgment = Judgment.model_construct(**fields)
    return JudgmentOutcome(judgment=judgment, failure=None, attempts=1, trail_ids=[])


def make_task(procedure_id: str, kind: TaskKind, subject_id: str | None) -> ReviewTask:
    """某条程序的复核任务, 指向该程序第 1 次执行."""
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
