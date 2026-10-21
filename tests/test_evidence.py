"""A5 CSH-04 函证发起: 按幂等键不重复发函, 存档回函, 建函证 (接口文档第 6 节外部动作, 合成数据集设计第 7 节第 10 行)."""

from datetime import date
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.evidence import (
    EvidenceAgent,
    confirmation_key,
    current_confirmations,
    legacy_accounts,
    legacy_key,
)
from backend.contracts.data import DataInput
from backend.contracts.models import (
    AgentId,
    Confirmation,
    ConfirmationResult,
    DocumentType,
    Procedure,
    ReceiptChannel,
    Reliability,
    ReviewTask,
    RunStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import ConfirmationReceipt, ConfirmationRequest
from backend.contracts.records import BankAccount, DatasetTraits
from backend.orchestration.nodes import GraphNodes, apply_patch
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_RECORDS,
    FakeConfirmationChannel,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    make_ctx,
    make_deps,
    preset_rerun,
)
from tests.test_fieldwork import dispatch

EID = ENGAGEMENT.id
# 发函的幂等键含账户识别信息 (开户行, BSB, 账号) 的摘要
KEYS = [
    confirmation_key(EID, BankAccount.model_validate(NUMBAT_RECORDS[("bank_account", a)])) for a in ("BA-01", "BA-02")
]


def setup(
    channel: FakeConfirmationChannel | None = None, trail: FakeTrail | None = None, as_of: date | None = None
) -> tuple[EvidenceAgent, RunContext, FakeConfirmationChannel, FakeTrail, Any]:
    """A5 与 CSH-04 第 1 次执行: 财务数据有 Numbat 的两个银行账户 (存储按 BA-02, BA-01 的顺序返回),
    参数快照 as_of 缺省为期末."""
    channel = channel or FakeConfirmationChannel()
    trail = trail or FakeTrail()
    records = dict(reversed(list(NUMBAT_RECORDS.items())))  # 存储返回的顺序没有约定, 倒过来检查 A5 自己排序
    deps = make_deps(FakeRegistry(), trail, financial_store=FakeFinancialStore(records=records), confirmations=channel)
    ctx = make_ctx(deps, "CSH-04")
    params = deps.library.resolve_params(
        ctx.template, ctx.engagement, ctx.state, None, traits=DatasetTraits(), profile=None
    )
    if as_of is not None:
        params["as_of"] = as_of
    run = ctx.run.model_copy(update={"params_snapshot": params})
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, run, ctx.state)
    return EvidenceAgent(deps), ctx, channel, trail, deps


async def test_confirmations_for_each_account() -> None:
    agent, ctx, channel, _, _ = setup()
    patch = await agent.run(ctx)
    documents = {d.file_name: d for d in patch["documents"]}
    c1, c2 = patch["confirmations"]
    for c, account, bank, key in (
        (c1, "BA-01", "Harbourline Bank Limited", KEYS[0]),
        (c2, "BA-02", "Bluegum Savings Bank Limited", KEYS[1]),
    ):
        receipt = channel.receipts[key]
        assert (c.id, c.version, c.bank_account_id, c.confirming_party, c.idempotency_key) == (
            f"C-{account}",
            1,
            account,
            bank,
            key,
        )
        assert c.reply_document_id == documents[receipt.reply_file_name].id
        assert (c.receipt_channel, c.sent_at, c.replied_at) == (
            ReceiptChannel.direct_email,
            receipt.sent_at,
            receipt.replied_at,
        )
        assert c.result is ConfirmationResult.pending
    assert [(d.type, d.reliability, d.status) for d in patch["documents"]] == [
        (DocumentType.confirmation, Reliability.external_original, "received")
    ] * 2
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.succeeded and run.ended_at is not None


async def test_submit_and_trail() -> None:
    agent, ctx, channel, trail, _ = setup()
    patch = await agent.run(ctx)
    assert [key for key, _ in channel.submits] == KEYS  # 按账户 ID 排序, 不按存储返回的顺序
    assert {req.as_of for _, req in channel.submits} == {date(2026, 6, 30)}
    assert [(req.bank_name, req.bsb, req.account_no) for _, req in channel.submits] == [
        ("Harbourline Bank Limited", "999-101", "12345601"),
        ("Bluegum Savings Bank Limited", "999-202", "23456702"),
    ]
    calls = [e for e in trail.entries if e.kind is TrailKind.external_call]
    assert [e.idempotency_key for e in calls] == KEYS
    for entry, c in zip(calls, patch["confirmations"], strict=True):
        assert (entry.actor, entry.procedure_id, entry.run_id) == ("A5", "CSH-04", "RUN-CSH-04-1")
        assert entry.payload == {
            "bank_account_id": c.bank_account_id,
            "confirmation_id": c.id,
            "sent_at": c.sent_at.isoformat(),
            "replied_at": c.replied_at.isoformat() if c.replied_at else None,
            "receipt_channel": "direct_email",
            "reply_document_id": c.reply_document_id,
        }


async def test_replay_rebuilds_from_trail_without_sending() -> None:
    agent, ctx, _, trail, deps = setup()
    first = await agent.run(ctx)
    # 同一项目重启: 留痕与已登记的回函还在, 函证平台是新的
    channel = FakeConfirmationChannel()
    again, ctx2, _, _, deps2 = setup(channel, trail)
    deps2.financial_store.document_refs.extend(deps.financial_store.document_refs)
    saves_before = len(deps2.evidence_store.saves)
    second = await again.run(ctx2)
    assert channel.submits == [] and len(deps2.evidence_store.saves) == saves_before
    assert second["confirmations"] == first["confirmations"]
    assert second["documents"] == first["documents"]
    assert len([e for e in trail.entries if e.kind is TrailKind.external_call]) == 2


async def test_only_missing_account_is_sent() -> None:
    agent, ctx, _, trail, deps = setup()
    first = await agent.run(ctx)
    kept = [e for e in trail.entries if e.idempotency_key == KEYS[0]]
    trail.entries[:] = kept
    channel = FakeConfirmationChannel()
    again, ctx2, _, _, deps2 = setup(channel, trail)
    deps2.financial_store.document_refs.extend(deps.financial_store.document_refs[:1])
    second = await again.run(ctx2)
    assert [key for key, _ in channel.submits] == [KEYS[1]]
    assert second["confirmations"][0] == first["confirmations"][0]


class FailingTrail(FakeTrail):
    """第一次写 external_call 留痕时抛错."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def record(self, entry: TrailEntry) -> str:
        if entry.kind is TrailKind.external_call and not self.failed:
            self.failed = True
            raise RuntimeError("trail unavailable")
        return await super().record(entry)


async def test_trail_failure_then_rerun_reuses_receipt() -> None:
    """发函与存档成功而留痕写失败: 再运行时查不到留痕, 用同一键再发, 平台给同一回执, 存档得同一文件."""
    trail = FailingTrail()
    channel = FakeConfirmationChannel()
    agent, ctx, _, _, deps = setup(channel, trail)
    with pytest.raises(RuntimeError, match="trail unavailable"):
        await agent.run(ctx)
    first_receipt = channel.receipts[KEYS[0]]
    first_document = deps.financial_store.document_refs[0]
    patch = await agent.run(ctx)
    assert [key for key, _ in channel.submits] == [KEYS[0], KEYS[0], KEYS[1]]
    c1 = patch["confirmations"][0]
    assert c1.sent_at == first_receipt.sent_at and c1.reply_document_id == first_document.id
    assert len(deps.financial_store.document_refs) == 2  # 同一哈希不重复登记
    normal_agent, normal_ctx, _, _, _ = setup()
    normal = await normal_agent.run(normal_ctx)
    assert patch["confirmations"] == normal["confirmations"] and patch["documents"] == normal["documents"]


async def test_portal_reply_is_external_copy() -> None:
    agent, ctx, _, _, _ = setup(FakeConfirmationChannel(ReceiptChannel.portal_via_client))
    patch = await agent.run(ctx)
    assert {d.reliability for d in patch["documents"]} == {Reliability.external_copy}
    assert {c.receipt_channel for c in patch["confirmations"]} == {ReceiptChannel.portal_via_client}


async def test_as_of_comes_from_params_snapshot() -> None:
    agent, ctx, channel, _, _ = setup(as_of=date(2026, 7, 15))
    await agent.run(ctx)
    assert {req.as_of for _, req in channel.submits} == {date(2026, 7, 15)}


@pytest.mark.parametrize("channel", [FakeConfirmationChannel(replies=False), FakeConfirmationChannel(body=b"")])
async def test_receipt_without_reply_raises(channel: FakeConfirmationChannel) -> None:
    """没有回函 (无文件名) 或回函内容为空: 不存档, 不写留痕, 抛错."""
    agent, ctx, _, trail, deps = setup(channel)
    with pytest.raises(ValueError, match="没有回函"):
        await agent.run(ctx)
    assert trail.entries == [] and deps.evidence_store.saves == []


async def test_shared_reply_document_listed_once() -> None:
    """一封回函答复两个账户 (同一内容, 存档得同一文件): 片段的文件去重, 两份函证都指向它, 合并校验通过; 重建时同样."""
    agent, ctx, _, trail, _ = setup(FakeConfirmationChannel(body=b"one letter for both accounts"))
    patch, writer, _ = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A5, patch["procedure_runs"][0].failure_reason
    [document] = patch["documents"]
    assert [c.reply_document_id for c in patch["confirmations"]] == [document.id, document.id]
    again = await agent.run(ctx)  # 留痕都在, 按留痕重建
    assert again["documents"] == [document] and again["confirmations"] == patch["confirmations"]
    assert len([e for e in trail.entries if e.kind is TrailKind.external_call]) == 2


class FailSecondOnce(FakeConfirmationChannel):
    """第二个账户第一次发函失败; 记下每次发函的幂等键."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str] = []
        self.failed = False

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        self.attempts.append(idempotency_key)
        if req.bank_account_id == "BA-02" and not self.failed:
            self.failed = True
            raise RuntimeError("second bank unavailable")
        return await super().submit(eid, req, idempotency_key)


async def test_second_account_failure_then_replay_and_rerun() -> None:
    """经 fieldwork 节点: 第二个账户发函失败, 整次失败且不写函证; 重放不再发函; rerun 只给第二个账户发函,
    两份函证都是第 1 版; 成功后再重放不发函, 不存档, 不写留痕."""
    channel = FailSecondOnce()
    _, ctx, _, trail, deps = setup(channel)
    nodes = GraphNodes(deps, 2)
    state = ctx.state
    await deps.output_store.put(EID, state["procedure_pack"], actor="A1")
    failed = await nodes.fieldwork(dispatch(state, "CSH-04"))
    assert set(failed["staged_results"][0]["patch"]) == {"procedure_runs", "review_tasks"}
    assert channel.attempts == KEYS and [e.idempotency_key for e in trail.entries] == [KEYS[0]]
    failed_state = apply_patch(state, failed)
    failed_state = apply_patch(failed_state, await nodes.fan_in(failed_state))
    assert failed_state["confirmations"] == [] and await deps.output_store.latest(EID, Confirmation) == []
    attempts = list(channel.attempts)
    replay = await nodes.fieldwork(dispatch(state, "CSH-04"))
    assert replay["staged_results"][0]["patch"]["procedure_runs"][0].status is RunStatus.failed
    assert channel.attempts == attempts
    task = failed_state["review_tasks"][0]
    await preset_rerun(deps.output_store, task)
    procedure = await deps.output_store.get(EID, Procedure, "CSH-04")
    assert procedure is not None
    failed_state["procedure_pack"] = [procedure]
    failed_state["review_tasks"] = await deps.output_store.latest(EID, ReviewTask)
    again = await nodes.fieldwork(dispatch(failed_state, "CSH-04"))
    [item] = again["staged_results"]
    assert item["patch"]["procedure_runs"][0].status is RunStatus.succeeded
    assert channel.attempts == [KEYS[0], KEYS[1], KEYS[1]]
    assert [c.version for c in item["patch"]["confirmations"]] == [1, 1]
    persisted = await nodes.fan_in(apply_patch(failed_state, again))
    assert [c.version for c in persisted["confirmations"]] == [1, 1]
    before = (len(channel.attempts), len(deps.evidence_store.saves), len(trail.entries))
    replay = await nodes.fieldwork(dispatch(failed_state, "CSH-04"))
    assert replay["staged_results"][0]["patch"]["procedure_runs"][0].status is RunStatus.succeeded
    assert (len(channel.attempts), len(deps.evidence_store.saves), len(trail.entries)) == before


async def legacy_case(
    data_version: int | None,
) -> tuple[EvidenceAgent, RunContext, FakeConfirmationChannel, Any, list[Confirmation], dict[str, str]]:
    """CSH-04 第 1 次执行后把留痕与函证换成识别信息进键之前的写法 (旧键 {eid}:CSH-04:{账户}, 留痕没有
    confirmation_id), 发函那次执行 (记数据版本 data_version) 存进新的 Store; 返回新的 A5 (同一留痕, 新的假函证平台),
    上下文, 平台, 依赖, 旧键的函证, 以及新键 → 旧键."""
    agent, ctx, _, trail, deps = setup()
    first = await agent.run(ctx)
    legacy = {key: legacy_key(EID, account) for key, account in zip(KEYS, ("BA-01", "BA-02"), strict=True)}
    trail.entries[:] = [
        e.model_copy(
            update={
                "idempotency_key": legacy[e.idempotency_key],
                "payload": {k: v for k, v in e.payload.items() if k != "confirmation_id"},
            }
        )
        if e.idempotency_key in legacy
        else e
        for e in trail.entries
    ]
    old = [c.model_copy(update={"idempotency_key": legacy[c.idempotency_key]}) for c in first["confirmations"]]
    channel = FakeConfirmationChannel()
    again, ctx2, _, _, deps2 = setup(channel, trail)
    deps2.financial_store.document_refs.extend(deps.financial_store.document_refs)
    sent = ctx.run.model_copy(update={"status": RunStatus.succeeded, "data_version": data_version})
    await deps2.output_store.put(EID, [sent, *old], actor=AgentId.A5)
    return again, ctx2, channel, deps2, old, legacy


@pytest.mark.parametrize("data_version", [1, None])
async def test_legacy_key_send_is_rebuilt_while_bank_accounts_unchanged(data_version: int | None) -> None:
    """识别信息进键之前发的函 (旧键, 留痕没有 confirmation_id): 函证没记 BSB 与账号, 改看发函之后银行账户表变没变过.
    没变过 (发函那次执行记的数据版本之后没有 bank_accounts 变动; 执行没记数据版本时无从比较, 同样) 视同识别信息
    没变: CSH-04 重跑按它重建, 不再发函; 这样的函证仍是当前的. 删掉的账户的函证照旧不是当前的."""
    agent, ctx, channel, deps, old, legacy = await legacy_case(data_version)
    deps.data.record(DataInput.trial_balance)  # 别的数据变了不算

    second = await agent.run(ctx)

    assert channel.submits == []
    assert [(c.id, c.idempotency_key) for c in second["confirmations"]] == [
        ("C-BA-01", legacy[KEYS[0]]),
        ("C-BA-02", legacy[KEYS[1]]),
    ]
    accounts = await deps.financial_store.bank_accounts(EID)
    held = await legacy_accounts(deps, EID, accounts)
    assert held == {"BA-01", "BA-02"}
    assert current_confirmations(old, EID, accounts, held) == old
    assert current_confirmations(old, EID, accounts) == []  # 不认旧键时都不是当前的
    assert current_confirmations(old, EID, [a for a in accounts if a.id == "BA-01"], held) == old[:1]


async def test_legacy_key_send_is_resent_after_bank_accounts_changed() -> None:
    """R274-16: 旧键的函发出之后银行账户表变过 (这里改了 BA-01 的账号, 合成): 识别信息不明, 旧键不再成立, 旧键的
    函证都不再是当前的 (DOC-06, CSH-02 不拿旧回函与新账户比), CSH-04 按新键给每个账户重新发函一次 (BA-02 没改也重发,
    代价已写明), 函证 ID 为 C-{账户}-2; 之后按新键去重, 不再发函."""
    agent, ctx, channel, deps, old, _ = await legacy_case(1)
    row = dict(deps.financial_store.records[("bank_account", "BA-01")]) | {"account_no": "99999901"}  # 合成
    deps.financial_store.records[("bank_account", "BA-01")] = row
    deps.data.record(DataInput.bank_accounts)
    accounts = await deps.financial_store.bank_accounts(EID)
    assert await legacy_accounts(deps, EID, accounts) == set()
    assert current_confirmations(old, EID, accounts, await legacy_accounts(deps, EID, accounts)) == []

    second = await agent.run(ctx)

    keys = [confirmation_key(EID, BankAccount.model_validate(row)), KEYS[1]]
    assert [(key, request.bank_account_id) for key, request in channel.submits] == list(
        zip(keys, ("BA-01", "BA-02"), strict=True)
    )
    assert [(c.id, c.idempotency_key) for c in second["confirmations"]] == [
        ("C-BA-01-2", keys[0]),
        ("C-BA-02-2", keys[1]),
    ]
    assert current_confirmations([*old, *second["confirmations"]], EID, accounts) == second["confirmations"]
    await agent.run(ctx)
    assert len(channel.submits) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("account_no", "99999901"), ("bsb", "999-901"), ("bank_name", "Synthetic Mutual Bank Ltd")],  # 合成
)
async def test_changed_identity_gets_new_confirmation_id_kept_in_trail(field: str, value: str) -> None:
    """账户识别信息变了 (改正账号, BSB 或开户行, 三项任一): 按新的幂等键重新发函, 函证 ID 取 C-{账户}-2 (C-BA-01
    已在 Store), 记进留痕; 旧函证不再是当前的; 写入 Store 之前中断再执行时按留痕的 ID 重建同一个函证, 不重发, 也不
    退回旧函证."""
    agent, ctx, channel, trail, deps = setup()
    first = await agent.run(ctx)
    old = next(c for c in first["confirmations"] if c.id == "C-BA-01")
    await deps.output_store.put(EID, [old], actor=AgentId.A5)
    row = dict(deps.financial_store.records[("bank_account", "BA-01")]) | {field: value}
    deps.financial_store.records[("bank_account", "BA-01")] = row
    new_key = confirmation_key(EID, BankAccount.model_validate(row))

    second = await agent.run(ctx)

    assert [key for key, _ in channel.submits[2:]] == [new_key]
    resent = next(c for c in second["confirmations"] if c.bank_account_id == "BA-01")
    assert (resent.id, resent.version, resent.idempotency_key) == ("C-BA-01-2", 1, new_key)
    [entry] = [e for e in trail.entries if e.idempotency_key == new_key]
    assert entry.payload["confirmation_id"] == "C-BA-01-2"
    accounts = await deps.financial_store.bank_accounts(EID)
    assert [c.id for c in current_confirmations([old, resent], EID, accounts)] == ["C-BA-01-2"]
    third = await agent.run(ctx)  # Store 里还没有 C-BA-01-2 (写入前中断): 按留痕重建
    assert len(channel.submits) == 3
    assert next(c for c in third["confirmations"] if c.bank_account_id == "BA-01") == resent
