"""A6: REV-08, REV-03, REV-02, CSH-05, CSH-02.

返回字段: exceptions, judgments, review_tasks, confirmations, procedure_runs.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, finish_run, gather_all
from backend.contracts.models import (
    AgentId,
    AuditException,
    ExceptionDisposition,
    Judgment,
    ReviewStatus,
    ReviewTask,
    TargetType,
    TaskKind,
    TaskStatus,
)
from backend.contracts.services import JudgmentContext, JudgmentOutcome, RuleResult
from backend.contracts.state import StatePatch
from backend.services.review_queue import attribute_exceptions


@dataclass(frozen=True)
class Subject:
    """一个判断对象: 由各判断程序按自己的规则选出, 带该对象的数据与规则例外."""

    subject_id: str  # 判断对象 ID: 合同, 发票或银行账户
    data: dict[str, Any]  # 该判断需要的数据行与抽取要素, 字段按程序提示词的头注释
    rule_findings: list[AuditException] = field(default_factory=list)  # 该对象的规则例外 (当前版本)


@dataclass(frozen=True)
class Judged:
    """一个判断对象送给判断引擎的上下文与引擎的结果."""

    ctx: JudgmentContext  # 判断上下文
    outcome: JudgmentOutcome  # 判断或失败类型


Findings = RuleResult | list[Judged]  # 规则程序为规则结果, 判断程序为各对象的判断结果
Execute = Callable[[RunContext], Awaitable[Findings]]  # 执行一条程序, 返回原始发现
Interpret = Callable[[RunContext, Findings], StatePatch]  # 把原始发现转成状态片段
SubjectSource = Callable[[RunContext], Awaitable[list[Subject]]]  # 判断程序取全部判断对象


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


class FieldworkAgent(ProcedureAgent[Findings]):
    """A6: 执行收入与货币资金的实质性程序, 产出例外与判断.

    按程序编号分派到各自的执行与解读. 判断程序 (REV-03, REV-02, CSH-02) 共用判断流程, 各自只提供
    "取判断对象" (登记在 subject_sources); 尚未登记的执行时报未实现.
    """

    agent_id: ClassVar[AgentId] = AgentId.A6  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02"})  # 负责的程序

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        self.subject_sources: dict[str, SubjectSource] = {  # 判断程序编号 -> 取判断对象, 各判断程序接入时登记
            "REV-03": self.rev03_subjects,
        }

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读). 没有登记的程序抛 NotImplementedError."""
        handlers: dict[str, tuple[Execute, Interpret]] = {
            "REV-08": (self.run_rules, self.rule_exceptions),
            "CSH-05": (self.run_rules, self.rule_exceptions),
        }
        handlers |= {pid: (self.judge_subjects, self.judgments_patch) for pid in self.subject_sources}
        if procedure_id not in handlers:
            raise NotImplementedError(procedure_id)
        return handlers[procedure_id]

    async def execute(self, ctx: RunContext) -> Findings:
        execute, _ = self.handler(ctx.procedure.id)
        return await execute(ctx)

    async def interpret(self, ctx: RunContext, findings: Findings) -> StatePatch:
        _, interpret = self.handler(ctx.procedure.id)
        return interpret(ctx, findings)

    async def run_rules(self, ctx: RunContext) -> RuleResult:
        """执行程序 YAML 中的规则, 参数取本次执行的参数快照. RuleError 原样上抛, 由 A1 按执行失败处理."""
        return await self.deps.rules.run(ctx.engagement.id, ctx.template, ctx.run, ctx.run.params_snapshot)

    def rule_exceptions(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """纯规则程序 (REV-08, CSH-05): 例外原样返回, 不作判断, 不建复核任务; 由之后的判断程序归因."""
        assert isinstance(findings, RuleResult)
        return {"exceptions": list(findings.exceptions), "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))]}

    async def judge_subjects(self, ctx: RunContext) -> list[Judged]:
        """判断程序的执行: 取判断对象 (rerun 时只留 rerun_subject_ids 中的, 已交人处理的不再重判), 并发逐个交判断引擎.

        任务说明含参数快照与本程序覆盖的风险 (Procedure.objective_ids 下的 RiskItem), 均为 JSON 形式;
        fixture_key 为对象 ID; performed_by 为项目负责人. 一个对象失败 (引擎返回 failure) 不影响其他对象;
        引擎抛异常时等其余对象结束再上抛.
        """
        subjects = await self.subject_sources[ctx.procedure.id](ctx)
        only = set(ctx.procedure.rerun_subject_ids)
        if only:
            subjects = [s for s in subjects if s.subject_id in only]
        waiting = subjects_awaiting_review(ctx)
        subjects = [s for s in subjects if s.subject_id not in waiting]
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
                fixture_key=subject.subject_id,
            )
            outcome = await self.deps.judgments.judge(
                ctx.engagement.id, ctx.template, ctx.run, jctx, ctx.engagement.owner_id
            )
            return Judged(jctx, outcome)

        return await gather_all(judge(s) for s in subjects)

    async def rev03_subjects(self, ctx: RunContext) -> list[Subject]:
        """REV-03 的判断对象: DOC-04 抽出的全部合同, 按合同编号排序.

        data 为 {contract, document} (REV-03.md.j2 头注释), document 取状态中该合同的文件登记; 不在状态中抛 LookupError
        (DOC-04 写合同与文件状态不在同一事务, 对不上说明存储与检查点不一致). 没有规则例外.
        """
        contracts = sorted(await self.deps.financial_store.contracts(ctx.engagement.id), key=lambda c: c.id)
        documents = {d.id: d for d in ctx.state["documents"]}
        subjects = []
        for contract in contracts:
            document = documents.get(contract.document_id)
            if document is None:
                raise LookupError(f"合同 {contract.id} 的文件 {contract.document_id} 不在状态中")
            data = {"contract": contract.model_dump(mode="json"), "document": document.model_dump(mode="json")}
            subjects.append(Subject(contract.id, data))
        return subjects

    def judgments_patch(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """判断程序的解读: 按 ReviewQueue.classify 逐个对象产出判断, 复核任务与例外新版本.

        - normal: 判断待复核, 以判断 v1 为目标建任务.
        - 不建任务: 判断第 1 版即 confirmed, 其解释的例外出新版本 attributed (attribute_exceptions).
        - unattributed: 第一条规则例外出新版本 unattributed, 以新版本为目标建任务 (带对象).
        - failed: 以本次执行 (成功终态) 为目标建带对象的 failed 任务.
        执行记录置成功终态: 个别对象失败不算整次失败.
        """
        assert isinstance(findings, list)
        now = datetime.now(UTC)
        run = finish_run(ctx.run, now)
        queue = self.deps.queue
        judgments: list[Judgment] = []
        exceptions: list[AuditException] = []
        tasks: list[ReviewTask] = []
        pid = ctx.procedure.id
        for judged in findings:
            subject_id = judged.ctx.subject_id
            findings_of_subject = judged.ctx.rule_findings
            kind = queue.classify(ctx.template, judged.outcome, findings_of_subject)
            judgment = judged.outcome.judgment
            if judgment is not None and kind is TaskKind.normal:
                judgments.append(judgment)
                tasks.append(queue.make_task(TaskKind.normal, judgment, pid, run.id, now))
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
        return {"judgments": judgments, "exceptions": exceptions, "review_tasks": tasks, "procedure_runs": [run]}
