"""A6: REV-08, REV-03, REV-02, CSH-05, CSH-02, GEN-05, GEN-02, GEN-03, GEN-06.

返回字段: exceptions, judgments, review_tasks, confirmations, procedure_runs.
"""

import functools
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, ClassVar, cast

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, rule_patch, run_rules
from backend.agents.evidence import current_confirmations, legacy_accounts
from backend.agents.going_concern import gen06_subjects
from backend.agents.ingest import client_profile
from backend.agents.journals import JSON_ROWS, entry_order, journal_rows
from backend.agents.judging import (
    Judged,
    JudgedRun,
    JudgingSteps,
    Subject,
    Subjects,
    earlier_findings,
    in_scope,
    judge_all,
    judging_steps,
    judgments_patch,
    open_findings,
    subjects_awaiting_review,
)
from backend.agents.ownership import declared
from backend.agents.review import ReviewAgent
from backend.contracts.models import (
    AgentId,
    AuditException,
    ClientProfile,
    Confirmation,
    ConfirmationResult,
    Contract,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    OwnershipGraph,
    PartyNode,
    Reliability,
    ReviewStatus,
    RuleOutcome,
)
from backend.contracts.records import BankAccount, JournalEntry, SalesInvoice
from backend.contracts.services import RuleResult
from backend.contracts.state import StatePatch
from backend.services.judgment_engine import document_pointer
from backend.services.party_names import party_name_key
from backend.services.staleness import current_results


@dataclass(frozen=True)
class CashFindings:
    """CSH-02 的原始发现: 回函与账面比对后的函证新版本, 规则部分的例外 (D01, D04), 要判断的账户的判断结果, 以及规则
    引擎没有执行的规则 (D02, D03 的执行前检查命中时, 需求 R130)."""

    confirmations: list[Confirmation]  # 写入账面余额, 差额与结果的函证新版本
    exceptions: list[AuditException]  # 本次新建的 D01 (回函与账面不符), D04 (回函应计利息未入账); 沿用的不在内
    judged: list[Judged] = field(default_factory=list)  # 有 D01 或 D04 的账户的判断结果
    outcomes: list[RuleOutcome] = field(default_factory=list)  # 规则引擎没有执行的规则


# 规则程序为规则结果, 判断程序为各对象的判断结果 (带取对象时没有执行的规则), CSH-02 为两者
Findings = RuleResult | JudgedRun | CashFindings


def accrued_interest_unbooked(confirmation: Confirmation, lines: Sequence[dict[str, Any]], trivial_cents: int) -> bool:
    """CSH-02 D04 的判定: 回函写明的应计利息不为空, 绝对值超过明显微小 (等于不算), 且 D04 的候选行 (窗口内全部科目
    的分录行, 已剔除作废对) 中没有借方或贷方等于该绝对值的行 (有即视为已计提). 纯函数."""
    interest = confirmation.accrued_interest_cents
    if interest is None or abs(interest) <= trivial_cents:
        return False
    return not any(abs(interest) in (line.get("debit_cents"), line.get("credit_cents")) for line in lines)


CASH_CATEGORY = "cash"  # 货币资金的标准分类: GEN-02 的凭证涉及它时另取银行账户与对账单行


def detail_entries(detail: Mapping[str, Any]) -> list[str]:
    """GEN-05 例外 detail 直接写出的其他凭证号: original_entry_no, reversal_entry_no (R11) 与 full_credits (R12).
    按键取, 不按规则编号; paired_invoices 写的是分录行, 见 detail_lines."""
    found = [detail.get("original_entry_no"), detail.get("reversal_entry_no"), *(detail.get("full_credits") or [])]
    return [str(no) for no in found if no]


def detail_lines(detail: Mapping[str, Any]) -> list[str]:
    """GEN-05 例外 detail 所指的其他发票的分录行: paired_invoices 的 journal_entry_id (R12; 为空的不算)."""
    return [str(p["journal_entry_id"]) for p in detail.get("paired_invoices") or [] if p.get("journal_entry_id")]


def detail_invoices(detail: Mapping[str, Any]) -> list[str]:
    """GEN-05 例外 detail 所指的发票: matched_invoices (R10), invoice_id 与 paired_invoices 的 invoice_id (R12)."""
    paired = [p.get("invoice_id") for p in detail.get("paired_invoices") or []]
    return [str(i) for i in [*(detail.get("matched_invoices") or []), detail.get("invoice_id"), *paired] if i]


Execute = Callable[[RunContext], Awaitable[Findings]]  # 执行一条程序, 返回原始发现
Interpret = Callable[[RunContext, Findings], StatePatch]  # 把原始发现转成状态片段
SubjectSource = Callable[[RunContext], Awaitable[Subjects]]  # 判断程序取全部判断对象, 带取对象时没有执行的规则
# 判断公共流程的名字移到 agents/judging.py, 这里再导出, 原有的导入路径照用
__all__ = ["CashFindings", "FieldworkAgent", "Judged", "Subject", "Subjects", "in_scope", "subjects_awaiting_review"]


class FieldworkAgent(ProcedureAgent[Findings]):
    """A6: 执行收入与货币资金的实质性程序, 全量分录扫描 (GEN-05), 分录例外归因 (GEN-02), 关联方图谱研判 (GEN-03) 与
    期后事项研判 (GEN-06), 产出例外与判断.

    按程序编号分派到各自的执行与解读. 判断程序 (REV-03, REV-02, CSH-02, GEN-02, GEN-03, GEN-06) 共用判断公共流程
    (agents/judging.py), 各自只提供 "取判断对象" (登记在 subject_sources; GEN-06 的在 agents/going_concern.py);
    尚未登记的执行时报未实现.
    """

    agent_id: ClassVar[AgentId] = AgentId.A6  # 智能体编号
    # 负责的程序 (GEN-05, GEN-02 属功能 journals, GEN-03 属 related_parties, GEN-06 属 going_concern, 未启用时注册表
    # 不映射)
    procedures: ClassVar[frozenset[str]] = frozenset(
        {"REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02", "GEN-05", "GEN-02", "GEN-03", "GEN-06"}
    )

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        self.reviewer = ReviewAgent(deps)  # A7: 每生成一条模型类判断即同步调用其独立复核 (GEN-09)
        self.steps: JudgingSteps = judging_steps(deps)  # 复核之后, 分类之前的判断级降级 (功能 autonomy 关闭时原样返回)
        self.subject_sources: dict[str, SubjectSource] = {  # 判断程序编号 -> 取判断对象, 各判断程序接入时登记
            "REV-03": self.rev03_subjects,
            "REV-02": self.rev02_subjects,
            "GEN-02": self.gen02_subjects,
            "GEN-03": self.gen03_subjects,
            "GEN-06": self.gen06_subjects,
        }

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读). 没有登记的程序抛 NotImplementedError."""
        handlers: dict[str, tuple[Execute, Interpret]] = {
            "REV-08": (functools.partial(run_rules, self.deps), rule_patch),  # 纯规则程序 (同 A5 的 GEN-07)
            "CSH-05": (functools.partial(run_rules, self.deps), rule_patch),
            "GEN-05": (functools.partial(run_rules, self.deps), rule_patch),
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

    async def judge_subjects(self, ctx: RunContext) -> JudgedRun:
        """判断程序的执行: 取判断对象 (rerun 时只留 rerun_subject_ids 中的, 已交人处理的不再重判), 并发逐个交判断引擎.

        任务说明含参数快照与本程序覆盖的风险 (Procedure.objective_ids 下的 RiskItem), 均为 JSON 形式;
        fixture_key 为对象 ID; performed_by 为项目负责人. 一个对象失败 (引擎返回 failure) 不影响其他对象;
        引擎抛异常时等其余对象结束再上抛. 取对象时规则引擎没有执行的规则 (Subjects.outcomes) 随结果交到解读.
        """
        subjects = await self.subject_sources[ctx.procedure.id](ctx)
        return JudgedRun(await self.judge_all(ctx, subjects), subjects.outcomes)

    async def judge_all(self, ctx: RunContext, subjects: list[Subject]) -> list[Judged]:
        """对给定的判断对象走判断公共流程 (judging.judge_all; CSH-02 先比对余额再交它).
        A7 复核与两个可替换步骤取自本 Agent (reviewer, steps)."""
        return await judge_all(self.deps, ctx, subjects, self.reviewer, self.steps)

    async def rev03_subjects(self, ctx: RunContext) -> Subjects:
        """REV-03 的判断对象: DOC-04 抽出的全部合同, 按合同编号排序.

        data 为 {contract, document} (REV-03.md.j2 头注释), document 为该合同的文件登记 (见 registered_documents);
        查不到抛 LookupError. 没有规则例外.
        """
        contracts = sorted(await self.deps.financial_store.contracts(ctx.engagement.id), key=lambda c: c.id)
        documents = await self.registered_documents(ctx)
        subjects = []
        for contract in contracts:
            document = documents.get(contract.document_id)
            if document is None:
                raise LookupError(f"合同 {contract.id} 的文件 {contract.document_id} 未登记")
            data = {"contract": contract.model_dump(mode="json"), "document": document.model_dump(mode="json")}
            subjects.append(Subject(contract.id, data))
        return Subjects(subjects)

    async def related_documents(self, eid: str, related_id: str) -> list[dict[str, Any]]:
        """关联对象含 related_id 的已登记文件 (FinancialDataStore.documents(related_id=...) 的顺序), 每项为 {document
        (DocumentRef, JSON 形式), pages (EvidenceStore.pages, 下标 0 为第 1 页)}; GEN-02, GEN-03, GEN-06 共用. 登记摘录
        (registry_extract) 不给 (需求 R310): 摘录页印有人员的出生日期, 人员检索的文件名带 yyyymmdd, 判断引擎的遮蔽
        只认人员节点 ID 与 birth_date 字段, 管不到页面文字."""
        return [
            {"document": d.model_dump(mode="json"), "pages": await self.deps.evidence_store.pages(eid, d.id)}
            for d in await self.deps.financial_store.documents(eid, related_id=related_id)
            if d.type is not DocumentType.registry_extract
        ]

    async def registered_documents(self, ctx: RunContext) -> dict[str, DocumentRef]:
        """文件登记, 以存储 (FinancialDataStore.documents) 的当前登记为准: 可靠性, 文件日期与关联对象是文件登记的属性,
        审计师补登记后 (需求 R90) 过期重跑用的就是新登记, 不用编排状态里的旧副本; 存储里没有而状态里有的才取状态.
        审计师在 DOC-04 之后上传的合同由 A3 直接分类与抽取 (需求 R14), 文件登记只在存储里, 不经编排并入状态."""
        documents = {d.id: d for d in ctx.state["documents"]}
        documents.update({d.id: d for d in await self.deps.financial_store.documents(ctx.engagement.id)})
        return documents

    async def rev02_subjects(self, ctx: RunContext) -> Subjects:
        """REV-02 的判断对象: 规则 S01 选中的发票, 加前置程序 (REV-08) 尚未归因的例外所指的发票 (Q13: R01, R02, R03
        都算; 只看当前结果, 过期重跑取代的执行的例外不算, services.staleness), 去重后按发票 ID 排序; 先按 in_scope
        筛选, 再取数据.

        data 字段按 REV-02.md.j2 头注释 (均为 JSON 形式): invoice, delivery_notes, contract 与 document 见
        invoice_records; journal_lines 为该发票过账分录的全部行 (见 invoice_journal_lines), 发票没有 journal_entry_id 时
        为 []; contract_judgment 为该合同当前有效的 REV-03 判断 (见 contract_judgment), 发票没有合同时与 contract,
        document 同为 null. 规则例外为该发票上前置程序尚未归因的例外 (按 ID 排序).
        发票, 发票指向的分录行, 合同或合同文件查不到抛 LookupError.
        """
        eid = ctx.engagement.id
        rules = await run_rules(self.deps, ctx)
        earlier = open_findings(ctx.state, ctx.template.depends_on)
        selected = {row["id"] for row in rules.rows.get("S01", [])}
        ids = sorted(i for i in selected | {i for x in earlier for i in x.source_ids} if in_scope(ctx, i))
        if not ids:
            return Subjects((), rules.outcomes)
        invoices = await self.invoice_records(ctx, ids)
        journal_lines = await self.invoice_journal_lines(eid, [invoices[i][0] for i in ids])
        subjects = []
        for invoice_id in ids:
            invoice, record = invoices[invoice_id]
            judgment = self.contract_judgment(ctx, invoice.contract_id) if invoice.contract_id is not None else None
            data = record | {"journal_lines": journal_lines[invoice_id], "contract_judgment": judgment}
            subjects.append(Subject(invoice_id, data, [x for x in earlier if invoice_id in x.source_ids]))
        return Subjects(subjects, rules.outcomes)

    async def invoice_records(
        self, ctx: RunContext, ids: Sequence[str]
    ) -> dict[str, tuple[SalesInvoice, dict[str, Any]]]:
        """发票与它的单据 (REV-02 与 GEN-02 共用): 发票 ID -> (发票, {invoice, delivery_notes, contract, document}).

        invoice 与 delivery_notes (按 ID 排序) 为带 source_type 的行, contract 为 DOC-04 抽取的合同, document 为该合同的
        文件登记 (见 registered_documents), 均为 JSON 形式; 发票没有合同时后两项为 None. 发票, 合同或合同文件查不到抛
        LookupError."""
        eid = ctx.engagement.id
        store = self.deps.financial_store
        invoices = {i.id: i for i in await store.sales_invoices(eid, ids)}
        missing = [i for i in ids if i not in invoices]
        if missing:
            raise LookupError(f"发票不在财务数据中: {', '.join(missing)}")
        notes: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        for note in sorted(await store.delivery_notes(eid, ids), key=lambda n: n.id):
            notes[note.invoice_id].append({"source_type": "delivery_note", **note.model_dump(mode="json")})
        contract_ids = sorted({cast(str, invoices[i].contract_id) for i in ids if invoices[i].contract_id})
        contracts = {c.id: c for c in await store.contracts(eid, contract_ids)} if contract_ids else {}
        documents = await self.registered_documents(ctx) if contracts else {}
        found: dict[str, tuple[SalesInvoice, dict[str, Any]]] = {}
        for invoice_id in ids:
            invoice = invoices[invoice_id]
            contract, document = None, None
            if invoice.contract_id is not None:
                matched = contracts.get(invoice.contract_id)
                if matched is None:
                    raise LookupError(f"发票 {invoice_id} 的合同 {invoice.contract_id} 不在财务数据中")
                doc = documents.get(matched.document_id)
                if doc is None:
                    raise LookupError(f"合同 {matched.id} 的文件 {matched.document_id} 未登记")
                contract, document = matched.model_dump(mode="json"), doc.model_dump(mode="json")
            record = {
                "invoice": {"source_type": "sales_invoice", **invoice.model_dump(mode="json")},
                "delivery_notes": notes[invoice_id],
                "contract": contract,
                "document": document,
            }
            found[invoice_id] = (invoice, record)
        return found

    async def gen03_subjects(self, ctx: RunContext) -> Subjects:
        """GEN-03 的判断对象: 关系图 (GEN-08) 中报告主体以外的公司节点, 按节点 ID 排序; 先按 in_scope 筛选, 再取数据.
        状态中没有关系图抛 LookupError.

        规则 C01, C02 的参数为参数快照加 party_names, party_acns (各对象节点的名称与 ACN, 一一对应), 不写回快照. data 见
        gen03_data; 规则例外为前置程序在该节点上尚未归因的例外 (只看当前结果, 按 ID 排序); 对象带本项目采用的那版
        客户画像 (需求 R128, 查不到抛 LookupError), 推理链可引用它.
        """
        graph = ctx.state["ownership_graph"]
        if graph is None:
            raise LookupError("GEN-03: 状态中没有关系图 (GEN-08 未执行)")
        companies = sorted(
            (
                n
                for n in graph.nodes
                if n.kind == "company" and n.id != graph.reporting_entity_id and in_scope(ctx, n.id)
            ),
            key=lambda n: n.id,
        )
        if not companies:
            return Subjects()
        eid = ctx.engagement.id
        parties = {"party_names": [n.name for n in companies], "party_acns": [n.acn for n in companies]}
        rules = await self.deps.rules.run(eid, ctx.template, ctx.run, {**ctx.run.params_snapshot, **parties})
        profile = await client_profile(self.deps, ctx.engagement, ctx.state["client_profile_version"])
        contracts = sorted(await self.deps.financial_store.contracts(eid), key=lambda c: c.id)
        documents = await self.registered_documents(ctx) if contracts else {}
        earlier = open_findings(ctx.state, ctx.template.depends_on)
        subjects = []
        for node in companies:
            data = await self.gen03_data(ctx, graph, node, rules, profile, contracts, documents)
            findings = [x for x in earlier if node.id in x.source_ids]
            subjects.append(Subject(node.id, data, findings, client_profile=profile))
        return Subjects(subjects, rules.outcomes)

    async def gen06_subjects(self, ctx: RunContext) -> Subjects:
        """GEN-06 期后事项研判的判断对象与 data (需求 R179): 见 going_concern.gen06_subjects, 已登记文件经
        related_documents 取 (不给登记摘录)."""
        return await gen06_subjects(self.deps, ctx, self.related_documents)

    async def gen03_data(
        self,
        ctx: RunContext,
        graph: OwnershipGraph,
        node: PartyNode,
        rules: RuleResult,
        profile: ClientProfile,
        contracts: Sequence[Contract],
        documents: Mapping[str, DocumentRef],
    ) -> dict[str, Any]:
        """一个公司节点的 data, 字段按 GEN-03.md.j2 头注释 (均为 JSON 形式). 名称键为节点名称经 party_name_key (去
        entity_suffixes, 同规则 SQL), 规则 C01 中 party_key 等于它的行归本节点 (record 展开, 带 source_type).

        - links: 一端为本节点的关系 (关系图的顺序); persons: 这些关系另一端的人员 (按 ID), 带该人与报告主体之间的关系
          (links_with_entity, 写法同 links, 带报告主体摘录那一行的证据指针), 不带出生日期; 人员节点 ID 的出生日期在
          渲染前由判断引擎遮蔽.
        - declared: 画像申报清单中与本节点匹配的一项 (ownership.declared, 同 GEN-08 R01 的口径), 没有为 null.
        - equity_entries: 本节点直接持有报告主体股份时为规则 C02 的行, 否则为 [].
        - contracts: 对方 ABN (去非数字) 的末 9 位等于节点 ACN, 或对方名称的键等于名称键的合同 (按合同编号), 带合同
          文件的登记 (见 registered_documents, 未登记抛 LookupError).
        - documents: 关联对象含本节点的文件, 带各页文字 (related_documents, 不给登记摘录; 摘录的内容已在关系图里).
        - reporting_entity, client_profile: 报告主体节点的 ID 与名称; 画像的 ID (client_profile 指针用) 与适用框架.
        """
        eid = ctx.engagement.id
        suffixes = list(ctx.run.params_snapshot["entity_suffixes"])
        key = party_name_key(node.name, (), suffixes)
        entity = graph.reporting_entity_id
        nodes = {n.id: n for n in graph.nodes}
        links = [x for x in graph.links if node.id in (x.from_id, x.to_id)]
        persons = [
            {
                "id": person,
                "name": nodes[person].name,
                "links_with_entity": [
                    x.model_dump(mode="json") for x in graph.links if x.from_id == person and x.to_id == entity
                ],
            }
            for person in sorted({x.from_id for x in links if nodes[x.from_id].kind == "person"})
        ]
        mine = [r for r in rules.rows.get("C01", []) if r["party_key"] == key]

        def rows(source_type: str) -> list[dict[str, Any]]:
            return [{"source_type": source_type, **r["record"]} for r in mine if r["source_type"] == source_type]

        holds = any(x.from_id == node.id and x.to_id == entity and x.relation == "shareholder" for x in graph.links)
        equity = JSON_ROWS.dump_python(rules.rows.get("C02", []), mode="json") if holds else []
        own: list[dict[str, Any]] = []
        for contract in contracts:
            abn = "".join(c for c in contract.counterparty_abn or "" if c.isdigit())
            same_name = key is not None and party_name_key(contract.counterparty, (), suffixes) == key
            if not (node.acn is not None and abn[-9:] == node.acn) and not same_name:
                continue
            document = documents.get(contract.document_id)
            if document is None:
                raise LookupError(f"合同 {contract.id} 的文件 {contract.document_id} 未登记")
            own.append({"contract": contract.model_dump(mode="json"), "document": document.model_dump(mode="json")})
        related = await self.related_documents(eid, node.id)
        found = declared(profile, node.name, node.acn, suffixes)
        return {
            "entity": {"name": node.name, "acn": node.acn},
            "reporting_entity": {"id": entity, "name": nodes[entity].name},
            "client_profile": {"id": profile.id, "applicable_framework": profile.applicable_framework},
            "links": [x.model_dump(mode="json") for x in links],
            "persons": persons,
            "declared": found.model_dump(mode="json") if found is not None else None,
            "transactions": {
                "entries": rows("journal_entry"),
                "bank_lines": rows("bank_transaction"),
                "invoices": rows("sales_invoice"),
            },
            "equity_entries": equity,
            "contracts": own,
            "documents": related,
        }

    async def invoice_journal_lines(
        self, eid: str, invoices: Sequence[SalesInvoice]
    ) -> dict[str, list[dict[str, Any]]]:
        """各发票过账分录的全部行 (需求 R100): journal_entry_id 指向的行经 record 取得凭证号, 再按凭证号一次取齐,
        按行 ID 排序; 每行为 {"source_type": "journal_entry", 分录行各列 (JSON 形式), account_name, account_category}
        (科目表里没有该科目时两项为 null). journal_entry_id 为空的发票为 []; 指向的行查不到抛 LookupError."""
        store = self.deps.financial_store
        entry_nos: dict[str, str] = {}
        for invoice in invoices:
            if invoice.journal_entry_id is None:
                continue
            row = await store.record(eid, "journal_entry", invoice.journal_entry_id)
            if row is None:
                raise LookupError(f"发票 {invoice.id} 的分录行 {invoice.journal_entry_id} 不在财务数据中")
            entry_nos[invoice.id] = str(row["entry_no"])
        found: dict[str, list[dict[str, Any]]] = {i.id: [] for i in invoices}
        if not entry_nos:
            return found
        lines = sorted(await store.journal_entries(eid, entry_nos=sorted(set(entry_nos.values()))), key=lambda x: x.id)
        chart = {a.code: a for a in await store.accounts(eid, sorted({x.account_code for x in lines}))}
        by_entry: dict[str, list[dict[str, Any]]] = {}
        for line in lines:
            account = chart.get(line.account_code)
            by_entry.setdefault(line.entry_no, []).append(
                {
                    "source_type": "journal_entry",
                    **line.model_dump(mode="json"),
                    "account_name": account.name if account is not None else None,
                    "account_category": account.category if account is not None else None,
                }
            )
        for invoice_id, entry_no in entry_nos.items():
            found[invoice_id] = by_entry.get(entry_no, [])
        return found

    @staticmethod
    def contract_judgment(ctx: RunContext, contract_id: str) -> dict[str, Any] | None:
        """该合同当前有效的 REV-03 判断 (id, conclusion, review_status): 状态中该合同未被推翻的 REV-03 判断里最新的一条
        (被 override 时即人工判断; 只看当前结果, 过期重跑取代的执行的不算); 没有为 None."""
        candidates = [
            j
            for j in current_results(ctx.state).judgments
            if j.procedure_id == "REV-03"
            and j.subject_id == contract_id
            and j.review_status is not ReviewStatus.overridden
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda j: (j.created_at, j.id))
        return latest.model_dump(mode="json", include={"id", "conclusion", "review_status"})

    async def gen02_subjects(self, ctx: RunContext) -> Subjects:
        """GEN-02 的判断对象: 前置程序 (GEN-05) 尚未归因的例外按 detail 的 entry_no 分组, 每笔凭证一个对象, 按凭证号
        自然序 (entry_order); 先按 in_scope 筛选, 再取数据. 规则例外为该凭证上尚未归因的例外 (按 ID 排序), fixture_key
        为凭证号. 只看当前结果 (services.staleness); GEN-05 的例外 detail 缺 entry_no 时抛 ValueError.

        data 字段按 GEN-02.md.j2 头注释 (均为 JSON 形式, 行带 source_type): entry_lines 为本凭证的全部分录行;
        account_balances 为本凭证各科目的科目表信息与两期试算表余额; bank 见 voucher_bank; related_entries 为相关凭证
        的全部分录行: 本凭证例外 detail 所指的凭证 (detail_entries, detail_lines 所在凭证) 与 detail 指向本凭证的其他
        GEN-05 例外所在的凭证 (不含本凭证, 按自然序与行 ID); related_invoices 为本凭证例外 detail 所指的发票
        (detail_invoices, 按 ID), 每张为 invoice_records 的 invoice 与 delivery_notes; contracts 为这些发票所挂的合同
        与其文件登记 (invoice_records 的 contract 与 document), 按文件 ID 去重, 按合同编号排序, 同一份合同只送一次,
        发票以 contract_id 引用; related_findings 为 detail 指向本凭证的其他 GEN-05 例外 (按 ID; 已归因的也算);
        documents 为关联对象含本凭证号的文件登记与各页文字 (related_documents, 整份送出, 暂不设页数上限);
        fraud_risks 为风险清单中 fraud_related 为真的全部风险. 本凭证, 相关凭证或 detail 所指的分录行查不到抛
        LookupError, 发票与合同同 invoice_records. 没有规则 SQL, 不经规则引擎.
        """
        eid = ctx.engagement.id
        store = self.deps.financial_store
        earlier = set(ctx.template.depends_on)
        scan = sorted(
            (x for x in current_results(ctx.state).exceptions if x.procedure_id in earlier), key=lambda x: x.id
        )
        entry_of: dict[str, str] = {}
        for x in scan:
            entry_no = x.detail.get("entry_no")
            if not isinstance(entry_no, str) or not entry_no:
                raise ValueError(f"{x.id} 的 detail 没有 entry_no")
            entry_of[x.id] = entry_no
        # 判断对象只看未归因的 (open_findings); scan 含已归因的, 供 related_findings 与相关凭证
        groups: dict[str, list[AuditException]] = {}
        for x in open_findings(ctx.state, ctx.template.depends_on):
            if in_scope(ctx, entry_of[x.id]):
                groups.setdefault(entry_of[x.id], []).append(x)
        if not groups:
            return Subjects()
        refers = await self.referenced_entries(eid, scan)  # 例外 ID -> detail 所指的其他凭证号
        vouchers = sorted(groups, key=entry_order)
        related: dict[str, list[str]] = {}
        referencing: dict[str, list[AuditException]] = {}
        for no in vouchers:
            referencing[no] = [x for x in scan if entry_of[x.id] != no and no in refers[x.id]]
            cited = {r for x in groups[no] for r in refers[x.id]} | {entry_of[x.id] for x in referencing[no]}
            related[no] = sorted(cited - {no}, key=entry_order)
        wanted = sorted({*vouchers, *(r for no in vouchers for r in related[no])}, key=entry_order)
        lines: dict[str, list[JournalEntry]] = {no: [] for no in wanted}
        for line in await store.journal_entries(eid, entry_nos=wanted):
            lines[line.entry_no].append(line)
        missing = [no for no in wanted if not lines[no]]
        if missing:
            raise LookupError(f"凭证不在财务数据中: {', '.join(missing)}")
        codes = sorted({line.account_code for no in vouchers for line in lines[no]})
        chart = {a.code: a for a in await store.accounts(eid, codes)}
        current = {r.account_code: r.balance_cents for r in await store.trial_balance(eid, "current")}
        prior = {r.account_code: r.balance_cents for r in await store.trial_balance(eid, "prior")}
        invoice_ids = sorted({i for no in vouchers for x in groups[no] for i in detail_invoices(x.detail)})
        invoices = await self.invoice_records(ctx, invoice_ids) if invoice_ids else {}
        cash = {code for code in codes if code in chart and chart[code].category == CASH_CATEGORY}
        bank_accounts = sorted(await store.bank_accounts(eid), key=lambda a: a.id) if cash else []
        fraud_risks = [r.model_dump(mode="json") for r in ctx.state["risk_register"] if r.fraud_related]
        subjects = []
        for no in vouchers:
            own = lines[no]
            balances = []
            for code in sorted({line.account_code for line in own}):
                account = chart.get(code)
                balances.append(
                    {
                        "source_type": "trial_balance",
                        "code": code,
                        "name": account.name if account is not None else None,
                        "category": account.category if account is not None else None,
                        "current_cents": current.get(code),
                        "prior_cents": prior.get(code),
                    }
                )
            touched = {line.account_code for line in own} & cash
            linked = [a for a in bank_accounts if a.gl_account_code in touched]
            documents = await self.related_documents(eid, no)
            records = [invoices[i][1] for i in sorted({i for x in groups[no] for i in detail_invoices(x.detail)})]
            contracts: dict[str, dict[str, Any]] = {}  # 文件 ID -> {contract, document}, 同一份合同只送一次
            for record in records:
                if record["document"] is not None:
                    contracts.setdefault(
                        record["document"]["id"], {"contract": record["contract"], "document": record["document"]}
                    )
            data = {
                "entry_lines": journal_rows(own),
                "account_balances": balances,
                "bank": await self.voucher_bank(ctx, own, linked) if touched else None,
                "related_entries": journal_rows([line for r in related[no] for line in lines[r]]),
                "related_invoices": [{"invoice": r["invoice"], "delivery_notes": r["delivery_notes"]} for r in records],
                "contracts": sorted(contracts.values(), key=lambda c: str(c["contract"]["id"])),
                "related_findings": [x.model_dump(mode="json") for x in referencing[no]],
                "documents": documents,
                "fraud_risks": fraud_risks,
            }
            subjects.append(Subject(no, data, groups[no]))
        return Subjects(subjects)

    async def referenced_entries(self, eid: str, findings: Sequence[AuditException]) -> dict[str, set[str]]:
        """各 GEN-05 例外 detail 所指的凭证号: detail_entries 直接写出的, 加 detail_lines 的分录行经 record 取得的凭证号
        (同一行只查一次). 分录行查不到抛 LookupError."""
        entry_of_line: dict[str, str] = {}
        found: dict[str, set[str]] = {}
        for x in findings:
            refs = set(detail_entries(x.detail))
            for line_id in detail_lines(x.detail):
                if line_id not in entry_of_line:
                    row = await self.deps.financial_store.record(eid, "journal_entry", line_id)
                    if row is None:
                        raise LookupError(f"{x.id} 所指的分录行 {line_id} 不在财务数据中")
                    entry_of_line[line_id] = str(row["entry_no"])
                refs.add(entry_of_line[line_id])
            found[x.id] = refs
        return found

    async def voucher_bank(
        self, ctx: RunContext, lines: Sequence[JournalEntry], accounts: Sequence[BankAccount]
    ) -> dict[str, Any]:
        """凭证涉及货币资金科目时的银行上下文: accounts 为这些科目对应的银行账户 (按 ID, 带 source_type); lines 为各账户
        的对账单行中, 交易日期在凭证日 (分录行的 posting_date, 有多个时取最早与最晚) 前后 bank_window_days 天内 (含
        两端) 的行, 加该账户本期 (项目期间) 的第一行, 去重后按 (账户, 日期, ID) 排序, 带 source_type."""
        window = timedelta(days=int(ctx.run.params_snapshot["bank_window_days"]))
        start, end = ctx.engagement.period_start, ctx.engagement.period_end
        low, high = min(x.posting_date for x in lines) - window, max(x.posting_date for x in lines) + window
        found: list[dict[str, Any]] = []
        for account in accounts:
            rows = await self.deps.financial_store.bank_transactions(
                ctx.engagement.id, account.id, min(start, low), max(end, high)
            )
            first = next((r for r in rows if start <= r.txn_date <= end), None)
            chosen = {r.id: r for r in rows if low <= r.txn_date <= high} | ({first.id: first} if first else {})
            for row in sorted(chosen.values(), key=lambda r: (r.txn_date, r.id)):
                found.append({"source_type": "bank_transaction", **row.model_dump(mode="json")})
        return {
            "accounts": [{"source_type": "bank_account", **a.model_dump(mode="json")} for a in accounts],
            "lines": found,
        }

    async def assess_confirmations(self, ctx: RunContext) -> CashFindings:
        """CSH-02 的执行: 先做规则部分 (D01, D04, 在代码中做), 再对有例外的账户组装上下文交判断引擎.

        只处理当前的函证 (账户仍在银行账户表中且识别信息没变, evidence.current_confirmations), 删掉或识别信息变了的
        账户的旧函证留作历史. 规则部分逐个函证比较回函余额与本期试算表中该账户总账科目的余额: 相等时函证写账面
        余额, 差额 0, 结果 agreed; 不等时结果 difference, 并建 D01 例外 X-CSH-02-{run_no}-D01-{n} (n 按银行账户 ID
        排序编号, 金额为回函减账面, 证据为回函余额所在页与试算表科目). 回函应计利息未入账时另建 D04 例外
        (见 accrued_interest_exception, 在 D04 内同样按账户编号), 函证结果不变 (余额相符的仍为 agreed).
        函证还没有回函余额时抛 ValueError. 账户的筛选同判断对象 (in_scope).
        判断对象为有 D01 或 D04 的账户 (按账户 ID, 每个账户一次), 候选调节项取规则 D02, D03 的行 (按账户切分).
        当前结果里该账户已有本程序尚未归因的 D01, D04 (judging.earlier_findings) 时沿用, 不再新建 (需求 R347, 审核
        R347-02, R347-11): 判断以它们为规则例外, 确认时给同一 ID 出 attributed 新版本. 若照常新建 (ID 带 run_no), 此前
        判断失败时已置 unattributed 的那条就没有任务再指向它 (重跑不取代旧执行, 两条都在当前结果里; 部分重跑整次失败后
        的整条重跑也是). 首次执行与过期重跑 (当前结果不含被取代的执行) 没有此前的, 照常按本次比对新建.
        """
        eid = ctx.engagement.id
        rules = await run_rules(self.deps, ctx)
        accounts = {a.id: a for a in await self.deps.financial_store.bank_accounts(eid)}
        books = {r.account_code: r.balance_cents for r in await self.deps.financial_store.trial_balance(eid, "current")}
        legacy = await legacy_accounts(self.deps, eid, accounts.values())
        confirmations = sorted(
            (
                c
                for c in current_confirmations(ctx.state["confirmations"], eid, accounts.values(), legacy)
                if in_scope(ctx, c.bank_account_id)
            ),
            key=lambda c: c.bank_account_id,
        )
        documents = await self.registered_documents(ctx)
        params = ctx.run.params_snapshot
        period_end = date.fromisoformat(str(params["period_end"]))  # 参数快照经 Store 读回时日期为字符串
        window = (period_end - timedelta(days=int(params["lookback_days"])), period_end)  # D04 的窗口, 同 D03
        updated: list[Confirmation] = []
        differences: list[AuditException] = []  # D01, 按账户编号
        accrued: list[AuditException] = []  # D04, 按账户编号
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
            unbooked = accrued_interest_unbooked(
                confirmation, rules.rows.get("D04", []), params["clearly_trivial_cents"]
            )
            # 有此前未归因的就沿用, 没有才按本次比对新建
            found = earlier_findings(ctx.state, ctx.procedure.id, ("D01", "D04"), account.id, lambda x: x.source_ids)
            if not found and not difference and not unbooked:
                continue
            reply = documents.get(cast(str, confirmation.reply_document_id))
            if reply is None:
                raise LookupError(f"{confirmation.id} 的回函 {confirmation.reply_document_id} 未登记")
            if not found:
                if difference:
                    n = len(differences) + 1
                    differences.append(self.difference_exception(ctx, confirmation, account, reply, n))
                    found.append(differences[-1])
                if unbooked:
                    n = len(accrued) + 1
                    accrued.append(self.accrued_interest_exception(ctx, confirmation, account, reply, window, n))
                    found.append(accrued[-1])
            subjects.append(self.cash_subject(ctx, confirmation, account, reply, found, rules))
        judged = await self.judge_all(ctx, subjects)
        return CashFindings(updated, [*differences, *accrued], judged, rules.outcomes)

    @staticmethod
    def difference_exception(
        ctx: RunContext, confirmation: Confirmation, account: BankAccount, document: DocumentRef, n: int
    ) -> AuditException:
        """回函与账面不符的 D01 例外 (CSH-02.yaml 注释): 证据为回函余额所在页与试算表中的总账科目. 回函页的指针按
        回函当前的文件登记 (document, 见 registered_documents) 经 judgment_engine.document_pointer 改写种类与可靠性,
        不沿用 DOC-06 当时写进函证的值 (补登记后重跑即为新可靠性, 需求 R90); 页码不在登记页数内的照原样."""
        reply = [
            document_pointer(p, document)[0] or p
            for p in confirmation.evidence
            if p.locator == "confirmed_balance_cents"
        ]
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
    def accrued_interest_exception(
        ctx: RunContext,
        confirmation: Confirmation,
        account: BankAccount,
        document: DocumentRef,
        window: tuple[date, date],
        n: int,
    ) -> AuditException:
        """回函应计利息未入账的 D04 例外 (CSH-02.yaml 注释, 判定见 accrued_interest_unbooked): 金额为回函写明的应计利息
        (带符号: 银行欠客户为正), detail 记应计利息, 搜索窗口 (D04 的闭区间, 同 D03) 与匹配到的行 (建例外时没有匹配,
        恒为空). 证据为函证中应计利息所在页的指针, 按回函当前的文件登记改写种类与可靠性 (同 D01 的回函页)."""
        reply = [
            document_pointer(p, document)[0] or p
            for p in confirmation.evidence
            if p.locator == "accrued_interest_cents"
        ]
        return AuditException(
            id=f"X-CSH-02-{ctx.run.run_no}-D04-{n}",
            procedure_id=ctx.procedure.id,
            run_id=ctx.run.id,
            rule_id="D04",
            severity="medium",
            source_ids=[account.id],
            amount_cents=confirmation.accrued_interest_cents,
            detail={
                "accrued_interest_cents": confirmation.accrued_interest_cents,
                "search_from": window[0].isoformat(),
                "search_to": window[1].isoformat(),
                "matched_line_ids": [],
            },
            evidence=reply,
        )

    @staticmethod
    def cash_subject(
        ctx: RunContext,
        confirmation: Confirmation,
        account: BankAccount,
        reply: DocumentRef,
        found: Sequence[AuditException],
        rules: RuleResult,
    ) -> Subject:
        """有 D01 或 D04 的账户的判断对象, data 字段按 CSH-02.md.j2 头注释 (均为 JSON 形式).

        规则例外为前置程序 (depends_on, 即 CSH-05) 在该账户上尚未归因的例外 (当前结果, 按 ID 排序), 再加该账户上本程序
        的 D01, D04 (found, 依次: 沿用的 earlier_findings, 没有时为本次新建的). reply 为回函当前的文件登记
        (调用方按 registered_documents 取, 未登记抛 LookupError).
        """
        findings = [x for x in open_findings(ctx.state, ctx.template.depends_on) if account.id in x.source_ids]

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
        return Subject(account.id, data, [*findings, *found])

    def cash_patch(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """CSH-02 的解读: 函证新版本与 D01, D04 例外, 加上要判断的账户的判断片段 (judgments_patch).

        本次新建的 D01, D04 (findings.exceptions) 是 judgments_patch 的 created: 判断失败时第一条规则例外可能就是它们,
        置 unattributed 写在第 1 版上, 取代刚建的那一份 (需求 R322: 只给 v1, Store 里从 v1 起, 不再报 VersionConflict);
        顺序同 findings.exceptions. 沿用的不是 created, 归因或再置 unattributed 时照常出新版本 (需求 R347).
        """
        assert isinstance(findings, CashFindings)
        patch = judgments_patch(self.deps, ctx, findings.judged, findings.outcomes, created=findings.exceptions)
        patch["confirmations"] = findings.confirmations
        return patch

    def judgments_patch(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """判断程序的解读: 见 judging.judgments_patch; 取对象时没有执行的规则记进执行记录终态."""
        assert isinstance(findings, JudgedRun)
        return judgments_patch(self.deps, ctx, findings.judged, findings.outcomes)
