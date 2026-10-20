"""A3: DOC-04, DOC-06. 返回字段: documents, confirmations, review_tasks, procedure_runs; 合同经 save_contract 写入.

另有上传文件的处理 (需求 R14, DocumentIntake): 审计师上传后立即读首页文字, 按文件名与首页文字分类, 合同走 DOC-04
抽取, 回函走 DOC-06 抽取, ASIC 摘录走 DOC-REGISTRY 抽取 (需求 R290); 财务报表以账套为准 (Issue #46), 只分类不抽取.
不经编排, 不建执行记录与复核任务, 只改文件登记, 写合同与抽取版本.
"""

import asyncio
import io
import logging
import re
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, ClassVar, cast

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, finish_run, gather_all
from backend.agents.evidence import current_confirmations, legacy_accounts
from backend.contracts.data import REGISTRY_TARGET, DataInput, DocumentChange, ExtractionRecord
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import (
    ConfirmationExtraction,
    ContractExtraction,
    DocumentClassification,
    RegistryExtraction,
)
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    ClientProfile,
    Confirmation,
    Contract,
    ContractTerm,
    DocumentFailedStep,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    Engagement,
    Engine,
    Phase,
    ProcedureRun,
    Reliability,
    RunStatus,
    RunTrigger,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.contracts.services import ValidationContext
from backend.contracts.state import StatePatch
from backend.orchestration.trail import record_transitions
from backend.services.data_import import Sheet, cell_text, mask_sample, read_sheet

logger = logging.getLogger(__name__)


def contract_id(document: DocumentRef) -> str:
    """合同编号取 PDF 文件名主干: CON-001.pdf → CON-001 (接口文档第 0 节)."""
    return PurePath(document.file_name).stem


def build_contract(cid: str, document: DocumentRef, extraction: ContractExtraction, run_id: str) -> Contract:
    """由抽取结果组装合同. 条款按页码升序编号 {合同编号}-T{n}, 同页保持模型输出的顺序."""
    ordered = sorted(extraction.terms, key=lambda t: t.page)  # sorted 是稳定排序, 同页不改顺序
    return Contract(
        id=cid,
        document_id=document.id,
        counterparty=extraction.counterparty,
        counterparty_abn=extraction.counterparty_abn,
        amount_cents=extraction.amount_cents,
        sign_date=extraction.sign_date,
        terms=[
            ContractTerm(id=f"{cid}-T{n}", term_type=t.term_type, text=t.text, page=t.page)
            for n, t in enumerate(ordered, start=1)
        ],
        evidence=extraction.evidence,
        extracted_by_run=run_id,
    )


async def client_profile(deps: AgentDeps, engagement: Engagement, version: int) -> ClientProfile:
    """项目采用的那一版客户画像; 查不到抛 LookupError."""
    profile = await deps.engagement_store.client_profile(engagement.client_id, version)
    if profile is None:
        raise LookupError(f"client profile {engagement.client_id} v{version} not found")
    return profile


async def extract_contract(
    deps: AgentDeps,
    eid: str,
    template: ProcedureTemplate,
    run: ProcedureRun,
    document: DocumentRef,
    profile: ClientProfile,
) -> Contract | None:
    """调大模型从一份合同 PDF 抽取要素 (DOC-04 的提示词与校验), 组装成第 1 版合同, 不写入; 校验不过为 None.
    run 为这次抽取记在留痕上的执行: 图里为 DOC-04 的执行记录, 上传后直接抽取的为 upload_run."""
    cid = contract_id(document)
    pages = await deps.evidence_store.pages(eid, document.id)
    variables = {"client_name": profile.legal_name, "document": document.model_dump(mode="json"), "pages": pages}
    result = await deps.judgments.call_llm(
        eid, template, run, variables, ValidationContext(document=document, standards_of=None), AgentId.A3, cid
    )
    if result.report.level_failed is not None:
        return None
    return build_contract(cid, document, cast(ContractExtraction, result.report.output), run.id)


async def contract_writes(deps: AgentDeps, eid: str, contract: Contract) -> list[Contract]:
    """写入一次新抽取出的合同要依次写的各版 (只读, 不写), 最后一项为新合同定好版本号的那一版.

    版本: 没有同编号的合同为第 1 版; 已有时接在其后 (+1), 不覆盖审计师的修改与已有历史; 但已有的当前版本正是
    这一次执行抽取的 (同一 extracted_by_run, 历史最新一版记 A3, 即执行中断后重做) 时覆盖这一版, 不多出版本.
    已有合同没有版本记录 (迁移 0004 之前抽取的) 时先补记它 (排在前面), 再写新版本."""
    found = await deps.financial_store.contracts(eid, [contract.id])
    if not found:
        return [contract]
    current = found[0]
    history = await deps.financial_store.contract_history(eid, contract.id)
    backfill = [] if history else [current]  # 补记修改前的一版
    latest = history[-1] if history else None
    redo = (
        latest is not None
        and latest.version == current.version
        and latest.changed_by == AgentId.A3
        and current.extracted_by_run == contract.extracted_by_run
    )
    return [*backfill, contract.model_copy(update={"version": current.version if redo else current.version + 1})]


async def save_new_contract(deps: AgentDeps, eid: str, contract: Contract) -> Contract:
    """写入一次新抽取出的合同 (DOC-04 与上传后的 A3 共用), 返回写入的版本; 版本规则见 contract_writes. 智能导入的
    确认不经它, 由 DataStore.commit_batch 在写表的同一事务里写 contract_writes 的各版."""
    writes = await contract_writes(deps, eid, contract)
    for version in writes:
        await deps.financial_store.save_contract(eid, version)
    return writes[-1]


def contract_sources(documents: Iterable[DocumentRef], contracts: Mapping[str, Contract]) -> list[DocumentRef]:
    """DOC-04 要处理的合同原件, 每个合同编号一份, 按文件名排序: 已有合同时取它当前引用的那份 (同名文件重新上传后,
    旧原件已被新版取代, 不再抽取); 没有合同, 或它引用的文件已不算合同时, 取最近存档的一份."""
    groups: dict[str, list[DocumentRef]] = {}
    for document in documents:
        groups.setdefault(contract_id(document), []).append(document)
    chosen = []
    for cid, group in groups.items():
        current = contracts.get(cid)
        source = next((d for d in group if current is not None and d.id == current.document_id), None)
        chosen.append(source or max(group, key=lambda d: (d.uploaded_at, d.id)))
    return sorted(chosen, key=lambda d: d.file_name)


@dataclass(frozen=True)
class Extracted:
    """一份文件的抽取结果: 成功时 output 非空, 失败时为空, 交人处理."""

    document: DocumentRef  # 被抽取的文件
    subject_id: str  # 判断对象: DOC-04 为合同编号, DOC-06 为银行账户 ID
    output: Contract | Confirmation | None  # DOC-04 为已写入的合同, DOC-06 为函证新版本; 失败时为空


Execute = Callable[[RunContext], Awaitable[list[Extracted]]]  # 执行一条程序, 返回各文件的抽取结果
Interpret = Callable[[RunContext, list[Extracted]], StatePatch]  # 把抽取结果转成状态片段


def extracted_from(confirmation: Confirmation, document_id: str | None) -> bool:
    """这一版函证有没有 DOC-06 对该回函 (document_id) 成功抽取的结果: evidence 里有指向它的指针 (只有 DOC-06 抽取
    写 evidence; 审计师手改不写, 修改沿用已有的). 证据页据此区分 "回函没提应计利息" 与 "还没抽取" (需求 R104),
    kept_edit 据此认定修改前抽取过. document_id 为空 (还没回函) 为假."""
    return document_id is not None and any(p.source_id == document_id for p in confirmation.evidence)


class IngestAgent(ProcedureAgent[list[Extracted]]):
    """A3: 执行 DOC-04, DOC-06, 从合同与回函 PDF 抽取要素. 按程序编号分派."""

    agent_id: ClassVar[AgentId] = AgentId.A3  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"DOC-04", "DOC-06"})  # 负责的程序

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读). 没有登记的程序抛 NotImplementedError."""
        handlers: dict[str, tuple[Execute, Interpret]] = {
            "DOC-04": (self.extract_contracts, self.documents_patch),
            "DOC-06": (self.extract_replies, self.replies_patch),
        }
        if procedure_id not in handlers:
            raise NotImplementedError(procedure_id)
        return handlers[procedure_id]

    async def execute(self, ctx: RunContext) -> list[Extracted]:
        execute, _ = self.handler(ctx.procedure.id)
        return await execute(ctx)

    async def interpret(self, ctx: RunContext, findings: list[Extracted]) -> StatePatch:
        _, interpret = self.handler(ctx.procedure.id)
        return interpret(ctx, findings)

    async def extract_contracts(self, ctx: RunContext) -> list[Extracted]:
        """DOC-04: 对每份合同 (rerun 只处理 rerun_subject_ids 中的) 并发调大模型抽取, 成功的经 save_new_contract
        写入. 已有该文件抽出的合同 (上传后由 A3 抽取, 或之前某次执行已写入) 时不重抽, 也不覆盖审计师的修改, 直接用已有
        的合同, 文件登记照样交回 (并入状态, 置 extracted); rerun 点名的合同重抽, 版本接在已有版本之后.
        同一合同编号有多份原件 (同名文件重新上传了不同内容) 时只处理一份, 见 contract_sources.

        单份失败 (重试用完仍没通过) 不影响其他合同. 合同按文件名排序, 结果顺序固定.
        某份抛异常 (如读不到文件) 时等其余合同都结束再上抛 (gather_all), 由 A1 记整次失败.
        """
        eid = ctx.engagement.id
        profile = await client_profile(self.deps, ctx.engagement, ctx.state["client_profile_version"])
        existing = {c.id: c for c in await self.deps.financial_store.contracts(eid)}
        documents = contract_sources(await self.deps.financial_store.documents(eid, DocumentType.contract), existing)
        only = set(ctx.procedure.rerun_subject_ids)
        if only:
            documents = [d for d in documents if contract_id(d) in only]

        async def extract(document: DocumentRef) -> Extracted:
            current = existing.get(contract_id(document))
            if not only and current is not None and current.document_id == document.id:
                return Extracted(document, current.id, current)
            contract = await extract_contract(self.deps, eid, ctx.template, ctx.run, document, profile)
            if contract is None:
                return Extracted(document, contract_id(document), None)
            return Extracted(document, contract.id, await save_new_contract(self.deps, eid, contract))

        return await gather_all(extract(d) for d in documents)

    def documents_patch(self, ctx: RunContext, findings: list[Extracted]) -> StatePatch:
        """成功的文件置 extracted; 失败的置 needs_manual, 并以本次执行为目标建带 subject_id 的 failed 任务.

        DOC-04, DOC-06 不允许 manual_result, 这类任务只能 rerun. 有失败时执行记录仍为成功, 节点按待决任务进入等待.
        """
        now = datetime.now(UTC)
        run = finish_run(ctx.run, now)
        documents = [
            f.document.model_copy(update={"status": "extracted" if f.output is not None else "needs_manual"})
            for f in findings
        ]
        tasks = [
            self.deps.queue.make_task(TaskKind.failed, run, ctx.procedure.id, run.id, now, subject_id=f.subject_id)
            for f in findings
            if f.output is None
        ]
        return {"documents": documents, "review_tasks": tasks, "procedure_runs": [run]}

    async def extract_replies(self, ctx: RunContext) -> list[Extracted]:
        """DOC-06: 对状态中当前的 (账户仍在银行账户表中且识别信息没变, evidence.current_confirmations) 已有回函的函证
        (rerun 只处理 rerun_subject_ids 中的账户) 并发调大模型抽取回函余额与截至日.

        回函取状态 documents 中 reply_document_id 对应的记录, 不在状态中抛 LookupError. 校验上下文带上该函证,
        抽出的账号与账户不符即判失败; 回函渠道以 CSH-04 的回执为准, 不符只记提示. 通过的出函证新版本.
        审计师改过且回函原件没变的函证不重新抽取, 沿用 Store 中的版本 (见 kept_edit), 留痕写明沿用审计师修改.
        """
        eid = ctx.engagement.id
        only = set(ctx.procedure.rerun_subject_ids)
        accounts = await self.deps.financial_store.bank_accounts(eid)
        legacy = await legacy_accounts(self.deps, eid, accounts)
        confirmations = sorted(
            (
                c
                for c in current_confirmations(ctx.state["confirmations"], eid, accounts, legacy)
                if c.reply_document_id is not None and (not only or c.bank_account_id in only)
            ),
            key=lambda c: c.bank_account_id,
        )
        documents = {d.id: d for d in ctx.state["documents"]}

        async def extract(confirmation: Confirmation) -> Extracted:
            document = documents.get(cast(str, confirmation.reply_document_id))
            if document is None:
                raise LookupError(f"{confirmation.id} 的回函 {confirmation.reply_document_id} 不在状态中")
            kept = await self.kept_edit(ctx, confirmation)
            if kept is not None:
                return Extracted(document, confirmation.bank_account_id, kept)
            pages = await self.deps.evidence_store.pages(eid, document.id)
            variables = {"document": document.model_dump(mode="json"), "pages": pages}
            vctx = ValidationContext(document=document, confirmation=confirmation, standards_of=None)
            result = await self.deps.judgments.call_llm(
                eid, ctx.template, ctx.run, variables, vctx, self.agent_id, confirmation.bank_account_id
            )
            if result.report.level_failed is not None:
                return Extracted(document, confirmation.bank_account_id, None)
            extraction = cast(ConfirmationExtraction, result.report.output)
            updated = confirmation.model_copy(
                update={
                    "version": confirmation.version + 1,
                    "confirmed_balance_cents": extraction.confirmed_balance_cents,
                    "balance_date": extraction.balance_date,
                    "accrued_interest_cents": extraction.accrued_interest_cents,
                    "evidence": extraction.evidence,
                }
            )
            return Extracted(document, confirmation.bank_account_id, updated)

        return await gather_all(extract(c) for c in confirmations)

    async def kept_edit(self, ctx: RunContext, confirmation: Confirmation) -> Confirmation | None:
        """DOC-06 沿用的审计师修改 (以 Store 为准: 审计师改过的回函是人对机器结果的更正, 不被机器用同一份原件再抽一次
        覆盖). 三条都满足时返回 Store 中的最新版本: 抽取版本表有该函证的修改记录; 最近一次修改之前, 对同一原件有过
        一次成功抽取 (该版之前有 evidence 指向这份回函的版本, 只有 DOC-06 抽取写 evidence); 从这次修改的那版起
        Store 中各版的回函原件 (reply_document_id) 都与当前函证的相同 (没有换成新回函). 沿用时不调大模型, 不出新
        版本, 写一条 data 留痕 (action keep_edit, 防重复键 {eid}:{执行 ID}:keep_edit:{函证 ID}). 其余为空, 照常
        抽取: 没改过; 修改前从没成功抽取过 (如第一次抽取失败后审计师手改, R21 重跑时照常抽取); 修改之后换了原件
        (银行或客户给了新回函), 新版本取代审计师那一版."""
        eid = ctx.engagement.id
        edits = [
            r
            for r in await self.deps.data.extraction_versions(eid, "confirmation", confirmation.id)
            if r.field is not None
        ]
        if not edits:
            return None
        edited = edits[-1].version
        history = await self.deps.output_store.history(eid, Confirmation, confirmation.id)
        reply = confirmation.reply_document_id
        since = [c for c in history if c.version >= edited]
        extracted = any(c.version < edited and extracted_from(c, reply) for c in history)
        if not since or not extracted or any(c.reply_document_id != reply for c in since):
            return None
        kept = max(since, key=lambda c: c.version)
        entry = TrailEntry(
            engagement_id=eid,
            kind=TrailKind.data,
            actor=self.agent_id,
            procedure_id=ctx.procedure.id,
            run_id=ctx.run.id,
            summary=f"{kept.id} 沿用审计师修改 (第 {kept.version} 版), 回函原件未变, 不重新抽取",
            idempotency_key=f"{eid}:{ctx.run.id}:keep_edit:{kept.id}",
            payload={
                "action": "keep_edit",
                "confirmation_id": kept.id,
                "version": kept.version,
                "document_id": kept.reply_document_id,
                "edited_by": edits[-1].changed_by,
            },
            created_at=datetime.now(UTC),
        )
        await record_transitions(self.deps.trail, [entry])
        return kept

    def replies_patch(self, ctx: RunContext, findings: list[Extracted]) -> StatePatch:
        """DOC-06: 同 DOC-04 处理文件状态与失败任务, 另交回通过的函证新版本 (失败的函证不更新)."""
        patch = self.documents_patch(ctx, findings)
        patch["confirmations"] = [f.output for f in findings if isinstance(f.output, Confirmation)]
        return patch


# ── 上传文件的处理 (需求 R14) ──

# 分类用的模板: 不在程序库里 (不是审计程序, 不进程序包), 只借 call_llm 的渲染, 校验, 重试与留痕
CLASSIFY = ProcedureTemplate(
    id="DOC-CLASSIFY",
    name="文件分类",
    phase=Phase.P1_Planning,
    cycle="Data",
    owner=AgentId.A3,
    engine=Engine.llm,
    autonomy_level=AutonomyLevel.L2,
    llm=LLMSpec(prompt="DOC-CLASSIFY.md.j2", output_model="DocumentClassification", max_tokens=512),
)
# ASIC 摘录抽取的模板 (需求 R290): 同 CLASSIFY 不在程序库里; 结果存抽取版本表 (target_type REGISTRY_TARGET)
REGISTRY = ProcedureTemplate(
    id="DOC-REGISTRY",
    name="ASIC 摘录抽取",
    phase=Phase.P0_Acceptance,
    cycle="Data",
    owner=AgentId.A3,
    engine=Engine.llm,
    autonomy_level=AutonomyLevel.L2,
    llm=LLMSpec(prompt="DOC-REGISTRY.md.j2", output_model="RegistryExtraction", max_tokens=2048),
)
# 各类型文件的可靠性 (审计师上传的都是副本), 每种类型一行, 按出具方: 第三方或有对方一起签署的 (合同, 回函, 对账单,
# 登记摘录, 约定书, 协议) 为外部副本, 被审计单位自己的 (含来源不定的往来函件) 为内部副本; 之后可经补登记改
RELIABILITY: dict[DocumentType, Reliability] = {
    DocumentType.contract: Reliability.external_copy,
    DocumentType.invoice: Reliability.internal_copy,
    DocumentType.confirmation: Reliability.external_copy,
    DocumentType.bank_statement: Reliability.external_copy,
    DocumentType.minutes: Reliability.internal_copy,
    DocumentType.financial_statements: Reliability.internal_copy,
    DocumentType.registry_extract: Reliability.external_copy,
    DocumentType.engagement_letter: Reliability.external_copy,
    DocumentType.representation_letter: Reliability.internal_copy,
    DocumentType.correspondence: Reliability.internal_copy,
    DocumentType.governance_communication: Reliability.internal_copy,
    DocumentType.agreement: Reliability.external_copy,
    DocumentType.internal_record: Reliability.internal_copy,
    DocumentType.other: Reliability.internal_copy,
}
DETAILS = "document"  # 抽取版本表里文件补登记的 target_type (需求 R90)
DETAIL_FIELDS = ("reliability", "document_date", "related_ids")  # 补登记的三项; 记改了的字段 (field) 时按此顺序


def details_data(document: DocumentRef) -> dict[str, Any]:
    """抽取版本表里文件登记一版的内容: 类型, 可靠性, 文件日期 (YYYY-MM-DD 或空) 与关联对象."""
    return document.model_dump(mode="json", include={"type", *DETAIL_FIELDS})


def details_entry(eid: str, record: ExtractionRecord) -> TrailEntry:
    """文件登记一版 (补登记, 或补登记过的文件之后改了类型) 的 data 留痕: action edit, payload 带 document_type (该版
    登记的类型), document_id, field, reason; 防重复键 {eid}:data:edit:document:{文件 ID}:v{版本}, 写入与补写共用."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=record.changed_by,
        summary=f"文件 {record.target_id} 的登记改了 {record.field}, 第 {record.version} 版",
        idempotency_key=f"{eid}:data:edit:{DETAILS}:{record.target_id}:v{record.version}",
        payload={
            "action": "edit",
            "document_type": record.data["type"],
            "document_id": record.target_id,
            "field": record.field,
            "reason": record.reason,
        },
        created_at=datetime.now(UTC),
    )


async def record_registration(
    deps: AgentDeps, eid: str, before: DocumentRef, after: DocumentRef, actor: str, reason: str | None
) -> None:
    """补登记过的文件 (抽取版本表里已有它的 document 版本) 之后改了类型或可靠性 (审计师指定类型, 按补交请求定类型,
    A3 分类): 照 "改一律出新版本" 再记一版 (data 为改后的登记, field 为变了的 type, reliability, 理由取这次定类型的),
    写 data 留痕, 使 history 的最新一版与当前登记一致 (需求 R90). 没补登记过的不记 (history 以当前登记作唯一一版);
    都没变的不记. 不改数据版本 (改类型本来就不记数据变动; 合同的重抽另记 contracts)."""
    fields = [name for name in ("type", "reliability") if getattr(before, name) != getattr(after, name)]
    if not fields:
        return
    records = await deps.data.extraction_versions(eid, DETAILS, after.id)
    if not records:
        return
    record = ExtractionRecord(
        target_type=DETAILS,
        target_id=after.id,
        version=records[-1].version + 1,
        data=details_data(after),
        field=",".join(fields),
        reason=reason,
        changed_by=actor,
        changed_at=datetime.now(UTC),
    )
    await deps.data.save_extraction(
        eid, DETAILS, after.id, record.version, record.data, changed_by=actor, field=record.field, reason=reason
    )
    await record_transitions(deps.trail, [details_entry(eid, record)])


async def registered_reliability(deps: AgentDeps, eid: str, document: DocumentRef, kind: DocumentType) -> Reliability:
    """文件定为 kind 类型时登记的可靠性, 只此一处决定 (需求 R90): 审计师补登记过可靠性的 (抽取版本表里有
    target_type document, field 含 reliability 的版本) 保留登记上的值, 原件或副本与类型无关; 没补登记过的取该类型的
    缺省 RELIABILITY[kind]. 审计师指定类型, A3 分类, 按补交请求定类型与确认导入都经这里."""
    records = await deps.data.extraction_versions(eid, DETAILS, document.id)
    if any(r.field is not None and "reliability" in r.field.split(",") for r in records):
        return document.reliability
    return RELIABILITY[kind]


FIRST_PAGE_CHARS = 4000  # 分类时送给大模型的首页文字上限
WORD_XML = re.compile(r"<[^>]+>")


def upload_run(procedure_id: str, document: DocumentRef, job: str | None = None) -> ProcedureRun:
    """上传后直接处理时记在留痕上的执行 (不写入审计产出, 不占程序的执行序号). 分类的 ID 为 UPLOAD-{文件 ID};
    抽取另带作业号 UPLOAD-{文件 ID}-{作业号}, 作业号取定下这次类型的那条 classify 留痕的 ID: 同一作业中断后补做
    沿用同一作业号 (save_new_contract 据此覆盖而不多出版本), 审计师重新指定类型是新的作业 (版本接着往后排)."""
    return ProcedureRun(
        id=f"UPLOAD-{document.id}" + (f"-{job}" if job else ""),
        procedure_id=procedure_id,
        run_no=1,
        trigger=RunTrigger.initial,
        params_snapshot={},
        started_at=datetime.now(UTC),
        status=RunStatus.running,
    )


async def first_page_text(deps: AgentDeps, eid: str, document: DocumentRef) -> str:
    """分类用的首页文字: PDF 取文本层第 1 页; DOCX 取正文开头; XLSX, XLS, CSV 取第一个工作表的表头与前 20 行, 人名列与
    邮箱经 data_import.mask_sample 遮蔽 (同 DOC-SHEET 的样本行); 图片等读不出文字的为空串 (只按文件名分类).
    截到 FIRST_PAGE_CHARS. 读不了 (文件损坏等) 抛出."""
    if document.media_type == "application/pdf":
        if document.page_count == 0:  # 存档时就数不出页数 (文件损坏)
            raise ValueError(f"{document.file_name} 读不出页")
        pages = await deps.evidence_store.pages(eid, document.id)
        return (pages[0] if pages else "")[:FIRST_PAGE_CHARS]
    suffix = PurePath(document.file_name).suffix.lower()
    if suffix not in {".docx", ".xlsx", ".xlsm", ".xls", ".csv"}:
        return ""
    path = await deps.evidence_store.path(eid, document.id)
    data = await asyncio.to_thread(path.read_bytes)
    if suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        return " ".join(WORD_XML.sub(" ", xml).split())[:FIRST_PAGE_CHARS]  # 去掉标签, 空白并成一个空格
    sheet = await asyncio.to_thread(read_sheet, document.file_name, data)
    return sheet_first_page(sheet)


def sheet_first_page(sheet: Sheet) -> str:
    """表格的首页文字: 表头 (显示名) 与前 20 行, 逗号分隔, 经 mask_sample 遮蔽 (按原始表头判定人名列, 重名的列也认得),
    截到 FIRST_PAGE_CHARS."""
    texts = mask_sample([sheet.headers, *([cell_text(c) for c in cells] for _, cells in sheet.rows[:20])])
    return "\n".join(", ".join(row) for row in [sheet.columns, *texts[1:]])[:FIRST_PAGE_CHARS]


# 分类后还要抽取要素的类型. 财务报表以账套为准 (Issue #46), 只分类不抽取
EXTRACTED_TYPES = frozenset({DocumentType.contract, DocumentType.confirmation, DocumentType.registry_extract})


def unfinished(document: DocumentRef) -> bool:
    """审计师上传的文件 (渠道 auditor_upload 且有上传人), A3 处理是否没做完: 还没分类 (received), 或已分类为合同,
    回函, ASIC 摘录 (EXTRACTED_TYPES) 而没抽取完 (classified; 作业在两步之间被取消或进程退出).
    extracted, needs_manual 与其他的 classified 都算做完. 系统存档的文件 (数据集载入的合同, CSH-04 存档的银行直达回函
    等, 没有上传人) 由编排处理, 不在其中. 智能导入批次里暂存的文件 (staged) 等确认, 不在其中 (需求 R277)."""
    if document.channel != "auditor_upload" or document.uploaded_by is None:
        return False
    return document.status == "received" or (document.status == "classified" and document.type in EXTRACTED_TYPES)


@dataclass(frozen=True)
class Chosen:
    """审计师在智能导入的确认单上改定的文件类型 (需求 R277): 重新识别时按它, 不再分类."""

    document_type: DocumentType  # 改定的类型
    reason: str  # 理由
    actor: str  # 审计师的用户 ID


@dataclass(frozen=True)
class Requested:
    """上传时对应的待补交文件请求 (需求 R15)."""

    request_id: str  # 请求 ID, 如 REQ-contract
    document_type: DocumentType  # 请求的文件类型


# 智能导入里不按上传后流程处理的文件 (需求 R277): 暂存等确认的, 确认时没勾选而未导入的; 经 "上传文件" 重传时照新上传处理
HELD: tuple[DocumentStatus, ...] = ("staged", "not_imported")


def retype(document: DocumentRef, requested: Requested) -> bool:
    """对着待补交的文件请求上传 (或补做这样上传的) 的审计师上传文件是否要按请求定类型: 还没分类, 分不了类 (转了人工),
    或已分类的类型与请求的不同. 已按请求类型处理过的不再处理 (重复上传幂等). 系统存档的文件不改; 批次里暂存的
    (staged) 等确认, 确认时没勾选的 (not_imported) 是终态, 也不改 (同一文件经 "上传文件" 重传时由
    DataService.upload_documents 另行处理)."""
    if document.channel != "auditor_upload" or document.uploaded_by is None or document.status in HELD:
        return False
    return document.status in ("received", "needs_manual") or document.type is not requested.document_type


@dataclass(frozen=True)
class Staged:
    """智能导入批次里的一份文件 (需求 R277) 由 A3 分类与抽取的结果, 还没落到登记, 合同表与抽取版本表上; 确认时由
    DocumentIntake.staged_change 换算成要写的登记与抽取结果, DataStore.commit_batch 写入. 分类已写 classify 留痕
    (job 为它的 ID, 抽取的作业号)."""

    document_type: DocumentType | None  # 类型; 判断不了 (或读不出) 为空
    reason: str | None  # 分类理由; 按请求定类型的注明请求
    job: str | None  # classify 留痕 ID; 读不出时没有分类, 为空
    failed_step: DocumentFailedStep | None = None  # 卡住的步骤: read, classify, extract; 都通过为空
    contract: Contract | None = None  # 合同的抽取要素 (第 1 版, 未写入)
    reply: dict[str, Any] | None = None  # 回函副本的抽取结果 (ConfirmationExtraction 的 JSON, 未写入)
    registry: dict[str, Any] | None = None  # ASIC 摘录的抽取结果 (RegistryExtraction 的 JSON, 未写入, 需求 R290)


@dataclass(frozen=True)
class Job:
    """补做时要接上的作业: 定下文件当前类型的那条 classify 留痕."""

    id: str  # 留痕 ID, 即作业号 (见 upload_run)
    started_at: datetime  # 留痕时间, 即作业开始的时间


class DocumentIntake:
    """A3 处理一份上传的文件 (需求 R14): 读首页文字 → 分类 → 合同走 DOC-04, 回函走 DOC-06, ASIC 摘录走
    DOC-REGISTRY (需求 R290) 抽取.

    结果只体现在文件登记 (类型, 可靠性, 状态, 卡住的步骤), 合同表与抽取版本表上; 不经编排, 不建执行记录与复核任务.
    失败不抛出: 读不出为 needs_manual (read), 判断不了类型为 needs_manual (classify), 抽取校验不过为
    needs_manual (extract). 合同, 回函与 ASIC 摘录以外的类型 (含财务报表, Issue #46) 停在 classified.
    """

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps  # 外部依赖

    async def mark(self, eid: str, document: DocumentRef, requested: Requested) -> tuple[DocumentRef, Job]:
        """按上传时对应的待补交文件请求定类型 (需求 R15), 不抽取: 审计师是对着这项请求上传的, 类型就取请求的文件类型,
        不调大模型分类; 写 classify 留痕 (actor 为上传人, 理由注明请求), 登记改为该类型 (可靠性随类型), 置 classified.
        返回改后的登记与作业 (留痕 ID 为之后抽取的作业号). 上传时在请求里就做 (登记先落库, 作业开始前中断也不丢)."""
        note = f"按补交请求 {requested.request_id} 上传"
        actor = document.uploaded_by or self.agent
        job = await self.record(eid, actor, document, requested.document_type, note, requested.request_id)
        kind = requested.document_type
        reliability = await registered_reliability(self.deps, eid, document, kind)
        updated = await self.deps.evidence_store.update_document(
            eid, document.id, type=kind, reliability=reliability, status="classified", failed_step=None
        )
        await record_registration(self.deps, eid, document, updated, actor, note)
        return updated, Job(job, datetime.now(UTC))

    async def assign(
        self, eid: str, document: DocumentRef, engagement: Engagement, requested: Requested
    ) -> DocumentRef:
        """按请求定类型 (mark) 后按类型抽取."""
        updated, job = await self.mark(eid, document, requested)
        return await self.classified(eid, updated, requested.document_type, engagement, job.id)

    async def process(self, eid: str, document: DocumentRef, engagement: Engagement) -> DocumentRef:
        """从头处理一份文件 (读首页, 大模型分类, 按类型抽取), 返回处理后的登记."""
        decided = await self.classify(eid, document)
        if decided is None:
            return await self.manual(eid, document, "read")
        kind, reason = decided
        job = await self.record(eid, AgentId.A3, document, kind, reason)
        if kind is None:
            return await self.manual(eid, document, "classify")
        return await self.classified(eid, document, kind, engagement, job)

    async def classify(self, eid: str, document: DocumentRef) -> tuple[DocumentType | None, str | None] | None:
        """读首页文字, 按文件名与首页文字调大模型分类 (DOC-CLASSIFY), 返回 (类型, 理由); 判断不了 (含调用出错) 类型为
        空, 读不出为 None. 不写留痕, 不改登记."""
        try:
            text = await first_page_text(self.deps, eid, document)
        except Exception:
            logger.exception("读不出 %s %s", eid, document.id)
            return None
        variables = {"file_name": document.file_name, "media_type": document.media_type, "first_page": text}
        stem = PurePath(document.file_name).stem
        try:
            result = await self.deps.judgments.call_llm(
                eid,
                CLASSIFY,
                upload_run(CLASSIFY.id, document),
                variables,
                ValidationContext(standards_of=None),
                self.agent,
                stem,
            )
            output = cast(DocumentClassification | None, result.report.output)
            kind = output.document_type if result.report.level_failed is None and output is not None else None
            reason = output.reason if output is not None else None
        except Exception:
            logger.exception("分类出错 %s %s", eid, document.id)
            kind, reason = None, None
        return kind, reason

    async def stage(
        self,
        eid: str,
        document: DocumentRef,
        engagement: Engagement,
        requested: Requested | None = None,
        chosen: Chosen | None = None,
    ) -> Staged:
        """智能导入批次里的一份文件 (需求 R277): 分类与抽取, 结果不落到登记与合同上, 等确认 (staged_change).

        审计师在确认单上改定了类型 (chosen) 时取它 (不调大模型, 留痕记审计师与理由); 对应了待补交的文件请求时取请求
        的 (理由注明请求); 否则调 DOC-CLASSIFY. 三者都写 classify 留痕 (同上传后的处理, 留痕 ID 为抽取的作业号, 改定
        类型的是新的作业). 合同按 DOC-04 抽出合同, 回函按 DOC-06 抽出余额与截至日, ASIC 摘录按 DOC-REGISTRY 抽取,
        都不写入; 抽取没通过的记 failed_step extract. 读不出, 判断不了类型的记 read, classify."""
        if chosen is not None:
            kind: DocumentType | None = chosen.document_type
            reason: str | None = chosen.reason
            job = await self.record(eid, chosen.actor, document, kind, reason)
        elif requested is not None:
            kind = requested.document_type
            reason = f"按补交请求 {requested.request_id} 上传"
            actor = document.uploaded_by or self.agent
            job = await self.record(eid, actor, document, kind, reason, requested.request_id)
        else:
            decided = await self.classify(eid, document)
            if decided is None:
                return Staged(None, None, None, "read")
            kind, reason = decided
            job = await self.record(eid, AgentId.A3, document, kind, reason)
        if kind is None:
            return Staged(None, reason, job, "classify")
        contract: Contract | None = None
        reply: dict[str, Any] | None = None
        registry: dict[str, Any] | None = None
        # 抽取按定下的类型看这份文件 (同上传后的处理先改登记再抽取; 这里登记不改, 只给抽取与校验用的副本)
        reliability = await registered_reliability(self.deps, eid, document, kind)
        typed = document.model_copy(update={"type": kind, "reliability": reliability, "status": "classified"})
        try:
            if kind is DocumentType.contract:
                profile = await client_profile(self.deps, engagement, engagement.client_profile_version)
                run = upload_run("DOC-04", typed, job)
                contract = await extract_contract(self.deps, eid, self.deps.library.load("DOC-04"), run, typed, profile)
            elif kind is DocumentType.confirmation:
                extraction = await self.extract_reply(eid, typed)
                reply = extraction.model_dump(mode="json") if extraction is not None else None
            elif kind is DocumentType.registry_extract:
                found = await self.extract_registry(eid, typed)
                registry = found.model_dump(mode="json") if found is not None else None
        except Exception:
            logger.exception("抽取出错 %s %s", eid, document.id)
        failed = kind in EXTRACTED_TYPES and all(r is None for r in (contract, reply, registry))
        return Staged(kind, reason, job, "extract" if failed else None, contract, reply, registry)

    async def staged_change(self, eid: str, document: DocumentRef, staged: Staged) -> DocumentChange:
        """确认单上的一份文件 (需求 R277) 确认时要写的, 由 DataStore.commit_batch 在写表的同一事务里写入; 只读, 不调
        大模型. 类型为 staged 的 (审计师改了类型时已按新类型重新识别与抽取): 合同 (版本规则见 contract_writes), 回函
        抽取结果 (reply, 第 1 版) 或 ASIC 摘录抽取结果 (registry_extract, 接在已有版本之后) 置 extracted, 抽取没通过的
        置 needs_manual (extract); 其余类型置 classified."""
        kind = staged.document_type
        if kind is None:
            raise ValueError(f"{document.id} 没有类型, 不能确认")
        reliability = await registered_reliability(self.deps, eid, document, kind)
        change = DocumentChange(document_id=document.id, type=kind, reliability=reliability, status="classified")
        if kind not in EXTRACTED_TYPES:
            return change
        if all(r is None for r in (staged.contract, staged.reply, staged.registry)):
            return change.model_copy(update={"status": "needs_manual", "failed_step": "extract"})
        contracts = await contract_writes(self.deps, eid, staged.contract) if staged.contract is not None else []
        update = {"contracts": contracts, "reply": staged.reply, "registry_extract": staged.registry}
        return change.model_copy(update={"status": "extracted", **update})

    async def resume(
        self,
        eid: str,
        document: DocumentRef,
        engagement: Engagement,
        job: Job | None,
        requested: Requested | None = None,
    ) -> DocumentRef:
        """处理或补做一份文件: 对应了待补交的文件请求且需要按它定类型的 (见 retype) 按请求定类型 (assign); 否则
        received 的从头处理; classified 的按已定类型接着抽取, job 为定下这次类型的 classify 留痕 (找不到为空, 按新的
        作业抽取). 合同的补做规则见 contract."""
        if requested is not None and (document.status in HELD or retype(document, requested)):
            return await self.assign(eid, document, engagement, requested)
        # 暂存或未导入的文件经 "上传文件" 重传: 照新上传处理
        if document.status == "received" or document.status in HELD:
            return await self.process(eid, document, engagement)
        if job is None:
            return await self.classified(eid, document, document.type, engagement)
        return await self.classified(eid, document, document.type, engagement, job.id, since=job.started_at)

    async def classified(
        self,
        eid: str,
        document: DocumentRef,
        kind: DocumentType,
        engagement: Engagement,
        job: str | None = None,
        *,
        since: datetime | None = None,
    ) -> DocumentRef:
        """类型已定 (A3 分类或审计师指定): 改登记的类型与可靠性, 置 classified, 再按类型抽取. job 为定下这次类型的
        classify 留痕 ID, 用作抽取的作业号 (见 upload_run); since 只在补做时给, 为该作业开始的时间."""
        reliability = await registered_reliability(self.deps, eid, document, kind)
        before = document
        document = await self.deps.evidence_store.update_document(
            eid, document.id, type=kind, reliability=reliability, status="classified", failed_step=None
        )
        await record_registration(self.deps, eid, before, document, AgentId.A3, None)
        try:
            if kind is DocumentType.contract:
                done = await self.contract(eid, document, engagement, job, since)
            elif kind is DocumentType.confirmation:
                done = await self.reply(eid, document)
            elif kind is DocumentType.registry_extract:
                done = await self.registry(eid, document, since)
            else:
                return document
        except Exception:
            logger.exception("抽取出错 %s %s", eid, document.id)
            done = False
        if not done:
            return await self.manual(eid, document, "extract")
        return await self.deps.evidence_store.update_document(
            eid, document.id, type=kind, reliability=reliability, status="extracted", failed_step=None
        )

    async def contract(
        self, eid: str, document: DocumentRef, engagement: Engagement, job: str | None, since: datetime | None
    ) -> bool:
        """DOC-04 抽取并写入合同, 版本规则见 save_new_contract: 重新上传或改类型后重抽 (新的作业) 接在已有版本之后;
        同一事务内数据版本 +1, 记数据变动 contracts.

        补做 (since 为作业开始的时间) 时先看已有的同编号合同: 正是这个作业写入的 (同 extracted_by_run; 中断在置
        extracted 之前, 之后可能还经审计师修改) 不重抽, 算做完; 作业开始之后被改动过 (之后的作业或审计师修改) 的也不
        重抽, 不拿旧作业覆盖: 引用的正是这份文件的算做完, 引用别的原件 (同名文件之后重新上传了) 的这份没抽成,
        转人工 (extract), 审计师可重新指定类型再抽. 其余照常抽取."""
        run = upload_run("DOC-04", document, job)
        if since is not None:
            found = await self.deps.financial_store.contracts(eid, [contract_id(document)])
            if found and found[0].extracted_by_run == run.id:
                return True
            if found:
                history = await self.deps.financial_store.contract_history(eid, found[0].id)
                if history and history[-1].changed_at > since:
                    return found[0].document_id == document.id
        template = self.deps.library.load("DOC-04")
        profile = await client_profile(self.deps, engagement, engagement.client_profile_version)
        contract = await extract_contract(self.deps, eid, template, run, document, profile)
        if contract is None:
            return False
        # 程序执行之外写进的合同新版本, 同审计师修改合同: 一个事务内记数据变动 contracts, 读合同的程序 (REV-03,
        # REV-02) 据此标过期 (需求 R274). DOC-04 执行写的合同不记, 靠 "依赖的程序在其后重跑过" 传播
        async with self.deps.output_store.transaction(eid):
            await save_new_contract(self.deps, eid, contract)
            await self.deps.data.bump_version(eid, DataInput.contracts)
        return True

    async def reply(self, eid: str, document: DocumentRef) -> bool:
        """DOC-06 抽取回函副本的余额与截至日, 记在抽取版本表 (reply, 文件 ID, 第 1 版; 重抽覆盖, 只留最近一次).

        不关联到函证 (函证由 CSH-04 发出, 回函经函证平台直达, 在编排里抽取); 这里只留副本的抽取结果供查看, 不能修改,
        所以不留历史."""
        extraction = await self.extract_reply(eid, document)
        if extraction is None:
            return False
        data = extraction.model_dump(mode="json")
        await self.deps.data.save_extraction(eid, "reply", document.id, 1, data, changed_by=AgentId.A3)
        return True

    async def extract_reply(self, eid: str, document: DocumentRef) -> ConfirmationExtraction | None:
        """DOC-06 抽取回函副本 (不写入); 校验不过为 None."""
        template = self.deps.library.load("DOC-06")
        pages = await self.deps.evidence_store.pages(eid, document.id)
        variables = {"document": document.model_dump(mode="json"), "pages": pages}
        result = await self.deps.judgments.call_llm(
            eid,
            template,
            upload_run("DOC-06", document),
            variables,
            ValidationContext(document=document, standards_of=None),
            self.agent,
            PurePath(document.file_name).stem,
        )
        if result.report.level_failed is not None or result.report.output is None:
            return None
        return cast(ConfirmationExtraction, result.report.output)

    async def registry(self, eid: str, document: DocumentRef, since: datetime | None) -> bool:
        """DOC-REGISTRY 抽取 ASIC 摘录 (需求 R290), 通过时在抽取版本表记新一版 (REGISTRY_TARGET, 文件 ID, 接在已有
        版本之后: 重新上传或改类型后重抽, A3 写), 同一事务内数据版本 +1, 记数据变动 registry_extracts (ACC-04 的
        reads; 承接阶段的执行不记数据版本, 按 R274 不判过期). 补做 (since 为作业开始的时间) 时已有作业开始之后的
        版本 (正是这个作业写的, 或之后审计师修改的) 不重抽, 算做完, 不拿旧作业覆盖."""
        records = await self.deps.data.extraction_versions(eid, REGISTRY_TARGET, document.id)
        if since is not None and records and records[-1].changed_at > since:
            return True
        extraction = await self.extract_registry(eid, document)
        if extraction is None:
            return False
        version = records[-1].version + 1 if records else 1
        async with self.deps.output_store.transaction(eid):
            await self.deps.data.save_extraction(
                eid, REGISTRY_TARGET, document.id, version, extraction.model_dump(mode="json"), changed_by=AgentId.A3
            )
            await self.deps.data.bump_version(eid, DataInput.registry_extracts)
        return True

    async def extract_registry(self, eid: str, document: DocumentRef) -> RegistryExtraction | None:
        """DOC-REGISTRY 抽取 ASIC 摘录 (不写入); 校验不过为 None."""
        pages = await self.deps.evidence_store.pages(eid, document.id)
        variables = {"document": document.model_dump(mode="json"), "pages": pages}
        result = await self.deps.judgments.call_llm(
            eid,
            REGISTRY,
            upload_run(REGISTRY.id, document),
            variables,
            ValidationContext(document=document, standards_of=None),
            self.agent,
            PurePath(document.file_name).stem,
        )
        if result.report.level_failed is not None or result.report.output is None:
            return None
        return cast(RegistryExtraction, result.report.output)

    async def manual(self, eid: str, document: DocumentRef, step: DocumentFailedStep) -> DocumentRef:
        """置 needs_manual, 记卡住的步骤; 类型与可靠性不变."""
        return await self.deps.evidence_store.update_document(
            eid,
            document.id,
            type=document.type,
            reliability=document.reliability,
            status="needs_manual",
            failed_step=step,
        )

    async def record(
        self,
        eid: str,
        actor: str,
        document: DocumentRef,
        kind: DocumentType | None,
        reason: str | None,
        request_id: str | None = None,
    ) -> str:
        """分类的 data 留痕: action classify, payload 带 document_id, document_type (判断不了为空) 与理由; 按补交请求
        定类型的另带 request_id (登记没改成时, 补做据此接着按请求定类型). 返回留痕 ID, 用作之后抽取的作业号."""
        label = kind.value if kind is not None else "无法判断"
        payload: dict[str, object] = {
            "action": "classify",
            "document_id": document.id,
            "document_type": kind.value if kind is not None else None,
            "reason": reason,
        }
        if request_id is not None:
            payload["request_id"] = request_id
        return await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.data,
                actor=actor,
                summary=f"{document.file_name} 分类为 {label}",
                payload=payload,
                created_at=datetime.now(UTC),
            )
        )

    agent: ClassVar[AgentId] = AgentId.A3  # 记在大模型留痕上的调用方
