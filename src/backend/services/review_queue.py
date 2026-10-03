"""复核队列: 任务类型判定, 建任务, 允许的决定, 例外归因 (A 负责). 规则见 开发接口文档.md 第 5 节"需要人", 第 7 节."""

from collections.abc import Sequence
from datetime import datetime

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    Decision,
    ExceptionDisposition,
    Judgment,
    ProcedureRun,
    ProposedAction,
    ReviewTask,
    TargetType,
    TaskKind,
)
from backend.contracts.services import JudgmentOutcome
from backend.services.procedure_library import ProcedureLibrary


def attribute_exceptions(judgment: Judgment, exceptions: Sequence[AuditException]) -> list[AuditException]:
    """判断置 confirmed 后归因它解释的例外: exception_ids 中的例外出新版本, 置 attributed, attributed_by 为该判断 ID.

    exceptions: 各例外的当前版本, 须含 exception_ids 中的全部, 缺了抛 LookupError; 其余忽略.
    已归因的不再出新版本. 返回例外新版本, 顺序同 exception_ids. 纯函数, 不读写存储;
    DecisionSubmit (confirm 等决定) 与 A6 自动关闭共用 (接口文档第 7 节 "凡 Judgment 置 confirmed").
    例: J-CSH-02-1-BA-01 解释 X-CSH-05-1-B01-1 与 X-CSH-02-1-D01-1, 二者各出 v2 attributed.
    """
    current = {x.id: x for x in exceptions}
    attributed = []
    for exception_id in judgment.exception_ids:
        exception = current.get(exception_id)
        if exception is None:
            raise LookupError(f"{judgment.id} 解释的例外 {exception_id} 不在给定的例外中")
        if exception.disposition is ExceptionDisposition.attributed:
            continue
        attributed.append(
            exception.model_copy(
                update={
                    "version": exception.version + 1,
                    "disposition": ExceptionDisposition.attributed,
                    "attributed_by": judgment.id,
                }
            )
        )
    return attributed


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
