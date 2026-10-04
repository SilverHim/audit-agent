"""A3: DOC-04, DOC-06. 返回字段: documents, confirmations, review_tasks, procedure_runs; 合同经 save_contract 写入."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import ClassVar, cast

from backend.agents.base import ProcedureAgent, RunContext, finish_run, gather_all
from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import AgentId, Contract, ContractTerm, DocumentRef, DocumentType, TaskKind
from backend.contracts.services import ValidationContext
from backend.contracts.state import StatePatch


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


@dataclass(frozen=True)
class Extracted:
    """一份文件的抽取结果: 成功时 contract 非空 (已写入), 失败时为空, 交人处理."""

    document: DocumentRef  # 被抽取的文件
    subject_id: str  # 判断对象, DOC-04 为合同编号
    contract: Contract | None  # 抽出并已写入的合同, 失败时为空


Execute = Callable[[RunContext], Awaitable[list[Extracted]]]  # 执行一条程序, 返回各文件的抽取结果
Interpret = Callable[[RunContext, list[Extracted]], StatePatch]  # 把抽取结果转成状态片段


class IngestAgent(ProcedureAgent[list[Extracted]]):
    """A3: 执行 DOC-04, DOC-06, 从合同与回函 PDF 抽取要素. 按程序编号分派, DOC-06 尚未登记."""

    agent_id: ClassVar[AgentId] = AgentId.A3  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"DOC-04", "DOC-06"})  # 负责的程序

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读). 没有登记的程序抛 NotImplementedError."""
        handlers: dict[str, tuple[Execute, Interpret]] = {"DOC-04": (self.extract_contracts, self.documents_patch)}
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
        """DOC-04: 对每份合同 PDF (rerun 只处理 rerun_subject_ids 中的) 并发调大模型抽取, 成功的经 save_contract 写入.

        单份失败 (重试耗尽, 引用失败) 不影响其他合同. 合同按文件名排序, 结果顺序固定.
        某份抛异常 (如读不到文件) 时等其余合同都结束再上抛 (gather_all), 由 A1 记整次失败.
        """
        eid = ctx.engagement.id
        version = ctx.state["client_profile_version"]
        profile = await self.deps.engagement_store.client_profile(ctx.engagement.client_id, version)
        if profile is None:
            raise LookupError(f"client profile {ctx.engagement.client_id} v{version} not found")
        documents = sorted(
            await self.deps.financial_store.documents(eid, DocumentType.contract), key=lambda d: d.file_name
        )
        only = set(ctx.procedure.rerun_subject_ids)
        if only:
            documents = [d for d in documents if contract_id(d) in only]

        async def extract(document: DocumentRef) -> Extracted:
            cid = contract_id(document)
            pages = await self.deps.evidence_store.pages(eid, document.id)
            variables = {
                "client_name": profile.legal_name,
                "document": document.model_dump(mode="json"),
                "pages": pages,
            }
            result = await self.deps.judgments.call_llm(
                eid, ctx.template, ctx.run, variables, ValidationContext(document=document), self.agent_id, cid
            )
            if result.report.level_failed is not None:
                return Extracted(document, cid, None)
            contract = build_contract(cid, document, cast(ContractExtraction, result.report.output), ctx.run.id)
            await self.deps.financial_store.save_contract(eid, contract)
            return Extracted(document, cid, contract)

        return await gather_all(extract(d) for d in documents)

    def documents_patch(self, ctx: RunContext, findings: list[Extracted]) -> StatePatch:
        """成功的文件置 extracted; 失败的置 needs_manual, 并以本次执行为目标建带 subject_id 的 failed 任务.

        DOC-04 不允许 manual_result, 这类任务只能 rerun. 有失败时执行记录仍为成功, 节点按待决任务进入等待.
        """
        now = datetime.now(UTC)
        run = finish_run(ctx.run, now)
        documents = [
            f.document.model_copy(update={"status": "extracted" if f.contract is not None else "needs_manual"})
            for f in findings
        ]
        tasks = [
            self.deps.queue.make_task(TaskKind.failed, run, ctx.procedure.id, run.id, now, subject_id=f.subject_id)
            for f in findings
            if f.contract is None
        ]
        return {"documents": documents, "review_tasks": tasks, "procedure_runs": [run]}
