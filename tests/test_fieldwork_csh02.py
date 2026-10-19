"""A6 CSH-02 函证差异评价: 规则部分 D01 比较回函与账面余额, 有差异的账户交判断 (合成数据集设计第 7 节第 12, 13 行)."""

from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.evidence import legacy_key
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.data import DataInput
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    Confirmation,
    ConfirmationResult,
    EvidencePointer,
    EvidenceType,
    ExceptionDisposition,
    Phase,
    ProcedureStatus,
    ProposedAction,
    ReceiptChannel,
    Reliability,
    ReviewStatus,
    RunStatus,
    TaskKind,
    TrailKind,
)
from backend.contracts.records import DatasetTraits, TrialBalanceRow
from backend.orchestration.nodes import GraphNodes
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_D02_ROWS,
    NUMBAT_RECORDS,
    NUMBAT_REPLIES,
    NUMBAT_RISKS,
    NUMBAT_TRIAL_BALANCE,
    FakeDataStore,
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeTrail,
    finished,
    legacy_sends,
    make_ctx,
    make_deps,
    make_procedure,
    make_run,
    numbat_confirmation_key,
    numbat_csh05_exception,
)
from tests.test_fieldwork_judging import failed, passed

EID = ENGAGEMENT.id
B01 = numbat_csh05_exception(make_run("CSH-05"))  # X-CSH-05-1-B01-1


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
        idempotency_key=numbat_confirmation_key(EID, account),
        sent_at=NOW,
        reply_document_id=doc.id,
        receipt_channel=ReceiptChannel.direct_email,
        confirmed_balance_cents=balance,
        balance_date=date(2026, 6, 30) if balance is not None else None,
        evidence=[pointer] if balance is not None else [],
    )


NUMBAT_CONFIRMATIONS = [extracted("BA-01", 120840000, 0), extracted("BA-02", 40000000, 1)]


def setup(
    confirmations: list[Confirmation] | None = None,
    books: list[TrialBalanceRow] | None = None,
    outcomes: dict[str, Any] | None = None,
    exceptions: list[Any] | None = None,
    real_engine: bool = False,
) -> tuple[FieldworkAgent, RunContext, Any]:
    """A6 与 CSH-02 第 1 次执行: 财务数据有两个银行账户, 本期试算表 (1010 = 125840000, 1020 = 40000000) 与两份回函;
    状态有 CSH-05 的余额不符例外与回函文件. 判断引擎缺省为假引擎 (L2 close, 待复核), real_engine 时为真实引擎 (mock)."""
    financial = FakeFinancialStore(
        list(NUMBAT_REPLIES), NUMBAT_RECORDS, trial_balance=NUMBAT_TRIAL_BALANCE if books is None else books
    )
    trail = FakeTrail()
    deps = make_deps(FakeRegistry(), trail, financial_store=financial)
    engine: Any = deps.judgments
    if not real_engine:
        default = passed(proposed_action=ProposedAction.close)
        engine = FakeJudgmentEngine(outcomes or {a: default for a in ("BA-01", "BA-02")})
        deps = replace(deps, judgments=engine, review_llm=None)
    ctx = make_ctx(deps, "CSH-02")
    procedure = ctx.procedure.model_copy(update={"phase": Phase.P2_Executing, "objective_ids": ["OBJ-CASH-EXI"]})
    params = deps.library.resolve_params(ctx.template, ctx.engagement, ctx.state, None, traits=DatasetTraits())
    state = ctx.state | {
        "confirmations": NUMBAT_CONFIRMATIONS if confirmations is None else confirmations,
        "documents": list(NUMBAT_REPLIES),
        "exceptions": [B01] if exceptions is None else exceptions,
        "risk_register": NUMBAT_RISKS,
    }
    run = ctx.run.model_copy(update={"params_snapshot": params})
    ctx = RunContext(ctx.engagement, procedure, ctx.template, run, state)  # type: ignore[arg-type]
    return FieldworkAgent(deps), ctx, engine if not real_engine else trail


# ── 规则部分 ─────────────────────────────────────────


async def test_difference_creates_d01_and_updates_confirmation() -> None:
    agent, ctx, _ = setup()
    patch = await agent.run(ctx)
    [d01] = patch["exceptions"]
    assert (d01.id, d01.version, d01.rule_id, d01.severity, d01.source_ids, d01.amount_cents) == (
        "X-CSH-02-1-D01-1",
        1,
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


async def test_agreed_account_has_no_exception_and_no_judgment() -> None:
    agent, ctx, engine = setup()
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
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == ["BA-01"]


async def test_two_differences_numbered_by_account() -> None:
    confirmations = [extracted("BA-02", 39000000, 1), extracted("BA-01", 120840000, 0)]
    agent, ctx, _ = setup(confirmations)
    patch = await agent.run(ctx)
    d01s = [x for x in patch["exceptions"] if x.rule_id == "D01"]
    assert [(x.id, x.source_ids, x.amount_cents) for x in d01s] == [
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
    agent, ctx, engine = setup(books=books)
    patch = await agent.run(ctx)
    assert patch["exceptions"] == [] and patch["judgments"] == [] and patch["review_tasks"] == []
    assert {c.result for c in patch["confirmations"]} == {ConfirmationResult.agreed}
    assert engine.calls == []
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.succeeded and run.ended_at is not None


async def test_missing_reply_balance_raises() -> None:
    agent, ctx, _ = setup([extracted("BA-01", None, 0)])
    with pytest.raises(ValueError, match="C-BA-01"):
        await agent.run(ctx)


async def test_confirmation_of_account_not_in_table_is_history() -> None:
    """函证跟着当前的银行账户走: 账户已不在银行账户表中的函证不是当前的, CSH-02 不处理它 (不出新版本, 不报错)."""
    agent, ctx, _ = setup([extracted("BA-09", 1, 0)])
    patch = await agent.run(ctx)
    assert patch["confirmations"] == [] and patch["exceptions"] == []


async def test_legacy_key_confirmations_follow_bank_account_changes() -> None:
    """R274-16: 识别信息进键之前发的函 (旧写法的键): 发函之后银行账户表没变过, 仍是当前的, CSH-02 照常比对 (BA-01
    出 D01); 变过了, 旧键不再成立, 不再处理 (不出新版本, 不报错, 等 CSH-04 按新键重发)."""
    legacy = [
        c.model_copy(update={"idempotency_key": legacy_key(EID, c.bank_account_id)}) for c in NUMBAT_CONFIRMATIONS
    ]
    for changed, expected in ((False, ["BA-01", "BA-02"]), (True, [])):
        agent, ctx, _ = setup(legacy)
        await legacy_sends(agent.deps, EID, ["BA-01", "BA-02"])
        data = agent.deps.data
        assert isinstance(data, FakeDataStore)
        if changed:
            data.record(DataInput.bank_accounts)
        patch = await agent.run(ctx)
        assert [c.bank_account_id for c in patch["confirmations"]] == expected, changed
        assert [x.rule_id for x in patch["exceptions"]] == (["D01"] if expected else [])


# ── 判断部分 ─────────────────────────────────────────


async def test_judging_context_for_ba01() -> None:
    agent, ctx, engine = setup()
    await agent.run(ctx)
    [(procedure_id, jctx, performed_by)] = engine.calls
    assert (procedure_id, jctx.subject_id, jctx.fixture_key, performed_by) == ("CSH-02", "BA-01", "BA-01", "U-1")
    data = jctx.data
    assert data["bank_account"] == {"source_type": "bank_account", **NUMBAT_RECORDS[("bank_account", "BA-01")]}
    assert (data["confirmation"]["id"], data["confirmation"]["version"]) == ("C-BA-01", 3)
    assert data["confirmation"]["balance_date"] == "2026-06-30"  # 回函余额截至期末 (CSH-02 判定标准 1)
    assert data["reply_document"] == NUMBAT_REPLIES[0].model_dump(mode="json")
    assert (data["book_balance_cents"], data["difference_cents"]) == (125840000, -5000000)
    assert [r["id"] for r in data["subsequent_transactions"]] == [r["id"] for r in NUMBAT_D02_ROWS]
    # BT-01-20260702-01, 日期为 JSON 字符串
    assert data["subsequent_transactions"][2] == {**NUMBAT_D02_ROWS[2], "txn_date": "2026-07-02"}
    assert [r["id"] for r in data["period_end_entries"]] == [
        "CR-SI-2026-0547-1",
        "CP-AP-2026-06-B-2",
        "CP-FRT-2026-06-2",
        "CP-OPEX-2026-06-2",
    ]
    assert [x.id for x in jctx.rule_findings] == ["X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1"]
    assert jctx.task["params"] == {"period_end": "2026-06-30", "subsequent_days": 10, "lookback_days": 5}
    assert [r["id"] for r in jctx.task["risks"]] == ["RISK-CASH-EXI"]


async def test_attributed_csh05_exception_not_passed_again() -> None:
    """CSH-05 的例外已被别的判断解释 (attributed) 时不再作为该账户的规则例外."""
    explained = B01.model_copy(
        update={"version": 2, "disposition": ExceptionDisposition.attributed, "attributed_by": "J-X"}
    )
    agent, ctx, engine = setup(exceptions=[explained])
    await agent.run(ctx)
    [(_, jctx, _)] = engine.calls
    assert [x.id for x in jctx.rule_findings] == ["X-CSH-02-1-D01-1"]


async def test_l3_close_still_reviewed_and_exceptions_stay_open() -> None:
    outcomes = {"BA-01": passed(autonomy_level=AutonomyLevel.L3, proposed_action=ProposedAction.close)}
    agent, ctx, _ = setup(outcomes=outcomes)
    patch = await agent.run(ctx)
    [j] = patch["judgments"]
    assert j.id == "J-CSH-02-1-BA-01" and j.review_status is ReviewStatus.pending
    assert j.exception_ids == ["X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1"]
    [task] = patch["review_tasks"]
    assert (task.id, task.kind) == ("T-J-CSH-02-1-BA-01-v1", TaskKind.normal)
    assert [(x.id, x.version, x.disposition) for x in patch["exceptions"]] == [
        ("X-CSH-02-1-D01-1", 1, ExceptionDisposition.open)
    ]


async def test_failure_marks_csh05_exception_unattributed() -> None:
    agent, ctx, _ = setup(outcomes={"BA-01": failed("unattributed")})
    patch = await agent.run(ctx)
    assert patch["judgments"] == []
    assert [(x.id, x.version, x.disposition) for x in patch["exceptions"]] == [
        ("X-CSH-02-1-D01-1", 1, ExceptionDisposition.open),
        ("X-CSH-05-1-B01-1", 2, ExceptionDisposition.unattributed),
    ]
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, task.subject_id) == ("T-X-CSH-05-1-B01-1-v2", TaskKind.unattributed, "BA-01")


async def test_failure_without_csh05_exception_keeps_one_d01_version() -> None:
    """没有前置例外时第一条规则例外就是本次的 D01: 片段里只留它的 v2 unattributed."""
    agent, ctx, _ = setup(outcomes={"BA-01": failed("unattributed")}, exceptions=[])
    patch = await agent.run(ctx)
    assert [(x.id, x.version, x.disposition) for x in patch["exceptions"]] == [
        ("X-CSH-02-1-D01-1", 2, ExceptionDisposition.unattributed)
    ]
    assert [t.id for t in patch["review_tasks"]] == ["T-X-CSH-02-1-D01-1-v2"]


async def test_patch_summary_and_merge_check() -> None:
    agent, ctx, _ = setup()
    csh05 = make_procedure("CSH-05", Phase.P2_Executing, status=ProcedureStatus.completed)
    state = ctx.state | {
        "procedure_pack": [*ctx.state["procedure_pack"], csh05],
        "procedure_runs": [finished(make_run("CSH-05"))],
    }
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, state)  # type: ignore[arg-type]
    patch, writer = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A6, patch["procedure_runs"][0].failure_reason
    assert [x.id for x in patch["exceptions"]] == ["X-CSH-02-1-D01-1"]
    assert [(c.id, c.version) for c in patch["confirmations"]] == [("C-BA-01", 3), ("C-BA-02", 3)]
    assert [j.id for j in patch["judgments"]] == ["J-CSH-02-1-BA-01"] and len(patch["review_tasks"]) == 1
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


# ── 模拟输出 ─────────────────────────────────────────


async def test_ba01_fixture_passes_real_engine() -> None:
    agent, ctx, trail = setup(real_engine=True)
    patch = await agent.run(ctx)
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    # A6 一次, 再由 A7 (L3, 建议关闭, 命中主规则) 用复核模型做两轮独立复核
    assert [(e.procedure_id, e.actor, e.payload["fixture_key"]) for e in calls] == [
        ("CSH-02", "A6", "BA-01"),
        ("GEN-09", "A7", "BA-01-r1"),
        ("GEN-09", "A7", "BA-01-r2"),
    ]
    assert all(e.payload["attempt"] == 1 and e.payload["level_failed"] is None for e in calls)
    [j] = patch["judgments"]
    assert j.proposed_action is ProposedAction.close and j.misstatement_draft is None
    # 一致: 置信度计入复核分项 (0.3 × 0.9 + 0.3 × 0 + 0.2 × 0.9 + 0.2 × 1) / 1.0
    assert (j.critic_verdict, j.critic_trail_ref, j.confidence) == ("consistent", calls[2].id, 0.65)
    assert "50,000.00" in j.conclusion and "deposit in transit" in j.conclusion
    assert "CR-SI-2026-0547" in j.conclusion and "BT-01-20260702-01" in j.conclusion
    sources = {(p.source_type, p.source_id) for p in j.evidence}
    assert sources >= {
        ("document", NUMBAT_REPLIES[0].id),
        ("trial_balance", "1010"),
        ("bank_transaction", "BT-01-20260702-01"),
        ("journal_entry", "CR-SI-2026-0547-1"),
        ("standard", "ASA505-P14"),
    }
    reply = next(p for p in j.evidence if p.locator == "confirmed_balance_cents")
    assert reply.page == 1
    assert [t.id for t in patch["review_tasks"]] == ["T-J-CSH-02-1-BA-01-v1"]
