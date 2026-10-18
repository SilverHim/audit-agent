"""A3: DOC-04, DOC-06. 返回字段: documents, confirmations, review_tasks, procedure_runs; 合同经 save_contract 写入.

另有上传文件的处理 (需求 R14, DocumentIntake): 审计师上传后立即读首页文字, 按文件名与首页文字分类, 合同走 DOC-04
抽取, 回函走 DOC-06 抽取; 不经编排, 不建执行记录与复核任务, 只改文件登记与写合同.
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
from typing import ClassVar, cast

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, finish_run, gather_all
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import ConfirmationExtraction, ContractExtraction, DocumentClassification
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    ClientProfile,
    Confirmation,
    Contract,
    ContractTerm,
    DocumentFailedStep,
    DocumentRef,
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
from backend.services.data_import import cell_text, read_sheet

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
        eid, template, run, variables, ValidationContext(document=document), AgentId.A3, cid
    )
    if result.report.level_failed is not None:
        return None
    return build_contract(cid, document, cast(ContractExtraction, result.report.output), run.id)


async def save_new_contract(deps: AgentDeps, eid: str, contract: Contract) -> Contract:
    """写入一次新抽取出的合同 (DOC-04 与上传后的 A3 共用), 返回写入的版本.

    版本: 没有同编号的合同为第 1 版; 已有时接在其后 (+1), 不覆盖审计师的修改与已有历史; 但已有的当前版本正是
    这一次执行抽取的 (同一 extracted_by_run, 历史最新一版记 A3, 即执行中断后重做) 时覆盖这一版, 不多出版本.
    已有合同没有版本记录 (迁移 0004 之前抽取的) 时先补记它, 再写新版本."""
    found = await deps.financial_store.contracts(eid, [contract.id])
    if not found:
        await deps.financial_store.save_contract(eid, contract)
        return contract
    current = found[0]
    history = await deps.financial_store.contract_history(eid, contract.id)
    if not history:
        await deps.financial_store.save_contract(eid, current)  # 补记修改前的一版
    latest = history[-1] if history else None
    redo = (
        latest is not None
        and latest.version == current.version
        and latest.changed_by == AgentId.A3
        and current.extracted_by_run == contract.extracted_by_run
    )
    contract = contract.model_copy(update={"version": current.version if redo else current.version + 1})
    await deps.financial_store.save_contract(eid, contract)
    return contract


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

        单份失败 (重试耗尽, 引用失败) 不影响其他合同. 合同按文件名排序, 结果顺序固定.
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
        """DOC-06: 对状态中已有回函的函证 (rerun 只处理 rerun_subject_ids 中的账户) 并发调大模型抽取回函余额与截至日.

        回函取状态 documents 中 reply_document_id 对应的记录, 不在状态中抛 LookupError. 校验上下文带上该函证,
        抽出的账号与账户不符即判失败; 回函渠道以 CSH-04 的回执为准, 不符只记提示. 通过的出函证新版本.
        """
        eid = ctx.engagement.id
        only = set(ctx.procedure.rerun_subject_ids)
        confirmations = sorted(
            (
                c
                for c in ctx.state["confirmations"]
                if c.reply_document_id is not None and (not only or c.bank_account_id in only)
            ),
            key=lambda c: c.bank_account_id,
        )
        documents = {d.id: d for d in ctx.state["documents"]}

        async def extract(confirmation: Confirmation) -> Extracted:
            document = documents.get(cast(str, confirmation.reply_document_id))
            if document is None:
                raise LookupError(f"{confirmation.id} 的回函 {confirmation.reply_document_id} 不在状态中")
            pages = await self.deps.evidence_store.pages(eid, document.id)
            variables = {"document": document.model_dump(mode="json"), "pages": pages}
            vctx = ValidationContext(document=document, confirmation=confirmation)
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
                    "evidence": extraction.evidence,
                }
            )
            return Extracted(document, confirmation.bank_account_id, updated)

        return await gather_all(extract(c) for c in confirmations)

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
    """分类用的首页文字: PDF 取文本层第 1 页; DOCX 取正文开头; XLSX, XLS, CSV 取第一个工作表的表头与前 20 行;
    图片等读不出文字的为空串 (只按文件名分类). 截到 FIRST_PAGE_CHARS. 读不了 (文件损坏等) 抛出."""
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
    lines = [", ".join(sheet.columns)] + [", ".join(cell_text(c) for c in cells) for _, cells in sheet.rows[:20]]
    return "\n".join(lines)[:FIRST_PAGE_CHARS]


EXTRACTED_TYPES = frozenset({DocumentType.contract, DocumentType.confirmation})  # 分类后还要抽取要素的类型


def unfinished(document: DocumentRef) -> bool:
    """审计师上传的文件 (渠道 auditor_upload 且有上传人), A3 处理是否没做完: 还没分类 (received), 或已分类为合同,
    回函而没抽取完 (classified; 作业在两步之间被取消或进程退出). extracted, needs_manual 与其他类型的 classified
    都算做完. 系统存档的文件 (数据集载入的合同, CSH-04 存档的银行直达回函等, 没有上传人) 由编排处理, 不在其中."""
    if document.channel != "auditor_upload" or document.uploaded_by is None:
        return False
    return document.status == "received" or (document.status == "classified" and document.type in EXTRACTED_TYPES)


@dataclass(frozen=True)
class Requested:
    """上传时对应的待补交文件请求 (需求 R15)."""

    request_id: str  # 请求 ID, 如 REQ-financial_statements
    document_type: DocumentType  # 请求的文件类型


def retype(document: DocumentRef, requested: Requested) -> bool:
    """对着待补交的文件请求上传 (或补做这样上传的) 的审计师上传文件是否要按请求定类型: 还没分类, 分不了类 (转了人工),
    或已分类的类型与请求的不同. 已按请求类型处理过的不再处理 (重复上传幂等). 系统存档的文件不改."""
    if document.channel != "auditor_upload" or document.uploaded_by is None:
        return False
    return document.status in ("received", "needs_manual") or document.type is not requested.document_type


@dataclass(frozen=True)
class Job:
    """补做时要接上的作业: 定下文件当前类型的那条 classify 留痕."""

    id: str  # 留痕 ID, 即作业号 (见 upload_run)
    started_at: datetime  # 留痕时间, 即作业开始的时间


class DocumentIntake:
    """A3 处理一份上传的文件 (需求 R14): 读首页文字 → 分类 → 合同走 DOC-04, 回函走 DOC-06 抽取.

    结果只体现在文件登记 (类型, 可靠性, 状态, 卡住的步骤), 合同表与抽取版本表上; 不经编排, 不建执行记录与复核任务.
    失败不抛出: 读不出为 needs_manual (read), 判断不了类型为 needs_manual (classify), 抽取校验不过为
    needs_manual (extract). 合同与回函以外的类型停在 classified.
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
        updated = await self.deps.evidence_store.update_document(
            eid, document.id, type=kind, reliability=RELIABILITY[kind], status="classified", failed_step=None
        )
        return updated, Job(job, datetime.now(UTC))

    async def assign(
        self, eid: str, document: DocumentRef, engagement: Engagement, requested: Requested
    ) -> DocumentRef:
        """按请求定类型 (mark) 后按类型抽取."""
        updated, job = await self.mark(eid, document, requested)
        return await self.classified(eid, updated, requested.document_type, engagement, job.id)

    async def process(self, eid: str, document: DocumentRef, engagement: Engagement) -> DocumentRef:
        """从头处理一份文件 (读首页, 大模型分类, 按类型抽取), 返回处理后的登记."""
        try:
            text = await first_page_text(self.deps, eid, document)
        except Exception:
            logger.exception("读不出 %s %s", eid, document.id)
            return await self.manual(eid, document, "read")
        variables = {"file_name": document.file_name, "media_type": document.media_type, "first_page": text}
        stem = PurePath(document.file_name).stem
        try:
            result = await self.deps.judgments.call_llm(
                eid, CLASSIFY, upload_run(CLASSIFY.id, document), variables, ValidationContext(), self.agent, stem
            )
            output = cast(DocumentClassification | None, result.report.output)
            kind = output.document_type if result.report.level_failed is None and output is not None else None
            reason = output.reason if output is not None else None
        except Exception:
            logger.exception("分类出错 %s %s", eid, document.id)
            kind, reason = None, None
        job = await self.record(eid, AgentId.A3, document, kind, reason)
        if kind is None:
            return await self.manual(eid, document, "classify")
        return await self.classified(eid, document, kind, engagement, job)

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
        if requested is not None and retype(document, requested):
            return await self.assign(eid, document, engagement, requested)
        if document.status == "received":
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
        reliability = RELIABILITY[kind]
        document = await self.deps.evidence_store.update_document(
            eid, document.id, type=kind, reliability=reliability, status="classified", failed_step=None
        )
        try:
            if kind is DocumentType.contract:
                done = await self.contract(eid, document, engagement, job, since)
            elif kind is DocumentType.confirmation:
                done = await self.reply(eid, document)
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
        """DOC-04 抽取并写入合同, 版本规则见 save_new_contract: 重新上传或改类型后重抽 (新的作业) 接在已有版本之后.

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
        await save_new_contract(self.deps, eid, contract)
        return True

    async def reply(self, eid: str, document: DocumentRef) -> bool:
        """DOC-06 抽取回函副本的余额与截至日, 记在抽取版本表 (reply, 文件 ID, 第 1 版; 重抽覆盖, 只留最近一次).

        不关联到函证 (函证由 CSH-04 发出, 回函经函证平台直达, 在编排里抽取); 这里只留副本的抽取结果供查看, 不能修改,
        所以不留历史."""
        template = self.deps.library.load("DOC-06")
        pages = await self.deps.evidence_store.pages(eid, document.id)
        variables = {"document": document.model_dump(mode="json"), "pages": pages}
        result = await self.deps.judgments.call_llm(
            eid,
            template,
            upload_run("DOC-06", document),
            variables,
            ValidationContext(document=document),
            self.agent,
            PurePath(document.file_name).stem,
        )
        if result.report.level_failed is not None or result.report.output is None:
            return False
        data = cast(ConfirmationExtraction, result.report.output).model_dump(mode="json")
        await self.deps.data.save_extraction(eid, "reply", document.id, 1, data, changed_by=AgentId.A3)
        return True

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
