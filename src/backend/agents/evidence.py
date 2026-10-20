"""A5: CSH-04 函证发起与 GEN-08 股权穿透 (需求 R137, agents/ownership.py), 按程序编号分派. CSH-04 返回字段:
confirmations, documents, procedure_runs; GEN-08 见 ownership.ownership_patch.

函证跟着当前的银行账户走 (需求 R274): 发函的幂等键含账户的识别信息 (开户行, BSB, 账号). 账户从银行账户表删了,
或识别信息变了, 它原来的函证不再是当前的 (留作历史, DOC-06, CSH-02 不再处理, 见 current_confirmations); 识别信息
变了的账户按新账户重新发函, 出另一个函证 (C-{账户 ID}-{n}).

识别信息进键之前发的函 (旧写法的键 {eid}:CSH-04:{账户 ID}): 函证只记了开户行 (confirming_party, 审计师还能改),
没记 BSB 与账号, 发函当时的识别信息无从核对, 改看发函之后银行账户表变没变过 (legacy_holds): 没变过视同识别信息
没变, 仍按它重建, 仍是当前的; 变过的不再是当前的, CSH-04 按新键重新发函一次, 之后按新键去重.
"""

import hashlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import ClassVar

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, finish_run
from backend.agents.ownership import Penetration, Penetrator, ownership_patch
from backend.contracts.data import DataInput
from backend.contracts.models import (
    AgentId,
    Confirmation,
    DocumentRef,
    DocumentType,
    ProcedureRun,
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


def account_identity(account: BankAccount) -> str:
    """账户识别信息 (开户行, BSB, 账号) 的摘要, sha256 前 12 位: 变了即视为新账户."""
    text = "\x1f".join((account.bank_name, account.bsb, account.account_no))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def confirmation_key(eid: str, account: BankAccount) -> str:
    """发函的幂等键: {eid}:CSH-04:{账户 ID}:{识别信息摘要}."""
    return f"{eid}:CSH-04:{account.id}:{account_identity(account)}"


def legacy_key(eid: str, account_id: str) -> str:
    """识别信息进幂等键之前的写法 {eid}:CSH-04:{账户 ID}: 那时发的函只在发函之后银行账户表没变过时视同识别信息没变
    (legacy_holds)."""
    return f"{eid}:CSH-04:{account_id}"


async def legacy_holds(deps: AgentDeps, eid: str, sent: TrailEntry) -> bool:
    """旧写法键的发函 (sent 为其成功留痕) 是否仍对得上当前账户: 发函那次执行 (留痕的 run_id, 取 Store 中的执行记录)
    记了数据版本 v, 而 DataStore.changes 中没有 v 之后的银行账户表变动. 变过即不成立 (识别信息不明, 不论变的是不是
    这个账户, 都按新键重发一次). 执行查不到或没记数据版本 (R16 之前的执行) 时无从比较, 仍视同没变."""
    run = await deps.output_store.get(eid, ProcedureRun, sent.run_id) if sent.run_id else None
    if run is None or run.data_version is None:
        return True
    version = run.data_version
    return not any(
        c.input is DataInput.bank_accounts and c.data_version > version for c in await deps.data.changes(eid)
    )


async def legacy_accounts(deps: AgentDeps, eid: str, accounts: Iterable[BankAccount]) -> set[str]:
    """旧写法键的函证仍是当前的账户 ID: 有旧写法键的成功发函留痕, 且 legacy_holds."""
    found: set[str] = set()
    for account in accounts:
        sent = await deps.trail.find_success(eid, legacy_key(eid, account.id))
        if sent is not None and await legacy_holds(deps, eid, sent):
            found.add(account.id)
    return found


def current_confirmations(
    confirmations: Iterable[Confirmation], eid: str, accounts: Iterable[BankAccount], legacy: Collection[str] = ()
) -> list[Confirmation]:
    """当前的函证: 幂等键对得上当前银行账户表中某账户的现有识别信息; legacy 中的账户 (legacy_accounts) 另认旧写法
    的键. 账户删了, 识别信息变了, 或旧写法的键已不成立的函证留作历史, 不在其中. 保持原顺序."""
    listed = list(accounts)
    keys = {confirmation_key(eid, a) for a in listed} | {legacy_key(eid, a.id) for a in listed if a.id in legacy}
    return [c for c in confirmations if c.idempotency_key in keys]


def reply_reliability(channel: ReceiptChannel | None) -> Reliability:
    """回函原件的可靠性, 按回执渠道定."""
    return Reliability.external_original if channel in DIRECT_CHANNELS else Reliability.external_copy


@dataclass(frozen=True)
class Confirmed:
    """一个银行账户的函证结果: 函证与存档的回函."""

    confirmation: Confirmation  # 新发函为第 1 版 (结果待 DOC-06, CSH-02 补); 已发过函的为 Store 中的最新版本
    document: DocumentRef  # 存档的回函


Findings = list[Confirmed] | Penetration  # CSH-04 的各账户函证结果, GEN-08 的登记查询所得


class EvidenceAgent(ProcedureAgent[Findings]):
    """A5: 执行 CSH-04 (向银行发询证函并存档回函) 与 GEN-08 (股权穿透与关系图, 功能 related_parties, 未启用时注册表
    不映射), 按程序编号分派.

    发函是外部动作, 按接口文档第 6 节三步保证不重复: 先按幂等键查成功留痕, 有则按留痕重建; 没有才发函,
    存档回函, 再写带幂等键的 external_call 留痕. GEN-08 的登记查询同样按幂等键查成功留痕, 有则重建 (ownership.Penetrator
    .query, 需求 R138).
    """

    agent_id: ClassVar[AgentId] = AgentId.A5  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"CSH-04", "GEN-08"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> Findings:
        """GEN-08 交 ownership.Penetrator 查登记; CSH-04 对每个银行账户 (按账户 ID 排序) 依次函证."""
        if ctx.procedure.id == "GEN-08":
            return await Penetrator(self.deps, ctx).run()
        accounts = sorted(await self.deps.financial_store.bank_accounts(ctx.engagement.id), key=lambda a: a.id)
        return [await self.confirm(ctx, account) for account in accounts]

    async def confirm(self, ctx: RunContext, account: BankAccount) -> Confirmed:
        """一个账户的函证: 已有成功留痕 (按含识别信息的幂等键; 没有时按旧写法的键, 且 legacy_holds) 时按留痕重建 (见
        rebuild), 不再发函与存档; 否则发函, 存档回函, 写留痕, 出第 1 版. CSH-04 重跑 (如银行账户表变动后的过期重跑)
        时, 识别信息没变的账户都走重建; 新账户, 识别信息变了的账户与旧写法的键已不成立的账户才发函. 函证 ID: 该账户
        还没有函证为 C-{账户 ID}, 已有 (识别信息变过) 为 C-{账户 ID}-{n}, n 从 2 起取未用的; ID 记进留痕, 重建按它."""
        eid = ctx.engagement.id
        key = confirmation_key(eid, account)
        done = await self.deps.trail.find_success(eid, key)
        if done is not None:
            return await self.rebuild(ctx, account, key, done.payload)
        legacy = legacy_key(eid, account.id)
        done = await self.deps.trail.find_success(eid, legacy)
        if done is not None and await legacy_holds(self.deps, eid, done):
            return await self.rebuild(ctx, account, legacy, done.payload)
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
            id=await self.new_id(eid, account),
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
                    "confirmation_id": confirmation.id,
                    "sent_at": receipt.sent_at.isoformat(),
                    "replied_at": receipt.replied_at.isoformat() if receipt.replied_at else None,
                    "receipt_channel": receipt.receipt_channel.value if receipt.receipt_channel else None,
                    "reply_document_id": document.id,
                },
            )
        )
        return Confirmed(confirmation, document)

    async def new_id(self, eid: str, account: BankAccount) -> str:
        """新发函的函证 ID: 该账户在 Store 中还没有函证为 C-{账户 ID}, 已有为 C-{账户 ID}-{n} (n 从 2 起取未用的)."""
        taken = {c.id for c in await self.deps.output_store.latest(eid, Confirmation, bank_account_id=account.id)}
        base = f"C-{account.id}"
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    async def archive(self, eid: str, account: BankAccount, receipt: ConfirmationReceipt) -> DocumentRef:
        """存档回函原件 (type confirmation, 来源渠道 bank_direct, 可靠性按回执渠道).

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
            channel="bank_direct",
        )

    async def rebuild(self, ctx: RunContext, account: BankAccount, key: str, payload: dict[str, object]) -> Confirmed:
        """按成功留痕重建函证: 回函取已登记的文件 (FinancialDataStore.documents). Store 中已有该函证时沿用其最新版本
        (以 Store 为准: 之后 DOC-06, CSH-02 与审计师修改出过新版本, 重跑 CSH-04 不退回第 1 版, 也不出新版本), 回函取
        该版 reply_document_id 的文件 (换过原件的取新回函); 没有时 (发函留痕已写, 函证还没写入 Store 就中断) 按留痕
        payload 的回函与发函时间等出第 1 版. 函证 ID 取留痕的 confirmation_id (旧留痕没有, 为 C-{账户 ID}).
        回函未登记抛 LookupError."""
        eid = ctx.engagement.id
        documents = {d.id: d for d in await self.deps.financial_store.documents(eid, DocumentType.confirmation)}
        confirmation_id = str(payload.get("confirmation_id") or f"C-{account.id}")
        stored = await self.deps.output_store.get(eid, Confirmation, confirmation_id)
        document_id = str(payload["reply_document_id"])
        if stored is not None and stored.reply_document_id is not None:
            document_id = stored.reply_document_id
        document = documents.get(document_id)
        if document is None:
            raise LookupError(f"{key} 的回函 {document_id} 未登记")
        if stored is not None:
            return Confirmed(stored, document)
        replied_at = payload.get("replied_at")
        channel = payload.get("receipt_channel")
        confirmation = Confirmation(
            id=confirmation_id,
            bank_account_id=account.id,
            confirming_party=account.bank_name,
            idempotency_key=key,
            sent_at=datetime.fromisoformat(str(payload["sent_at"])),
            reply_document_id=document.id,
            receipt_channel=ReceiptChannel(str(channel)) if channel is not None else None,
            replied_at=date.fromisoformat(str(replied_at)) if replied_at is not None else None,
        )
        return Confirmed(confirmation, document)

    async def interpret(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """GEN-08 见 ownership.ownership_patch. CSH-04: 函证 (新发函的第 1 版, 已发过函的为 Store 中的最新版本) 与
        回函文件 (received) 交节点写入, 执行记录置成功终态.

        几个账户可能共用一份回函 (同一封信答复多个账户, 存档按哈希返回同一记录): 文件按 ID 去重, 保持首次出现的顺序.
        """
        if isinstance(findings, Penetration):
            return ownership_patch(ctx, findings)
        documents = {f.document.id: f.document for f in findings}
        return {
            "confirmations": [f.confirmation for f in findings],
            "documents": list(documents.values()),
            "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
        }
