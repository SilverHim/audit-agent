"""复核队列: 任务类型判定, 建任务, 允许的决定 (A 负责). 规则见 开发接口文档.md 第 5 节"需要人"."""

from collections.abc import Sequence
from datetime import datetime

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    Decision,
    Judgment,
    ProcedureRun,
    ProposedAction,
    ReviewTask,
    TargetType,
    TaskKind,
)
from backend.contracts.services import JudgmentOutcome
from backend.services.procedure_library import ProcedureLibrary


class ReviewQueue:
    """复核队列: 判定哪些结果需要人, 建复核任务, 给出每类任务允许的决定.

    按程序区分的规则读程序 YAML 的 always_review 与 manual_result_allowed, 不按程序编号分支.
    """

    def __init__(self, library: ProcedureLibrary) -> None:
        self.library = library  # 程序库: 按任务的 procedure_id 取模板

    def allowed(self, task: ReviewTask) -> frozenset[Decision]:
        """某个任务允许的决定. failed 任务带判断对象且模板允许时, 另可人工执行并提交结果."""
        if task.kind is TaskKind.normal:
            return frozenset({Decision.confirm, Decision.override, Decision.misstatement})
        if task.kind is TaskKind.unattributed:
            return frozenset({Decision.override, Decision.misstatement})
        if task.subject_id is not None and self.library.load(task.procedure_id).manual_result_allowed:
            return frozenset({Decision.rerun, Decision.manual_result})
        return frozenset({Decision.rerun})

    def classify(
        self, template: ProcedureTemplate, outcome: JudgmentOutcome, rule_findings: Sequence[AuditException]
    ) -> TaskKind | None:
        """某个判断对象的结果需要哪类任务; None 表示自动关闭. 整次执行失败由 A1 另行处理."""
        if outcome.failure == "unattributed":
            return TaskKind.unattributed if rule_findings else TaskKind.failed
        if outcome.failure == "failed" or outcome.judgment is None:
            return TaskKind.failed
        j = outcome.judgment
        if (
            j.autonomy_level in (AutonomyLevel.L1, AutonomyLevel.L2)
            or j.proposed_action in (ProposedAction.misstatement, ProposedAction.more_evidence)
            or template.always_review
        ):
            return TaskKind.normal
        return None

    def make_task(
        self,
        kind: TaskKind,
        target: Judgment | AuditException | ProcedureRun,
        procedure_id: str,
        run_id: str,
        now: datetime,
        subject_id: str | None = None,
    ) -> ReviewTask:
        target_type = {
            Judgment: TargetType.Judgment,
            AuditException: TargetType.AuditException,
            ProcedureRun: TargetType.ProcedureRun,
        }[type(target)]
        middle = f"-{subject_id}" if kind is TaskKind.failed and subject_id else ""
        return ReviewTask(
            id=f"T-{target.id}{middle}-v{target.version}",
            kind=kind,
            procedure_id=procedure_id,
            target_type=target_type,
            target_id=target.id,
            target_version=target.version,
            run_id=run_id,
            subject_id=subject_id,
            created_at=now,
        )
