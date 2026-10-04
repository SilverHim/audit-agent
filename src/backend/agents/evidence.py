"""A5: CSH-04. 返回字段: confirmations, documents, procedure_runs."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.models import (
    AgentId,
    Confirmation,
    DocumentRef,
    DocumentType,
    ReceiptChannel,
    Reliability,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import ConfirmationReceipt, ConfirmationRequest
from backend.contracts.records import BankAccount
from backend.contracts.state import StatePatch

# 回函直达审计师 (银行直接寄或发邮件给审计师) 为外部原件, 其余 (经客户转交, 口头) 为外部副本 (接口文档第 6 节)
DIRECT_CHANNELS = {ReceiptChannel.direct_mail, ReceiptChannel.direct_email}


def reply_reliability(channel: ReceiptChannel | None) -> Reliability:
    """回函原件的可靠性, 按回执渠道定."""
    return Reliability.external_original if channel in DIRECT_CHANNELS else Reliability.external_copy


@dataclass(frozen=True)
class Confirmed:
    """一个银行账户的函证结果: 函证与存档的回函."""

    confirmation: Confirmation  # 函证第 1 版, 结果待 DOC-06, CSH-02 补
    document: DocumentRef  # 存档的回函


class EvidenceAgent(ProcedureAgent[list[Confirmed]]):
    """A5: 执行 CSH-04, 向银行发询证函并存档回函.

    发函是外部动作, 按接口文档第 6 节三步保证不重复: 先按幂等键查成功留痕, 有则按留痕重建; 没有才发函,
    存档回函, 再写带幂等键的 external_call 留痕.
    """

    agent_id: ClassVar[AgentId] = AgentId.A5  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"CSH-04"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> list[Confirmed]:
        """对每个银行账户 (按账户 ID 排序) 依次函证."""
        accounts = sorted(await self.deps.financial_store.bank_accounts(ctx.engagement.id), key=lambda a: a.id)
        return [await self.confirm(ctx, account) for account in accounts]

    async def confirm(self, ctx: RunContext, account: BankAccount) -> Confirmed:
        """一个账户的函证: 已有成功留痕时按留痕重建, 不再发函与存档; 否则发函, 存档回函, 写留痕."""
        eid = ctx.engagement.id
        key = f"{eid}:CSH-04:{account.id}"
        done = await self.deps.trail.find_success(eid, key)
        if done is not None:
            return await self.rebuild(ctx, account, key, done.payload)
        request = ConfirmationRequest(
            bank_account_id=account.id,
            bank_name=account.bank_name,
            bsb=account.bsb,
            account_no=account.account_no,
            as_of=ctx.run.params_snapshot["as_of"],  # 余额截至日取参数快照, 即期末
        )
        receipt = await self.deps.confirmations.submit(eid, request, key)
        document = await self.archive(eid, account, receipt)
        confirmation = Confirmation(
            id=f"C-{account.id}",
            bank_account_id=account.id,
            confirming_party=account.bank_name,
            idempotency_key=key,
            sent_at=receipt.sent_at,
            reply_document_id=document.id,
            receipt_channel=receipt.receipt_channel,
            replied_at=receipt.replied_at,
        )
        await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.external_call,
                actor=self.agent_id,
                procedure_id=ctx.procedure.id,
                run_id=ctx.run.id,
                summary=f"Bank confirmation {account.id} sent to {account.bank_name}",
                idempotency_key=key,
                payload={
                    "bank_account_id": account.id,
                    "sent_at": receipt.sent_at.isoformat(),
                    "replied_at": receipt.replied_at.isoformat() if receipt.replied_at else None,
                    "receipt_channel": receipt.receipt_channel.value if receipt.receipt_channel else None,
                    "reply_document_id": document.id,
                },
            )
        )
        return Confirmed(confirmation, document)

    async def archive(self, eid: str, account: BankAccount, receipt: ConfirmationReceipt) -> DocumentRef:
        """存档回函原件 (type confirmation, 可靠性按回执渠道).

        回执没有回函 (无文件名, 或内容为空) 时抛 ValueError: 本版只处理已回函的情况.
        """
        if not receipt.reply_bytes or not receipt.reply_file_name:
            raise ValueError(f"{account.id} 的函证回执没有回函, 无法存档")
        return await self.deps.evidence_store.save(
            eid,
            receipt.reply_file_name,
            receipt.reply_bytes,
            type=DocumentType.confirmation,
            reliability=reply_reliability(receipt.receipt_channel),
        )

    async def rebuild(self, ctx: RunContext, account: BankAccount, key: str, payload: dict[str, object]) -> Confirmed:
        """按成功留痕重建函证: 发函时间等取留痕 payload, 回函取已登记的文件 (FinancialDataStore.documents)."""
        eid = ctx.engagement.id
        documents = {d.id: d for d in await self.deps.financial_store.documents(eid, DocumentType.confirmation)}
        document_id = str(payload["reply_document_id"])
        document = documents.get(document_id)
        if document is None:
            raise LookupError(f"{key} 的留痕记录回函 {document_id}, 但文件未登记")
        replied_at = payload.get("replied_at")
        channel = payload.get("receipt_channel")
        confirmation = Confirmation(
            id=f"C-{account.id}",
            bank_account_id=account.id,
            confirming_party=account.bank_name,
            idempotency_key=key,
            sent_at=datetime.fromisoformat(str(payload["sent_at"])),
            reply_document_id=document.id,
            receipt_channel=ReceiptChannel(str(channel)) if channel is not None else None,
            replied_at=date.fromisoformat(str(replied_at)) if replied_at is not None else None,
        )
        return Confirmed(confirmation, document)

    async def interpret(self, ctx: RunContext, findings: list[Confirmed]) -> StatePatch:
        """函证第 1 版与回函文件 (received) 交节点写入, 执行记录置成功终态.

        几个账户可能共用一份回函 (同一封信答复多个账户, 存档按哈希返回同一记录): 文件按 ID 去重, 保持首次出现的顺序.
        """
        documents = {f.document.id: f.document for f in findings}
        return {
            "confirmations": [f.confirmation for f in findings],
            "documents": list(documents.values()),
            "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
        }
