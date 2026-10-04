"""A6 CSH-02 函证差异评价: 规则部分 D01 比较回函与账面余额 (合成数据集设计第 5.4 节, 第 7 节第 13 行)."""

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.models import (
    Confirmation,
    ConfirmationResult,
    EvidencePointer,
    EvidenceType,
    Phase,
    ReceiptChannel,
    Reliability,
    RunStatus,
)
from backend.contracts.records import TrialBalanceRow
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_RECORDS,
    NUMBAT_REPLIES,
    NUMBAT_TRIAL_BALANCE,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    make_ctx,
    make_deps,
)

EID = ENGAGEMENT.id


def extracted(account: str, balance: int | None, reply: int) -> Confirmation:
    """DOC-06 抽取后的函证第 2 版: 回函余额, 截至日与回函余额所在页的指针."""
    doc = NUMBAT_REPLIES[reply]
    pointer = EvidencePointer(
        type=EvidenceType.external,
        source_type="document",
        source_id=doc.id,
        locator="confirmed_balance_cents",
        page=1,
        reliability=Reliability.external_original,
    )
    return Confirmation(
        id=f"C-{account}",
        version=2,
        bank_account_id=account,
        confirming_party="bank",
        idempotency_key=f"{EID}:CSH-04:{account}",
        sent_at=NOW,
        reply_document_id=doc.id,
        receipt_channel=ReceiptChannel.direct_email,
        confirmed_balance_cents=balance,
        evidence=[pointer] if balance is not None else [],
    )


NUMBAT_CONFIRMATIONS = [extracted("BA-01", 120840000, 0), extracted("BA-02", 40000000, 1)]


def setup(
    confirmations: list[Confirmation] | None = None, books: list[TrialBalanceRow] | None = None
) -> tuple[FieldworkAgent, RunContext]:
    """A6 与 CSH-02 第 1 次执行: 财务数据有两个银行账户与本期试算表 (1010 = 125840000, 1020 = 40000000)."""
    financial = FakeFinancialStore(
        records=NUMBAT_RECORDS, trial_balance=NUMBAT_TRIAL_BALANCE if books is None else books
    )
    deps = make_deps(FakeRegistry(), FakeTrail(), financial_store=financial)
    ctx = make_ctx(deps, "CSH-02")
    procedure = ctx.procedure.model_copy(update={"phase": Phase.P2_Executing})
    state = ctx.state | {"confirmations": NUMBAT_CONFIRMATIONS if confirmations is None else confirmations}
    return FieldworkAgent(deps), RunContext(ctx.engagement, procedure, ctx.template, ctx.run, state)  # type: ignore[arg-type]


async def test_difference_creates_d01_and_updates_confirmation() -> None:
    agent, ctx = setup()
    patch = await agent.run(ctx)
    [d01] = patch["exceptions"]
    assert (d01.id, d01.rule_id, d01.severity, d01.source_ids, d01.amount_cents) == (
        "X-CSH-02-1-D01-1",
        "D01",
        "medium",
        ["BA-01"],
        -5000000,
    )
    assert (d01.procedure_id, d01.run_id) == ("CSH-02", "RUN-CSH-02-1")
    reply, ledger = d01.evidence
    assert (reply.source_type, reply.source_id, reply.locator, reply.page) == (
        "document",
        NUMBAT_REPLIES[0].id,
        "confirmed_balance_cents",
        1,
    )
    assert (ledger.source_type, ledger.source_id, ledger.reliability) == (
        "trial_balance",
        "1010",
        Reliability.internal_original,
    )
    c1 = patch["confirmations"][0]
    assert (c1.id, c1.version, c1.book_balance_cents, c1.difference_cents, c1.result) == (
        "C-BA-01",
        3,
        125840000,
        -5000000,
        ConfirmationResult.difference,
    )


async def test_agreed_account_has_no_exception() -> None:
    agent, ctx = setup()
    patch = await agent.run(ctx)
    c2 = patch["confirmations"][1]
    assert (c2.id, c2.version, c2.book_balance_cents, c2.difference_cents, c2.result) == (
        "C-BA-02",
        3,
        40000000,
        0,
        ConfirmationResult.agreed,
    )
    assert [x.source_ids for x in patch["exceptions"]] == [["BA-01"]]


async def test_two_differences_numbered_by_account() -> None:
    confirmations = [extracted("BA-02", 39000000, 1), extracted("BA-01", 120840000, 0)]
    agent, ctx = setup(confirmations)
    patch = await agent.run(ctx)
    assert [(x.id, x.source_ids, x.amount_cents) for x in patch["exceptions"]] == [
        ("X-CSH-02-1-D01-1", ["BA-01"], -5000000),
        ("X-CSH-02-1-D01-2", ["BA-02"], -1000000),
    ]
    assert [c.id for c in patch["confirmations"]] == ["C-BA-01", "C-BA-02"]


async def test_all_agreed() -> None:
    books = [
        TrialBalanceRow(period="current", account_code="1010", balance_cents=120840000),
        TrialBalanceRow(period="current", account_code="1020", balance_cents=40000000),
        TrialBalanceRow(period="prior", account_code="1010", balance_cents=1),
    ]
    agent, ctx = setup(books=books)
    patch = await agent.run(ctx)
    assert patch["exceptions"] == [] and patch["judgments"] == [] and patch["review_tasks"] == []
    assert {c.result for c in patch["confirmations"]} == {ConfirmationResult.agreed}
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.succeeded and run.ended_at is not None


async def test_missing_reply_balance_raises() -> None:
    agent, ctx = setup([extracted("BA-01", None, 0)])
    with pytest.raises(ValueError, match="C-BA-01"):
        await agent.run(ctx)


async def test_unknown_account_raises() -> None:
    agent, ctx = setup([extracted("BA-09", 1, 0)])
    with pytest.raises(LookupError, match="BA-09"):
        await agent.run(ctx)
