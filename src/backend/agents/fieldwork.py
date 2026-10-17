"""A6: REV-08, REV-03, REV-02, CSH-05, CSH-02.

返回字段: exceptions, judgments, review_tasks, confirmations, procedure_runs.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from pydantic import TypeAdapter

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, finish_run, gather_all
from backend.agents.review import ReviewAgent
from backend.contracts.models import (
    AgentId,
    AuditException,
    Confirmation,
    ConfirmationResult,
    EvidencePointer,
    EvidenceType,
    ExceptionDisposition,
    Judgment,
    Reliability,
    ReviewStatus,
    ReviewTask,
    TargetType,
    TaskKind,
    TaskStatus,
)
from backend.contracts.records import BankAccount
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


@dataclass(frozen=True)
class CashFindings:
    """CSH-02 的原始发现: 回函与账面比对后的函证新版本与差异例外 (D01), 以及有差异账户的判断结果."""

    confirmations: list[Confirmation]  # 写入账面余额, 差额与结果的函证新版本
    exceptions: list[AuditException]  # 回函与账面不符的 D01 例外
    judged: list[Judged] = field(default_factory=list)  # 有差异账户的判断结果


Findings = RuleResult | list[Judged] | CashFindings  # 规则程序为规则结果, 判断程序为各对象的判断结果, CSH-02 为两者
Execute = Callable[[RunContext], Awaitable[Findings]]  # 执行一条程序, 返回原始发现
Interpret = Callable[[RunContext, Findings], StatePatch]  # 把原始发现转成状态片段
SubjectSource = Callable[[RunContext], Awaitable[list[Subject]]]  # 判断程序取全部判断对象
JSON_ROWS = TypeAdapter(list[dict[str, Any]])  # 规则返回的行转 JSON 形式 (日期为字符串), 供提示词 tojson


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


class FieldworkAgent(ProcedureAgent[Findings]):
    """A6: 执行收入与货币资金的实质性程序, 产出例外与判断.

    按程序编号分派到各自的执行与解读. 判断程序 (REV-03, REV-02, CSH-02) 共用判断流程, 各自只提供
    "取判断对象" (登记在 subject_sources); 尚未登记的执行时报未实现.
    """

    agent_id: ClassVar[AgentId] = AgentId.A6  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02"})  # 负责的程序

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        self.reviewer = ReviewAgent(deps)  # A7: 每生成一条模型类判断即同步调用其独立复核 (GEN-09)
        self.subject_sources: dict[str, SubjectSource] = {  # 判断程序编号 -> 取判断对象, 各判断程序接入时登记
            "REV-03": self.rev03_subjects,
            "REV-02": self.rev02_subjects,
        }

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读). 没有登记的程序抛 NotImplementedError."""
        handlers: dict[str, tuple[Execute, Interpret]] = {
            "REV-08": (self.run_rules, self.rule_exceptions),
            "CSH-05": (self.run_rules, self.rule_exceptions),
            "CSH-02": (self.assess_confirmations, self.cash_patch),
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
        return await self.judge_all(ctx, await self.subject_sources[ctx.procedure.id](ctx))

    async def judge_all(self, ctx: RunContext, subjects: list[Subject]) -> list[Judged]:
        """对给定的判断对象做判断 (judge_subjects 的后半, CSH-02 先比对余额再交它): 筛选同 judge_subjects."""
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
                fixture_key=subject.subject_id,
            )
            outcome = await self.deps.judgments.judge(
                ctx.engagement.id, ctx.template, ctx.run, jctx, ctx.engagement.owner_id
            )
            if outcome.judgment is not None:
                # 独立复核在分类与写入之前: 结论进置信度 (8.4), 不一致的由 ReviewQueue 建争议任务
                outcome = outcome.model_copy(update={"judgment": await self.reviewer.review(ctx, outcome.judgment)})
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

    async def rev02_subjects(self, ctx: RunContext) -> list[Subject]:
        """REV-02 的判断对象: 规则 S01 选中的发票, 加前置程序 (REV-08) 尚未归因的例外所指的发票 (Q13: R01, R02, R03
        都算), 去重后按发票 ID 排序; 先按 in_scope 筛选, 再取数据.

        data 字段按 REV-02.md.j2 头注释 (均为 JSON 形式): invoice 与 delivery_notes (按 ID 排序) 带 source_type;
        contract, document (状态中该合同的文件登记), contract_judgment (该合同当前有效的 REV-03 判断,
        见 contract_judgment) 在发票没有合同时为 null. 规则例外为该发票上前置程序尚未归因的例外 (按 ID 排序).
        发票, 合同或合同文件查不到抛 LookupError.
        """
        eid = ctx.engagement.id
        store = self.deps.financial_store
        rules = await self.run_rules(ctx)
        earlier = set(ctx.template.depends_on)
        open_findings = sorted(
            (
                x
                for x in ctx.state["exceptions"]
                if x.procedure_id in earlier and x.disposition is not ExceptionDisposition.attributed
            ),
            key=lambda x: x.id,
        )
        selected = {row["id"] for row in rules.rows.get("S01", [])}
        ids = sorted(i for i in selected | {i for x in open_findings for i in x.source_ids} if in_scope(ctx, i))
        if not ids:
            return []
        invoices = {i.id: i for i in await store.sales_invoices(eid, ids)}
        missing = [i for i in ids if i not in invoices]
        if missing:
            raise LookupError(f"发票不在财务数据中: {', '.join(missing)}")
        notes: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        for note in sorted(await store.delivery_notes(eid, ids), key=lambda n: n.id):
            notes[note.invoice_id].append({"source_type": "delivery_note", **note.model_dump(mode="json")})
        contract_ids = sorted({cast(str, invoices[i].contract_id) for i in ids if invoices[i].contract_id})
        contracts = {c.id: c for c in await store.contracts(eid, contract_ids)} if contract_ids else {}
        documents = {d.id: d for d in ctx.state["documents"]}
        subjects = []
        for invoice_id in ids:
            invoice = invoices[invoice_id]
            contract, document, judgment = None, None, None
            if invoice.contract_id is not None:
                found = contracts.get(invoice.contract_id)
                if found is None:
                    raise LookupError(f"发票 {invoice_id} 的合同 {invoice.contract_id} 不在财务数据中")
                doc = documents.get(found.document_id)
                if doc is None:
                    raise LookupError(f"合同 {found.id} 的文件 {found.document_id} 不在状态中")
                contract, document = found.model_dump(mode="json"), doc.model_dump(mode="json")
                judgment = self.contract_judgment(ctx, found.id)
            data = {
                "invoice": {"source_type": "sales_invoice", **invoice.model_dump(mode="json")},
                "delivery_notes": notes[invoice_id],
                "contract": contract,
                "document": document,
                "contract_judgment": judgment,
            }
            subjects.append(Subject(invoice_id, data, [x for x in open_findings if invoice_id in x.source_ids]))
        return subjects

    @staticmethod
    def contract_judgment(ctx: RunContext, contract_id: str) -> dict[str, Any] | None:
        """该合同当前有效的 REV-03 判断 (id, conclusion, review_status): 状态中该合同未被推翻的 REV-03 判断里最新的一条
        (被 override 时即人工判断); 没有为 None."""
        candidates = [
            j
            for j in ctx.state["judgments"]
            if j.procedure_id == "REV-03"
            and j.subject_id == contract_id
            and j.review_status is not ReviewStatus.overridden
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda j: (j.created_at, j.id))
        return latest.model_dump(mode="json", include={"id", "conclusion", "review_status"})

    async def assess_confirmations(self, ctx: RunContext) -> CashFindings:
        """CSH-02 的执行: 先做规则部分 (D01, 在代码中做), 再对有差异的账户组装上下文交判断引擎.

        规则部分逐个函证比较回函余额与本期试算表中该账户总账科目的余额: 相等时函证写账面余额, 差额 0, 结果 agreed;
        不等时结果 difference, 并建 D01 例外 X-CSH-02-{run_no}-D01-{n} (n 按银行账户 ID 排序编号, 金额为回函减账面,
        证据为回函余额所在页与试算表科目). 函证还没有回函余额时抛 ValueError. 账户的筛选同判断对象 (in_scope).
        判断对象为有 D01 的账户, 候选调节项取规则 D02, D03 的行 (按账户切分).
        """
        eid = ctx.engagement.id
        rules = await self.run_rules(ctx)
        accounts = {a.id: a for a in await self.deps.financial_store.bank_accounts(eid)}
        books = {r.account_code: r.balance_cents for r in await self.deps.financial_store.trial_balance(eid, "current")}
        confirmations = sorted(
            (c for c in ctx.state["confirmations"] if in_scope(ctx, c.bank_account_id)),
            key=lambda c: c.bank_account_id,
        )
        updated: list[Confirmation] = []
        exceptions: list[AuditException] = []
        subjects: list[Subject] = []
        for c in confirmations:
            if c.confirmed_balance_cents is None:
                raise ValueError(f"{c.id} 还没有回函余额 (DOC-06 未抽取)")
            account = accounts.get(c.bank_account_id)
            if account is None or account.gl_account_code not in books:
                raise LookupError(f"{c.id} 的账户 {c.bank_account_id} 或其总账科目不在财务数据中")
            book = books[account.gl_account_code]
            difference = c.confirmed_balance_cents - book
            result = ConfirmationResult.agreed if difference == 0 else ConfirmationResult.difference
            confirmation = c.model_copy(
                update={
                    "version": c.version + 1,
                    "book_balance_cents": book,
                    "difference_cents": difference,
                    "result": result,
                }
            )
            updated.append(confirmation)
            if difference:
                d01 = self.difference_exception(ctx, confirmation, account, len(exceptions) + 1)
                exceptions.append(d01)
                subjects.append(self.cash_subject(ctx, confirmation, account, d01, rules))
        return CashFindings(updated, exceptions, await self.judge_all(ctx, subjects))

    @staticmethod
    def difference_exception(
        ctx: RunContext, confirmation: Confirmation, account: BankAccount, n: int
    ) -> AuditException:
        """回函与账面不符的 D01 例外 (CSH-02.yaml 注释): 证据为回函余额所在页与试算表中的总账科目."""
        reply = [p for p in confirmation.evidence if p.locator == "confirmed_balance_cents"]
        ledger = EvidencePointer(
            type=EvidenceType.voucher,
            source_type="trial_balance",
            source_id=account.gl_account_code,
            locator="balance_cents",
            reliability=Reliability.internal_original,
        )
        return AuditException(
            id=f"X-CSH-02-{ctx.run.run_no}-D01-{n}",
            procedure_id=ctx.procedure.id,
            run_id=ctx.run.id,
            rule_id="D01",
            severity="medium",
            source_ids=[account.id],
            amount_cents=confirmation.difference_cents,
            detail={
                "confirmed_balance_cents": confirmation.confirmed_balance_cents,
                "book_balance_cents": confirmation.book_balance_cents,
                "gl_account_code": account.gl_account_code,
            },
            evidence=[*reply, ledger],
        )

    @staticmethod
    def cash_subject(
        ctx: RunContext, confirmation: Confirmation, account: BankAccount, d01: AuditException, rules: RuleResult
    ) -> Subject:
        """有差异账户的判断对象, data 字段按 CSH-02.md.j2 头注释 (均为 JSON 形式).

        规则例外为前置程序 (depends_on, 即 CSH-05) 在该账户上尚未归因的例外 (按 ID 排序), 再加本次的 D01.
        回函取状态 documents 中的记录, 不在状态中抛 LookupError.
        """
        documents = {d.id: d for d in ctx.state["documents"]}
        reply = documents.get(cast(str, confirmation.reply_document_id))
        if reply is None:
            raise LookupError(f"{confirmation.id} 的回函 {confirmation.reply_document_id} 不在状态中")
        earlier = set(ctx.template.depends_on)
        findings = sorted(
            (
                x
                for x in ctx.state["exceptions"]
                if x.procedure_id in earlier
                and account.id in x.source_ids
                and x.disposition is not ExceptionDisposition.attributed
            ),
            key=lambda x: x.id,
        )

        def rows(rule_id: str) -> list[dict[str, Any]]:
            mine = [r for r in rules.rows.get(rule_id, []) if r.get("bank_account_id") == account.id]
            return cast(list[dict[str, Any]], JSON_ROWS.dump_python(mine, mode="json"))

        data = {
            "bank_account": {"source_type": "bank_account", **account.model_dump(mode="json")},
            "confirmation": confirmation.model_dump(mode="json"),
            "reply_document": reply.model_dump(mode="json"),
            "book_balance_cents": confirmation.book_balance_cents,
            "difference_cents": confirmation.difference_cents,
            "subsequent_transactions": rows("D02"),
            "period_end_entries": rows("D03"),
        }
        return Subject(account.id, data, [*findings, d01])

    def cash_patch(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """CSH-02 的解读: 函证新版本与 D01 例外, 加上有差异账户的判断片段 (judgments_patch).

        判断失败时第一条规则例外可能就是本次的 D01, 片段里同一例外只留最高版本.
        """
        assert isinstance(findings, CashFindings)
        patch = self.judgments_patch(ctx, findings.judged)
        latest: dict[str, AuditException] = {}
        for x in [*findings.exceptions, *patch["exceptions"]]:
            if x.id not in latest or x.version > latest[x.id].version:
                latest[x.id] = x
        patch["confirmations"] = findings.confirmations
        patch["exceptions"] = list(latest.values())
        return patch

    def judgments_patch(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """判断程序的解读: 按 ReviewQueue.classify 逐个对象产出判断, 复核任务与例外新版本.

        - normal, dispute: 判断待复核 (dispute 为独立复核不一致, 由审计师仲裁), 以判断 v1 为目标建任务.
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
        return {"judgments": judgments, "exceptions": exceptions, "review_tasks": tasks, "procedure_runs": [run]}
