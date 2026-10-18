"""复核队列: 任务类型判定, 建任务, 允许的决定, 例外归因 (A 负责). 规则见 开发接口文档.md 第 5 节"需要人", 第 7 节."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    CriticVerdict,
    Decision,
    DisputeResolution,
    EvidencePointer,
    ExceptionDisposition,
    Judgment,
    ProcedureRun,
    ProposedAction,
    ReviewTask,
    ReviewTier,
    TargetType,
    TaskKind,
)
from backend.contracts.services import JudgmentOutcome
from backend.contracts.views import JudgmentView
from backend.services.judgment_engine import critic_chain_ok
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


# 交易行的类型: 判断对象本身是这类行时, 该行金额即涉及金额 (截止性判断错了, 影响的就是整行金额)
TRANSACTION_SOURCES = ("sales_invoice", "journal_entry", "bank_transaction")
CHECK_EVIDENCE_BELOW = 0.70  # 置信度低于它: 查验证据
BATCH_ABOVE = 0.90  # 置信度高于它才可能批量确认; 不超过它 (且不低于上一档): 阅读推理链


# 复核队列的派生字段 (金额, 档位, 仲裁选项) 按输出视图算: 接口给的是盲判过滤后的 JudgmentView, 被隐藏的字段不得
# 经派生字段漏出; DecisionSubmit 校验时给的是存储的 Judgment
Shown = Judgment | JudgmentView


def subject_row(judgment: Shown) -> EvidencePointer | None:
    """判断对象本身是交易行时, 指向该行的证据指针: source_id 等于 subject_id, 行类型在 TRANSACTION_SOURCES 中.
    先找结论引用的证据, 再找推理链里的 (推理链被隐藏时不找). 没有返回 None (如判断对象是合同或银行账户)."""
    chain = judgment.reasoning_chain or []
    pointers = [*judgment.evidence, *(p for step in chain for p in step.evidence)]
    return next(
        (p for p in pointers if p.source_id == judgment.subject_id and p.source_type in TRANSACTION_SOURCES),
        None,
    )


def row_amount(source_type: str, row: Mapping[str, Any]) -> int | None:
    """交易行的金额, 单位分: 分录行取借方减贷方, 发票与银行流水取 amount_cents."""
    if source_type == "journal_entry":
        return int(row.get("debit_cents", 0)) - int(row.get("credit_cents", 0))
    amount = row.get("amount_cents")
    return int(amount) if amount is not None else None


def task_amount(
    target: Shown | AuditException | ProcedureRun,
    exceptions: Mapping[str, AuditException],
    subject_amount: int | None = None,
) -> int | None:
    """复核任务涉及的金额, 单位分, 保留正负号; 按顺序取到即止:
    1. 判断有错报草稿 (未被隐藏), 取草稿金额; 2. 判断解释的例外中绝对值最大的金额; 3. 目标本身是例外, 取其金额;
    4. 判断对象本身是交易行, 取该行金额 (subject_amount, 由调用方按 subject_row 读出); 5. 都没有为 None.

    exceptions: 例外 ID -> 当前版本, 须含 target.exception_ids 中的 (缺的跳过). 纯函数.
    """
    if isinstance(target, AuditException):
        return target.amount_cents
    if not isinstance(target, Judgment | JudgmentView):
        return None
    if target.misstatement_draft is not None:
        return target.misstatement_draft.amount_cents
    amounts = [
        x.amount_cents
        for i in target.exception_ids
        if (x := exceptions.get(i)) is not None and x.amount_cents is not None
    ]
    if amounts:
        return max(amounts, key=abs)
    return subject_amount


def review_tier(
    task: ReviewTask,
    target: Shown | AuditException | ProcedureRun,
    amount: int | None,
    performance_cents: int | None,
    trivial_cents: int | None,
) -> ReviewTier:
    """复核档位 (规格书 9.1 节), 按顺序判断, 命中即停. 金额一律按绝对值比较; 阈值未定 (PLN-01/03 之前) 为 None.

    0. 争议任务, 或判断的独立复核为不一致: 完整复核 (9.1 "A7 不一致" 一行).
    1. 目标不是判断 (例外或执行记录): 查验证据.
    2. 金额超过执行重要性: 完整复核 (执行重要性未定时跳过).
    3. 置信度被隐藏或低于 0.70: 查验证据 (同前端 lib/review.ts); 不超过 0.90: 阅读推理链.
    4. 置信度高于 0.90: L1 或任务不是 normal 的阅读推理链; 金额为空或不超过明显微小临界值的可批量确认;
       其余阅读推理链. "金额较小" 取明显微小临界值是保守口径 (规格书只写了 "低"), 要调改这一处.
    """
    if task.kind is TaskKind.dispute or (
        isinstance(target, Judgment | JudgmentView) and target.critic_verdict is CriticVerdict.inconsistent
    ):
        return ReviewTier.full_review
    if not isinstance(target, Judgment | JudgmentView):
        return ReviewTier.check_evidence
    if performance_cents is not None and amount is not None and abs(amount) > performance_cents:
        return ReviewTier.full_review
    if target.confidence is None or target.confidence < CHECK_EVIDENCE_BELOW:
        return ReviewTier.check_evidence
    if target.confidence <= BATCH_ABOVE:
        return ReviewTier.read_reasoning
    if target.autonomy_level is AutonomyLevel.L1 or task.kind is not TaskKind.normal:
        return ReviewTier.read_reasoning
    if amount is None or (trivial_cents is not None and abs(amount) <= trivial_cents):
        return ReviewTier.batch_eligible
    return ReviewTier.read_reasoning


class ReviewQueue:
    """复核队列: 判定哪些结果需要人, 建复核任务, 给出每类任务允许的决定.

    按程序区分的规则读程序 YAML 的 always_review 与 manual_result_allowed, 不按程序编号分支.
    """

    def __init__(self, library: ProcedureLibrary) -> None:
        self.library = library  # 程序库: 按任务的 procedure_id 取模板

    def allowed(self, task: ReviewTask) -> frozenset[Decision]:
        """某个任务允许的决定. dispute 任务只能仲裁 (确认会绕过仲裁); failed 任务带判断对象且模板允许时,
        另可人工执行并提交结果."""
        if task.kind is TaskKind.dispute:
            return frozenset({Decision.dispute_resolution})
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
        if j.critic_verdict is CriticVerdict.inconsistent:
            return TaskKind.dispute  # 独立复核不一致: 单列仲裁, 优先于其余规则
        if j.critic_verdict is CriticVerdict.not_applicable:
            return TaskKind.normal  # 命中触发规则而复核没做成: 这条判断本来没人看, 交人复核
        if (
            j.autonomy_level in (AutonomyLevel.L1, AutonomyLevel.L2)
            or j.proposed_action in (ProposedAction.misstatement, ProposedAction.more_evidence)
            or template.always_review
        ):
            return TaskKind.normal
        return None

    @staticmethod
    def resolutions(task: ReviewTask, target: Shown | AuditException | ProcedureRun) -> list[DisputeResolution]:
        """争议任务可选的仲裁结果; A7 的推理链没通过 8.6 校验 (或被隐藏) 时不提供 a7_upheld. 不是争议任务为空."""
        if task.kind is not TaskKind.dispute or not isinstance(target, Judgment | JudgmentView):
            return []
        chain = target.critic_reasoning_chain
        a7 = [DisputeResolution.a7_upheld] if chain is not None and critic_chain_ok(chain) else []
        return [DisputeResolution.a6_upheld, *a7, DisputeResolution.neither]

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
