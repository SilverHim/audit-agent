"""判断公共流程: A6 的判断程序与 A8 的 FIN-03 (需求 R158) 共用 (接口文档第 6 节 A6 判断公共流程).

每个判断对象依次经过 judge → A7 复核 (critic) → 判断级降级 (downgrade) → classify → make_task.
判断失败的对象不经中间两步; 规则程序 (REV-08, CSH-05) 不进本流程. 降级由对应需求实现, 缺省原样返回.
"""

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.agents.base import AgentDeps, RunContext, finish_run, gather_all
from backend.contracts.models import (
    AuditException,
    ClientProfile,
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
from backend.contracts.state import StatePatch
from backend.services.review_queue import attribute_exceptions, misstatement_updates


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


Step = Callable[[RunContext, Judgment], Awaitable[Judgment]]  # 复核之后, 分类之前的一步: 收判断, 返回 (可能改过的) 判断


async def unchanged(ctx: RunContext, judgment: Judgment) -> Judgment:
    """缺省步骤: 原样返回."""
    return judgment


@dataclass(frozen=True)
class JudgingSteps:
    """A7 复核之后, classify 之前的可替换步骤; 收到的判断已带 critic 字段.

    downgrade: 判断级降级 (规格书第 7.4 节, 在 A7 之后), 缺省原样返回.
    """

    downgrade: Step = unchanged


DEFAULT_STEPS = JudgingSteps()  # 原样返回


def subjects_awaiting_review(ctx: RunContext) -> set[str]:
    """本程序仍有待决复核任务的判断对象: 带 subject_id 的任务 (unattributed), 或以该对象的判断为目标的 normal 任务.

    这些对象已交人处理, 结论由审计师的决定给出, 整条重跑时不再重判: 重判会让其例外升版, 旧任务随之过期,
    整次结果被合并校验拒绝; 也会多出一份待复核的判断. failed 任务在 rerun 时已整组关闭, 不在其中.
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


async def judge_all(
    deps: AgentDeps, ctx: RunContext, subjects: list[Subject], critic: Critic, steps: JudgingSteps = DEFAULT_STEPS
) -> list[Judged]:
    """对给定的判断对象做判断: 先按 in_scope 筛选, 再并发逐个交判断引擎, 成功的依次经 A7 复核与降级.

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
        jctx = JudgmentContext(
            subject_id=subject.subject_id,
            task=task,
            data=subject.data,
            rule_findings=subject.rule_findings,
            fixture_key=subject.fixture_key or subject.subject_id,
            autonomy_level=ctx.procedure.autonomy_level,
            client_profile=subject.client_profile,
        )
        outcome = await deps.judgments.judge(ctx.engagement.id, ctx.template, ctx.run, jctx, ctx.engagement.owner_id)
        if outcome.judgment is not None:
            # 独立复核在降级与分类之前: 结论进置信度 (8.4), 不一致的由 ReviewQueue 建争议任务
            judgment = await critic.review(ctx, outcome.judgment, subject.rule_findings)
            judgment = await steps.downgrade(ctx, judgment)
            outcome = outcome.model_copy(update={"judgment": judgment})
        return Judged(jctx, outcome)

    return await gather_all(judge(s) for s in subjects)


def judgments_patch(
    deps: AgentDeps, ctx: RunContext, judged: list[Judged], outcomes: Sequence[RuleOutcome] = ()
) -> StatePatch:
    """判断程序的解读: 按 ReviewQueue.classify 逐个对象产出判断, 复核任务与例外新版本.

    - normal, dispute: 判断待复核 (dispute 为独立复核不一致, 由审计师仲裁), 以判断 v1 为目标建任务.
    - 不建任务: 判断第 1 版即 confirmed, 其解释的例外出新版本 attributed (attribute_exceptions); 判断带错报更新载荷
      (FIN-03 的分类, 需求 R159) 时按载荷写出所指错报的新版本 (misstatement_updates, 错报取状态中的, 同复核决定确认时),
      片段另有 misstatements (只在有时带, A6 的判断没有载荷).
    - unattributed: 第一条规则例外出新版本 unattributed, 以新版本为目标建任务 (带对象).
    - failed: 以本次执行 (成功终态) 为目标建带对象的 failed 任务.
    执行记录置成功终态: 个别对象失败不算整次失败; outcomes 为取对象时规则引擎没有执行的规则 (需求 R130), 记进终态.
    """
    now = datetime.now(UTC)
    run = finish_run(ctx.run, now, outcomes)
    queue = deps.queue
    judgments: list[Judgment] = []
    exceptions: list[AuditException] = []
    tasks: list[ReviewTask] = []
    pid = ctx.procedure.id
    for item in judged:
        subject_id = item.ctx.subject_id
        findings_of_subject = item.ctx.rule_findings
        kind = queue.classify(ctx.template, item.outcome, findings_of_subject)
        judgment = item.outcome.judgment
        if judgment is not None and kind in (TaskKind.normal, TaskKind.dispute):
            judgments.append(judgment)
            tasks.append(queue.make_task(kind, judgment, pid, run.id, now))
        elif judgment is not None and kind is None:
            confirmed = judgment.model_copy(update={"review_status": ReviewStatus.confirmed})
            judgments.append(confirmed)
            exceptions += attribute_exceptions(confirmed, findings_of_subject)
        elif kind is TaskKind.unattributed:
            first = findings_of_subject[0]
            marked = first.model_copy(
                update={"version": first.version + 1, "disposition": ExceptionDisposition.unattributed}
            )
            exceptions.append(marked)
            tasks.append(queue.make_task(TaskKind.unattributed, marked, pid, run.id, now, subject_id=subject_id))
        else:
            tasks.append(queue.make_task(TaskKind.failed, run, pid, run.id, now, subject_id=subject_id))
    patch: StatePatch = {"judgments": judgments, "exceptions": exceptions, "review_tasks": tasks}
    updated = misstatement_updates(judgments, {m.id: m for m in ctx.state["misstatements"]})
    return {**patch, **({"misstatements": updated} if updated else {}), "procedure_runs": [run]}
