"""复核队列: 任务类型判定, 建任务, 允许的决定, 例外归因 (A 负责). 规则见 开发接口文档.md 第 5 节"需要人", 第 7 节."""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, cast

from backend.contracts.errors import VersionConflict
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    CriticVerdict,
    Decision,
    DisputeResolution,
    DraftRule,
    EvidencePointer,
    EvidenceType,
    ExceptionDisposition,
    Judgment,
    ManualRequest,
    Misstatement,
    MisstatementStatus,
    MisstatementUpdate,
    ProcedureRun,
    ProposedAction,
    ReviewStatus,
    ReviewTask,
    ReviewTier,
    SourceType,
    StepKind,
    StepStatus,
    TargetType,
    TaskKind,
    Versioned,
)
from backend.contracts.ports import FinancialDataStore
from backend.contracts.services import JudgmentOutcome
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


def apply_misstatement_update(misstatement: Misstatement, update: MisstatementUpdate, judgment_id: str) -> Misstatement:
    """判断置 confirmed 后按其错报更新载荷写出的错报新版本 (需求 R159, 迁移表见接口文档第 2.5 节). 纯函数.

    载荷中不为空的字段照写 (没写的不改), 版本 +1. 分类另记 classification_judgment_id 为该判断; 验证把状态改为
    corrected 或 partially_corrected 时记 verified_by_judgment_id 为该判断, 改为 communicated (分录与错报对不上) 时置
    needs_recommunication. 经 model_validate 重建 (model_copy 不跑校验), 状态与字段不一致抛 ValidationError.
    载荷依据的不是该错报的当前版本 (ID 或 base_version 不符, 判断之后错报已出新版本) 抛 VersionConflict.
    例: M-D-XJ499 v1 按 J-FIN-03-1-M-D-XJ499 的分类载荷 → v2, type factual, origin current_period,
    classification_judgment_id J-FIN-03-1-M-D-XJ499, status 仍为 identified.
    """
    if (update.misstatement_id, update.base_version) != (misstatement.id, misstatement.version):
        raise VersionConflict(
            f"{judgment_id} 的错报更新依据 {update.misstatement_id} 第 {update.base_version} 版, "
            f"{misstatement.id} 当前为第 {misstatement.version} 版"
        )
    changes = update.model_dump(exclude={"misstatement_id", "base_version", "kind"}, exclude_none=True)
    if update.kind == "classification":
        changes["classification_judgment_id"] = judgment_id
    elif update.status in (MisstatementStatus.corrected, MisstatementStatus.partially_corrected):
        changes["verified_by_judgment_id"] = judgment_id
    elif update.status is MisstatementStatus.communicated:
        changes["needs_recommunication"] = True
    return Misstatement.model_validate({**misstatement.model_dump(), **changes, "version": misstatement.version + 1})


def misstatement_updates(written: Iterable[Versioned], current: Mapping[str, Misstatement]) -> list[Misstatement]:
    """凡判断置 confirmed 且带错报更新载荷 (需求 R159), 按载荷写出所指错报的新版本 (apply_misstatement_update), 顺序同
    written 中的判断. written: 要写出的对象 (被确认的 AI 判断, 或带人给载荷的人工判断; 其余对象忽略); current: 错报
    ID → 当前版本, 缺了载荷所指的抛 LookupError. 纯函数; DecisionSubmit 的复核决定与 A8 自动确认 (agents/judging) 共用,
    同 attribute_exceptions."""
    updated = []
    for judgment in written:
        if not isinstance(judgment, Judgment) or judgment.review_status is not ReviewStatus.confirmed:
            continue
        update = judgment.misstatement_update
        if update is None:
            continue
        misstatement = current.get(update.misstatement_id)
        if misstatement is None:
            raise LookupError(f"{judgment.id} 的错报更新所指的错报 {update.misstatement_id} 不在给定的错报中")
        updated.append(apply_misstatement_update(misstatement, update, judgment.id))
    return updated


# 交易行的类型: 判断对象本身是这类行时, 该行金额即涉及金额 (截止性判断错了, 影响的就是整行金额)
TRANSACTION_SOURCES = ("sales_invoice", "journal_entry", "bank_transaction")
CHECK_EVIDENCE_BELOW = 0.70  # 置信度低于它: 查验证据
BATCH_ABOVE = 0.90  # 置信度高于它才可能批量确认; 不超过它 (且不低于上一档): 阅读推理链
# 各类任务允许的决定 (第 7 节允许决定矩阵), 每种任务类型一行. dispute 任务只能仲裁 (确认会绕过仲裁); failed 任务带判断
# 对象且模板 manual_result_allowed 时另加 manual_result (见 ReviewQueue.allowed). manual 任务 (人工程序) 只能提交结果.
# unattributed 任务 (带规则例外的对象判断失败) 与 failed 一样可以重跑, 也可以由审计师自己下结论 (需求 R347)
ALLOWED: dict[TaskKind, frozenset[Decision]] = {
    TaskKind.normal: frozenset({Decision.confirm, Decision.override, Decision.misstatement, Decision.more_evidence}),
    TaskKind.unattributed: frozenset({Decision.override, Decision.misstatement, Decision.rerun}),
    TaskKind.failed: frozenset({Decision.rerun}),
    TaskKind.dispute: frozenset({Decision.dispute_resolution}),
    TaskKind.manual: frozenset({Decision.manual_result}),
}
# 可以重跑的任务类型 (ALLOWED 中含 rerun 的): 判断或执行失败留下的, 一轮失败算一组 (Q17, decisions.rerun_group)
RERUNNABLE: frozenset[TaskKind] = frozenset(kind for kind, decisions in ALLOWED.items() if Decision.rerun in decisions)


def subject_row(judgment: Judgment) -> EvidencePointer | None:
    """判断对象本身是交易行时, 指向该行的证据指针: source_id 等于 subject_id, 行类型在 TRANSACTION_SOURCES 中.
    先找结论引用的证据, 再找推理链里的. 没有返回 None (如判断对象是合同或银行账户)."""
    pointers = [*judgment.evidence, *(p for step in judgment.reasoning_chain for p in step.evidence)]
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


async def subject_amount(financial: FinancialDataStore, eid: str, judgment: Judgment) -> int | None:
    """判断对象本身是交易行时该行的金额 (按 subject_row 找行, row_amount 取金额), 否则或查不到该行为 None.
    复核任务的金额 (api/routes/reviews.py) 与判断级降级 (agents/judging.downgrade) 都经它交给 judgment_amount."""
    pointer = subject_row(judgment)
    if pointer is None:
        return None
    row = await financial.record(eid, cast(SourceType, pointer.source_type), pointer.source_id)
    return row_amount(pointer.source_type, row) if row is not None else None


def missing_evidence(template: ProcedureTemplate, judgment: Judgment) -> list[EvidenceType]:
    """模板 evidence_required 中, 判断推理链的受支持步骤里一个指针都没有的证据种类, 顺序同 evidence_required.
    unsupported 步骤的指针不算 (同置信度的证据覆盖率). 非空即关键证据缺失 (规格书第 7.4, 8.4 节): 功能 autonomy
    打开时由 classify 交人复核, 不经置信度加权抵消. 纯函数.
    例: CSH-02 要 [external, bank], 只有回函 (external) 的指针 → [bank]."""
    found = {p.type for step in judgment.reasoning_chain if step.status is StepStatus.supported for p in step.evidence}
    return [t for t in template.evidence_required if t not in found]


def explained_exceptions(judgment: Judgment, exceptions: Mapping[str, AuditException]) -> list[AuditException]:
    """判断解释的例外: exception_ids 中的, 顺序同它, 不在 exceptions 中的跳过; exceptions 里别的例外 (如同一执行记录
    其他判断解释的) 不算. judgment_amount 与自主度判断级降级 (autonomy_policy.judgment_level) 共用这一取法. 纯函数."""
    return [x for i in judgment.exception_ids if (x := exceptions.get(i)) is not None]


def judgment_amount(
    judgment: Judgment,
    exceptions: Mapping[str, AuditException],
    subject_amount: int | None = None,
    misstatements: Mapping[str, Misstatement] | None = None,
) -> int | None:
    """判断涉及的金额, 单位分, 保留正负号; 复核档位 (经 task_amount) 与自主度判断级降级 (amount_over_pm, 需求 R168)
    共用这一口径. 按顺序取到即止:
    1. 判断携带错报更新 (FIN-03 分类, FIN-09 验证; misstatement_update), 取所指错报的金额; 2. 判断有错报草稿, 取草稿
    金额; 3. 判断解释的例外中绝对值最大的金额; 4. 判断对象本身是交易行, 取该行金额 (subject_amount, 由调用方按
    subject_row 读出); 5. 都没有为 None.

    exceptions: 例外 ID -> 当前版本, 按 explained_exceptions 取 (可以多给, 缺的跳过); misstatements: 错报 ID -> 当前
    版本, 所指错报不在其中时跳过这一步. 纯函数.
    """
    update = judgment.misstatement_update
    if update is not None and (found := (misstatements or {}).get(update.misstatement_id)) is not None:
        return found.amount_cents
    if judgment.misstatement_draft is not None:
        return judgment.misstatement_draft.amount_cents
    amounts = [x.amount_cents for x in explained_exceptions(judgment, exceptions) if x.amount_cents is not None]
    if amounts:
        return max(amounts, key=abs)
    return subject_amount


def task_amount(
    target: Judgment | AuditException | ProcedureRun,
    exceptions: Mapping[str, AuditException],
    subject_amount: int | None = None,
    misstatements: Mapping[str, Misstatement] | None = None,
) -> int | None:
    """复核任务涉及的金额, 单位分, 保留正负号: 目标是判断时为 judgment_amount (参数同它), 是例外时为其金额, 是执行
    记录时为 None. 纯函数."""
    if isinstance(target, AuditException):
        return target.amount_cents
    if not isinstance(target, Judgment):
        return None
    return judgment_amount(target, exceptions, subject_amount, misstatements)


def review_tier(
    task: ReviewTask,
    target: Judgment | AuditException | ProcedureRun,
    amount: int | None,
    performance_cents: int | None,
    trivial_cents: int | None,
) -> ReviewTier:
    """复核档位 (规格书 9.1 节), 按顺序判断, 命中即停. 金额一律按绝对值比较; 阈值未定 (PLN-01/03 之前) 为 None.

    0. 争议任务, 或判断的独立复核为不一致: 完整复核 (9.1 "A7 不一致" 一行).
    1. 目标不是判断 (例外或执行记录): 查验证据.
    2. 金额超过执行重要性: 完整复核 (执行重要性未定时跳过).
    3. 置信度低于 0.70: 查验证据; 不超过 0.90: 阅读推理链.
    4. 置信度高于 0.90: L1 或任务不是 normal 的阅读推理链; 建议动作不是接受或关闭 (AI 判为错报或暂时判断不了) 的
       阅读推理链 (批量确认只用于 AI 判为不是错报的, 需求 R363); 金额为空或不超过明显微小临界值的可批量确认; 其余
       阅读推理链. "金额较小" 取明显微小临界值是保守口径 (规格书只写了 "低"), 要调改这一处.
    """
    if task.kind is TaskKind.dispute or (
        isinstance(target, Judgment) and target.critic_verdict is CriticVerdict.inconsistent
    ):
        return ReviewTier.full_review
    if not isinstance(target, Judgment):
        return ReviewTier.check_evidence
    if performance_cents is not None and amount is not None and abs(amount) > performance_cents:
        return ReviewTier.full_review
    if target.confidence < CHECK_EVIDENCE_BELOW:
        return ReviewTier.check_evidence
    if target.confidence <= BATCH_ABOVE:
        return ReviewTier.read_reasoning
    if target.autonomy_level is AutonomyLevel.L1 or task.kind is not TaskKind.normal:
        return ReviewTier.read_reasoning
    if target.proposed_action not in (ProposedAction.accept, ProposedAction.close):
        return ReviewTier.read_reasoning
    if amount is None or (trivial_cents is not None and abs(amount) <= trivial_cents):
        return ReviewTier.batch_eligible
    return ReviewTier.read_reasoning


def draft_rule(target: Judgment, resolution: DisputeResolution) -> DraftRule:
    """仲裁 resolution 时错报草稿的规则 (功能 collab, 需求 R150): a6_upheld 看原判断的 proposed_action, a7_upheld 看
    critic_proposed_action; 该方建议 misstatement 为 required, 建议其他动作为 hidden, 没给建议动作 (collab 之前的
    判断) 为 optional; neither 为 optional (审计师可自行认定). 校验 (decisions.check_dispute_draft) 与复核队列
    (ReviewQueue.draft_rules) 共用. 纯函数."""
    proposed: ProposedAction | None
    if resolution is DisputeResolution.a6_upheld:
        proposed = target.proposed_action
    elif resolution is DisputeResolution.a7_upheld:
        proposed = target.critic_proposed_action
    else:
        return "optional"
    if proposed is None:
        return "optional"
    return "required" if proposed is ProposedAction.misstatement else "hidden"


class ReviewQueue:
    """复核队列: 判定哪些结果需要人, 建复核任务, 给出每类任务允许的决定.

    按程序区分的规则读程序 YAML 的 always_review 与 manual_result_allowed, 不按程序编号分支.
    """

    def __init__(self, library: ProcedureLibrary) -> None:
        self.library = library  # 程序库: 按任务的 procedure_id 取模板

    def allowed(self, task: ReviewTask, target: Versioned | None = None) -> frozenset[Decision]:
        """某个任务允许的决定: 按任务类型取 ALLOWED; failed 任务带判断对象且模板允许时, 另可人工执行并提交结果.
        复核选项跟着 AI 结论 (需求 R363): 目标 (target, 任务目标的当前版本; 不给时只按任务) 是建议认定错报的判断时没有
        confirm, 同意即以 misstatement 认定 (带错报草稿), 不同意以 override 另出结论, 不能以确认结束而不生成错报."""
        decisions = ALLOWED[task.kind]
        if isinstance(target, Judgment) and target.proposed_action is ProposedAction.misstatement:
            decisions = decisions - {Decision.confirm}
        if (
            task.kind is TaskKind.failed
            and task.subject_id is not None
            and self.library.load(task.procedure_id).manual_result_allowed
        ):
            return decisions | {Decision.manual_result}
        return decisions

    def classify(
        self,
        template: ProcedureTemplate,
        outcome: JudgmentOutcome,
        rule_findings: Sequence[AuditException],
        *,
        key_evidence: bool = False,
    ) -> TaskKind | None:
        """某个判断对象的结果需要哪类任务; None 表示自动关闭. 整次执行失败由 A1 另行处理. 级别读判断的有效级别
        (功能 autonomy 打开时为判断级降级之后的); key_evidence 为真时关键证据缺失 (missing_evidence 非空) 的判断
        也交人 (需求 R170; 判断公共流程按 judging_steps 传, 功能 autonomy 打开时为真)."""
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
            or (key_evidence and missing_evidence(template, j))
        ):
            return TaskKind.normal
        return None

    @staticmethod
    def resolutions(task: ReviewTask, target: Judgment | AuditException | ProcedureRun) -> list[DisputeResolution]:
        """争议任务可选的仲裁结果; A7 的推理链没通过 8.6 校验时不提供 a7_upheld. 不是争议任务为空."""
        if task.kind is not TaskKind.dispute or not isinstance(target, Judgment):
            return []
        a7 = [DisputeResolution.a7_upheld] if critic_chain_ok(target.critic_reasoning_chain) else []
        return [DisputeResolution.a6_upheld, *a7, DisputeResolution.neither]

    @staticmethod
    def draft_rules(
        task: ReviewTask, target: Judgment | AuditException | ProcedureRun, collab: bool
    ) -> dict[DisputeResolution, DraftRule]:
        """争议任务每种可选仲裁的错报草稿规则 (draft_rule), 功能 collab 打开时才给 (关闭时草稿两可, 同 collab 之前);
        不是争议任务为空. 复核队列给前端, 前端据此显示 "同时登记错报" 一项, 不自己推."""
        if not collab or not isinstance(target, Judgment):
            return {}
        return {r: draft_rule(target, r) for r in ReviewQueue.resolutions(task, target)}

    def make_task(
        self,
        kind: TaskKind,
        target: Judgment | AuditException | ProcedureRun,
        procedure_id: str,
        run_id: str,
        now: datetime,
        subject_id: str | None = None,
        material: Mapping[str, Any] | None = None,
    ) -> ReviewTask:
        """建一条待决任务, ID 为 T-{目标 ID}[-{subject_id}]-v{目标版本} (subject_id 只在 failed 任务进 ID).

        manual 任务 (人工程序, 需求 R92): 目标为本次执行记录, subject_id 缺省取模板 manual.subject, 请求
        (manual_request) 取模板 manual 段的说明, 结果模型与证据下限, 加上 material (派发时生成的材料, 缺省为空).
        例: make_task(TaskKind.manual, RUN-TST-M01-1 v1, "TST-M01", ...) → T-RUN-TST-M01-1-v1, subject_id RESULT.
        """
        request = None
        if kind is TaskKind.manual:
            spec = self.library.load(procedure_id).manual
            if spec is None:
                raise ValueError(f"{procedure_id} 没有 manual 段, 不能建 manual 任务")
            subject_id = subject_id if subject_id is not None else spec.subject
            request = ManualRequest(
                instructions=spec.instructions,
                result_model=spec.result_model,
                min_evidence=spec.min_evidence,
                material=dict(material or {}),
            )
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
            manual_request=request,
        )


# 补充证据请求 (需求 R369, Issue #65): 从判断里取缺的证据, 整理成结构化请求.
EVIDENCE_SOURCE_WORDS = (
    ("bank", ("bank statement", "bank confirmation", "银行对账单", "银行回函", "银行")),
    ("management", ("management representation", "management explanation", "管理层说明", "管理层")),
    ("other", ("third party", "third-party", "第三方", "registry", "登记处")),
)


def evidence_source(judgment: Judgment) -> Literal["client", "bank", "other", "management"]:
    """向谁要 (需求 R369): 看判断的局限步骤与反方论证里写的文件与对象, 认出银行 (对账单/回函) 为 bank,
    管理层说明为 management, 其他第三方 (登记处等) 为 other, 其余向客户要."""
    texts = " ".join(
        [step.text for step in judgment.reasoning_chain if step.kind is StepKind.limitation]
        + [judgment.counter_argument]
        + [judgment.conclusion]
    ).lower()
    for source, words in EVIDENCE_SOURCE_WORDS:
        if any(word in texts for word in words):
            return source  # type: ignore[return-value]
    return "client"


def evidence_description(judgment: Judgment) -> str:
    """要什么 (需求 R369): 优先取局限步骤 (缺的证据), 没有时取反方论证; 都没有时用结论末句. 原文照录, 审计师可改."""
    limitations = [step.text for step in judgment.reasoning_chain if step.kind is StepKind.limitation]
    if limitations:
        return " ".join(limitations)
    counter = judgment.counter_argument.strip()
    return counter or judgment.conclusion
