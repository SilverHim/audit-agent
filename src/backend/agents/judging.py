"""判断公共流程: A6 的判断程序与 A8 的 FIN-03, FIN-09 (需求 R158, R165) 共用 (接口文档第 6 节 A6 判断公共流程).

每个判断对象依次经过 judge → A7 复核 (critic) → 判断级降级 (downgrade) → classify → make_task.
判断失败的对象不经中间两步; 规则程序 (REV-08, CSH-05) 不进本流程. 判断级降级在功能 autonomy 打开时生效
(judging_steps, 需求 R170), 关闭时原样返回.
"""

import functools
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.agents.base import AgentDeps, RunContext, finish_run, gather_all
from backend.contracts.models import (
    AuditException,
    ClientProfile,
    DocumentType,
    ExceptionDisposition,
    Judgment,
    ReviewStatus,
    ReviewTask,
    RuleOutcome,
    TargetType,
    TaskKind,
    TaskStatus,
)
from backend.contracts.services import JudgmentContext, JudgmentOutcome
from backend.contracts.state import EngagementState, StatePatch
from backend.services.autonomy_policy import judgment_level
from backend.services.features import AUTONOMY
from backend.services.review_queue import attribute_exceptions, judgment_amount, misstatement_updates, subject_amount
from backend.services.staleness import current_results


@dataclass(frozen=True)
class Subject:
    """一个判断对象: 由各判断程序按自己的规则选出, 带该对象的数据与规则例外."""

    subject_id: str  # 判断对象 ID: 合同, 发票, 银行账户或关系图的公司节点
    data: dict[str, Any]  # 该判断需要的数据行与抽取要素, 字段按程序提示词的头注释
    rule_findings: list[AuditException] = field(default_factory=list)  # 该对象的规则例外 (当前版本)
    # 本项目采用版本的客户画像 (需求 R128): 给了时推理链可引用它 (client_profile 指针), 进 JudgmentContext; 缺省不给
    client_profile: ClientProfile | None = None
    # mock 模式取固定输出的键, 缺省为对象 ID; FIN-03 的对象是错报, 取其来源判断的对象 (需求 R158)
    fixture_key: str | None = None
    # FIN-09 (需求 R165): 规则算出的剩余额与匹配凭证各行的科目中错报科目以外的, 原样进 JudgmentContext; 其余程序为空
    expected_residual_cents: int | None = None
    entry_account_codes: list[str] | None = None


class Subjects(list[Subject]):
    """取判断对象的结果: 判断对象的列表. 混合程序取对象时经规则引擎, outcomes 为引擎没有执行的规则
    (RuleResult.outcomes, 需求 R130), 随判断结果 (JudgedRun) 交到执行记录的终态. 取对象函数 (fieldwork.SubjectSource)
    都返回它, 不经规则引擎的 outcomes 为空."""

    def __init__(self, items: Iterable[Subject] = (), outcomes: Sequence[RuleOutcome] = ()) -> None:
        super().__init__(items)
        self.outcomes = list(outcomes)


@dataclass(frozen=True)
class Judged:
    """一个判断对象送给判断引擎的上下文与引擎的结果."""

    ctx: JudgmentContext  # 判断上下文
    outcome: JudgmentOutcome  # 判断或失败类型


@dataclass(frozen=True)
class JudgedRun:
    """判断程序一次执行的原始发现: 各对象的判断结果, 与取对象时规则引擎没有执行的规则 (Subjects.outcomes, 需求 R130)."""

    judged: list[Judged]
    outcomes: list[RuleOutcome] = field(default_factory=list)


class Critic(Protocol):
    """A7 独立复核 (agents/review.ReviewAgent): 返回写好 critic 字段的同一版本判断. findings 为该判断解释的例外
    (判断对象的规则例外, A6 判断时用的同一组), 校验复核方错报草稿的金额用."""

    async def review(
        self, ctx: RunContext, judgment: Judgment, findings: Sequence[AuditException] = ()
    ) -> Judgment: ...


# 复核之后, 分类之前的一步: 收判断与该对象的规则例外 (当前版本, 同 A7 收到的), 返回 (可能改过的) 判断
Step = Callable[[RunContext, Judgment, Sequence[AuditException]], Awaitable[Judgment]]


async def unchanged(ctx: RunContext, judgment: Judgment, findings: Sequence[AuditException]) -> Judgment:
    """缺省步骤: 原样返回."""
    return judgment


@dataclass(frozen=True)
class JudgingSteps:
    """判断公共流程中按功能替换的步骤 (由 judging_steps 按功能集选).

    downgrade: A7 复核之后, classify 之前的判断级降级 (规格书第 7.4 节), 收到的判断已带 critic 字段; 缺省原样返回,
    功能 autonomy 打开时为 downgrade.
    """

    downgrade: Step = unchanged


DEFAULT_STEPS = JudgingSteps()  # 原样返回


async def downgrade(
    deps: AgentDeps, ctx: RunContext, judgment: Judgment, findings: Sequence[AuditException]
) -> Judgment:
    """判断级降级 (规格书第 7.4 节, 需求 R170): 按 autonomy_policy.judgment_level 改写判断的 autonomy_level 与
    downgrade_reasons (同一版本, 第 1 版写入之前), 之后 classify 读这个有效级别.

    起点与装配时的理由取 ctx.procedure (运行时级别); 判断金额经 review_queue.judgment_amount, 与复核档位同一口径:
    例外取 findings (该对象的规则例外, 含本次执行刚建的, 如 CSH-02 的 D01, D04), 交易行的金额经 subject_amount 读,
    所指错报取状态中的当前版本; 执行重要性取状态的, 未定时 amount_over_pm 不适用. A7 的结论不影响级别.
    例: CSH-02 运行时 L2 [high_risk], BA-091 金额 4853 (执行重要性 16216), 置信度不低于 0.7 → L2 [high_risk].
    """
    exceptions = {x.id: x for x in findings}
    amount = judgment_amount(
        judgment,
        exceptions,
        await subject_amount(deps.financial_store, ctx.engagement.id, judgment),
        {m.id: m for m in ctx.state["misstatements"]},
    )
    decision = judgment_level(
        deps.autonomy,
        ctx.template,
        ctx.procedure,
        judgment,
        amount=amount,
        performance_cents=ctx.state["performance_materiality_cents"],
        exceptions=exceptions,
    )
    return judgment.model_copy(update={"autonomy_level": decision.level, "downgrade_reasons": decision.reasons})


def judging_steps(deps: AgentDeps) -> JudgingSteps:
    """判断公共流程按功能选的部分: 功能 autonomy 打开时降级为 downgrade 且关键证据缺失交人, 关闭时为 DEFAULT_STEPS
    (原样返回, 不看关键证据). A6 各判断程序与 A8 的 FIN-03, FIN-09 都由它取; classify 是否看关键证据见
    checks_key_evidence."""
    if AUTONOMY not in deps.features:
        return DEFAULT_STEPS
    return JudgingSteps(downgrade=functools.partial(downgrade, deps))


def checks_key_evidence(deps: AgentDeps) -> bool:
    """classify 是否把关键证据缺失 (review_queue.missing_evidence) 也算作交人: 功能 autonomy 打开时为真 (需求 R170;
    与 judging_steps 同看 deps.features). judgments_patch 按它传 classify 的 key_evidence."""
    return AUTONOMY in deps.features


def subjects_awaiting_review(ctx: RunContext) -> set[str]:
    """本程序仍有待决复核任务的判断对象: 带 subject_id 的任务 (unattributed), 或以该对象的判断为目标的 normal 任务.

    这些对象已交人处理, 结论由审计师的决定给出, 整条重跑时不再重判: 重判会让其例外升版, 旧任务随之过期,
    整次结果被合并校验拒绝; 也会多出一份待复核的判断. 可重跑任务 (failed, unattributed, 需求 R347) 在 rerun 时已
    整组关闭, 重跑的对象不在其中.
    """
    judgments = {j.id: j for j in ctx.state["judgments"]}
    waiting: set[str] = set()
    for task in ctx.state["review_tasks"]:
        if task.procedure_id != ctx.procedure.id or task.status is not TaskStatus.pending:
            continue
        if task.subject_id is not None:
            waiting.add(task.subject_id)
        elif task.target_type is TargetType.Judgment and task.target_id in judgments:
            waiting.add(judgments[task.target_id].subject_id)
    return waiting


def in_scope(ctx: RunContext, subject_id: str) -> bool:
    """该对象本次要不要处理: rerun_subject_ids 非空时须在其中, 且不在已交人处理的对象里."""
    only = ctx.procedure.rerun_subject_ids
    return (not only or subject_id in only) and subject_id not in subjects_awaiting_review(ctx)


def open_findings(state: EngagementState, procedure_ids: Collection[str]) -> list[AuditException]:
    """当前结果中这些程序尚未归因的例外 (第 1 版 open, 或判断失败时置的 unattributed 版本; 过期重跑取代的执行的例外
    不在当前结果里, services.staleness), 按 ID 排序. 判断程序取前置程序 (depends_on) 的例外 (REV-02, GEN-03, CSH-02,
    GEN-02 的判断对象, GEN-06) 与沿用本程序此前的例外 (earlier_findings) 都经它, agents 里不另写这一过滤. 纯函数."""
    wanted = set(procedure_ids)
    return sorted(
        (
            x
            for x in current_results(state).exceptions
            if x.procedure_id in wanted and x.disposition is not ExceptionDisposition.attributed
        ),
        key=lambda x: x.id,
    )


def earlier_findings(
    state: EngagementState,
    procedure_id: str,
    rule_ids: Collection[str],
    subject_id: str,
    subjects_of: Callable[[AuditException], Collection[str]],
) -> list[AuditException]:
    """对象 subject_id 沿用的规则例外 (需求 R347, R165): 本程序尚未归因的例外 (open_findings; 已归因的不带回, 再失败时
    不会被改回 unattributed) 中规则编号在 rule_ids 中, 涉及该对象 (subjects_of 给出例外涉及的对象, 只对本程序这些
    规则的例外调用) 的, 按规则编号与 ID 排序. 有就沿用, 不再按本次比对新建: 若照常新建 (ID 带 run_no), 此前判断失败
    时已置 unattributed 的那条就没有任务再指向它 (重跑不取代旧执行, 两条都在当前结果里). 沿用的不是 judgments_patch
    的 created, 确认时出 attributed 新版本, 再失败时出 unattributed 新版本. CSH-02 (D01, D04 的 source_ids, 账户) 与
    FIN-09 (V02 的 detail.misstatement_id, 错报) 共用. 纯函数."""
    return sorted(
        (x for x in open_findings(state, [procedure_id]) if x.rule_id in rule_ids and subject_id in subjects_of(x)),
        key=lambda x: (x.rule_id, x.id),
    )


# annotate 不能改的判断字段: 身份与版本, 自主度及其理由, A7 复核的各项
KEPT_BY_ANNOTATE = (
    "id",
    "version",
    "autonomy_level",
    "downgrade_reasons",
    *(name for name in Judgment.model_fields if name.startswith("critic_")),
)


def annotated(annotate: Callable[[JudgmentOutcome], Judgment], outcome: JudgmentOutcome) -> Judgment:
    """judge_all 的 annotate 给成功的判断补字段; 补了 KEPT_BY_ANNOTATE 中的字段时断言失败 (程序错误)."""
    before = outcome.judgment
    assert before is not None  # 只对成功的判断调用
    after = annotate(outcome)
    changed = [name for name in KEPT_BY_ANNOTATE if getattr(after, name) != getattr(before, name)]
    assert not changed, f"annotate 只补字段, 不能改 {', '.join(changed)}"
    return after


async def judge_all(
    deps: AgentDeps,
    ctx: RunContext,
    subjects: list[Subject],
    critic: Critic,
    steps: JudgingSteps = DEFAULT_STEPS,
    annotate: Callable[[JudgmentOutcome], Judgment] | None = None,
) -> list[Judged]:
    """对给定的判断对象做判断: 先按 in_scope 筛选, 再并发逐个交判断引擎, 成功的依次经 A7 复核与降级 (两步都收该对象的
    规则例外).

    annotate: 判断引擎之后, A7 之前给成功的判断补上程序自己的字段 (纯函数, 收判断引擎的结果, 返回补了字段的判断;
    判断失败的不调), 之后的复核与降级都看得到. 只补字段: ID, 版本, 级别 (autonomy_level, downgrade_reasons) 与
    critic 字段不能改 (调用后断言, 见 annotated). FIN-03 用它写上分类载荷 (workpaper.with_update), 降级才能按所指
    错报的金额与舞弊迹象判 (需求 R170); FIN-09 写上验证载荷 (workpaper.with_verification). 缺省不改.

    任务说明含参数快照与本程序覆盖的风险 (Procedure.objective_ids 下的 RiskItem), 均为 JSON 形式;
    fixture_key 取对象的 (缺省为对象 ID); 自主度取 Procedure 的运行时级别; 客户画像取对象的 client_profile;
    performed_by 为项目负责人.
    一个对象失败 (引擎返回 failure) 不影响其他对象; 引擎抛异常时等其余对象结束再上抛.
    """
    subjects = [s for s in subjects if in_scope(ctx, s.subject_id)]
    objectives = set(ctx.procedure.objective_ids)
    risks = [r.model_dump(mode="json") for r in ctx.state["risk_register"] if r.objective_id in objectives]
    # 参数快照按 JSON 取 (日期转成字符串), 与提示词头注释 "对象均为 model_dump(mode="json")" 一致
    task = {"params": ctx.run.model_dump(mode="json")["params_snapshot"], "risks": risks}

    async def judge(subject: Subject) -> Judged:
        data = dict(subject.data)
        if ctx.procedure.rerun_subject_ids:
            data["supplementary_documents"] = [
                {
                    "document": d.model_dump(mode="json"),
                    "pages": await deps.evidence_store.pages(ctx.engagement.id, d.id),
                }
                for d in await deps.financial_store.documents(ctx.engagement.id, related_id=subject.subject_id)
                if d.type is not DocumentType.registry_extract
            ]
        jctx = JudgmentContext(
            subject_id=subject.subject_id,
            task=task,
            data=data,
            rule_findings=subject.rule_findings,
            fixture_key=subject.fixture_key or subject.subject_id,
            autonomy_level=ctx.procedure.autonomy_level,
            client_profile=subject.client_profile,
            expected_residual_cents=subject.expected_residual_cents,
            entry_account_codes=subject.entry_account_codes,
        )
        outcome = await deps.judgments.judge(ctx.engagement.id, ctx.template, ctx.run, jctx, ctx.engagement.owner_id)
        if outcome.judgment is not None:
            judgment = annotated(annotate, outcome) if annotate is not None else outcome.judgment
            # 独立复核在降级与分类之前: 结论进置信度 (8.4), 不一致的由 ReviewQueue 建争议任务
            judgment = await critic.review(ctx, judgment, subject.rule_findings)
            judgment = await steps.downgrade(ctx, judgment, subject.rule_findings)
            outcome = outcome.model_copy(update={"judgment": judgment})
        return Judged(jctx, outcome)

    return await gather_all(judge(s) for s in subjects)


def judgments_patch(
    deps: AgentDeps,
    ctx: RunContext,
    judged: list[Judged],
    outcomes: Sequence[RuleOutcome] = (),
    created: Sequence[AuditException] = (),
) -> StatePatch:
    """判断程序的解读: 按 ReviewQueue.classify 逐个对象产出判断, 复核任务与例外新版本.

    - normal, dispute: 判断待复核 (dispute 为独立复核不一致, 由审计师仲裁), 以判断 v1 为目标建任务.
    - 不建任务: 判断第 1 版即 confirmed, 其解释的例外出新版本 attributed (attribute_exceptions); 判断带错报更新载荷
      (FIN-03 的分类, FIN-09 的验证, 需求 R159, R165) 时按载荷写出所指错报的新版本 (misstatement_updates, 错报取
      状态中的, 同复核决定确认时), 片段另有 misstatements (只在有时带, A6 的判断没有载荷).
    - unattributed: 第一条规则例外出新版本 unattributed, 以新版本为目标建任务 (带对象).
    - failed: 以本次执行 (成功终态) 为目标建带对象的 failed 任务.
    执行记录置成功终态: 个别对象失败不算整次失败; outcomes 为取对象时规则引擎没有执行的规则 (需求 R130), 记进终态.
    created 为本次执行刚建, 还没有写入的例外 (第 1 版; CSH-02 的 D01, D04, FIN-09 的 V02): 片段的 exceptions 依次为
    它们 (归因或标未归因的换成写好的那一版), 再接其余例外的新版本. 它们不出新版本, attributed 或 unattributed 直接写在
    刚建的版本上 (同判断第 1 版即 confirmed), 片段里同一例外只有一个版本 (merge.check_ids), 写入 Store 也从 v1 起
    (需求 R322). 例: BA-091 判断失败, 第一条规则例外为本次的 X-CSH-02-1-D04-1 → v1 unattributed, 任务
    T-X-CSH-02-1-D04-1-v1.
    """

    fresh = {x.id: x.version for x in created}

    def settled(exception: AuditException) -> AuditException:
        """例外新版本; 本次刚建的写在刚建的版本上 (内容为新版本的)."""
        if exception.id not in fresh:
            return exception
        return exception.model_copy(update={"version": fresh[exception.id]})

    now = datetime.now(UTC)
    run = finish_run(ctx.run, now, outcomes)
    queue = deps.queue
    judgments: list[Judgment] = []
    exceptions: list[AuditException] = []
    tasks: list[ReviewTask] = []
    pid = ctx.procedure.id
    key_evidence = checks_key_evidence(deps)
    for item in judged:
        subject_id = item.ctx.subject_id
        findings_of_subject = item.ctx.rule_findings
        kind = queue.classify(ctx.template, item.outcome, findings_of_subject, key_evidence=key_evidence)
        judgment = item.outcome.judgment
        if judgment is not None and kind in (TaskKind.normal, TaskKind.dispute):
            judgments.append(judgment)
            tasks.append(queue.make_task(kind, judgment, pid, run.id, now))
        elif judgment is not None and kind is None:
            confirmed = judgment.model_copy(update={"review_status": ReviewStatus.confirmed})
            judgments.append(confirmed)
            exceptions += [settled(x) for x in attribute_exceptions(confirmed, findings_of_subject)]
        elif kind is TaskKind.unattributed:
            first = findings_of_subject[0]
            marked = settled(
                first.model_copy(
                    update={"version": first.version + 1, "disposition": ExceptionDisposition.unattributed}
                )
            )
            exceptions.append(marked)
            tasks.append(queue.make_task(TaskKind.unattributed, marked, pid, run.id, now, subject_id=subject_id))
        else:
            tasks.append(queue.make_task(TaskKind.failed, run, pid, run.id, now, subject_id=subject_id))
    written = {x.id: x for x in exceptions if x.id in fresh}  # 刚建的例外写好的那一版 (归因或标未归因的)
    exceptions = [written.get(x.id, x) for x in created] + [x for x in exceptions if x.id not in fresh]
    patch: StatePatch = {"judgments": judgments, "exceptions": exceptions, "review_tasks": tasks}
    updated = misstatement_updates(judgments, {m.id: m for m in ctx.state["misstatements"]})
    return {**patch, **({"misstatements": updated} if updated else {}), "procedure_runs": [run]}
