"""A6 CSH-02 函证差异评价: 规则部分 D01 比较回函与账面余额, D04 查回函应计利息是否已入账, 有 D01 或 D04 的账户交判断
(合成数据集设计第 7 节第 12, 13 行; 需求 R105); 提示词按余额差异与应计利息分写 (需求 R106)."""

import json
import re
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
    AuditException,
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
    TaskStatus,
    TrailKind,
)
from backend.contracts.records import DatasetTraits, TrialBalanceRow
from backend.contracts.services import RuleResult
from backend.orchestration.nodes import GraphNodes, save_outputs
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_D02_ROWS,
    NUMBAT_D03_ROWS,
    NUMBAT_D04_ROWS,
    NUMBAT_RECORDS,
    NUMBAT_REPLIES,
    NUMBAT_RISKS,
    NUMBAT_TRIAL_BALANCE,
    FakeDataStore,
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    ScriptedLLM,
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
from tests.xero_harness import FlakyLLM, LayeredMockLLMClient, Ran, offline_deps, run_programs
from tests.xero_injection import XeroCase

EID = ENGAGEMENT.id
B01 = numbat_csh05_exception(make_run("CSH-05"))  # X-CSH-05-1-B01-1


def extracted(account: str, balance: int | None, reply: int, accrued: int | None = None) -> Confirmation:
    """DOC-06 抽取后的函证第 2 版: 回函余额, 截至日与回函余额所在页的指针; accrued 给了时另有回函写明的应计利息
    及其所在页的指针 (Numbat 的两份回函都没提, 为空)."""
    doc = NUMBAT_REPLIES[reply]
    pointers = [
        EvidencePointer(
            type=EvidenceType.external,
            source_type="document",
            source_id=doc.id,
            locator=locator,
            page=1,
            reliability=Reliability.external_original,
        )
        for locator in ("confirmed_balance_cents", *(["accrued_interest_cents"] if accrued is not None else []))
    ]
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
        accrued_interest_cents=accrued,
        balance_date=date(2026, 6, 30) if balance is not None else None,
        evidence=pointers if balance is not None else [],
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
    state = ctx.state | {
        "confirmations": NUMBAT_CONFIRMATIONS if confirmations is None else confirmations,
        "documents": list(NUMBAT_REPLIES),
        "exceptions": [B01] if exceptions is None else exceptions,
        "risk_register": NUMBAT_RISKS,
        "clearly_trivial_cents": 600000,  # PLN-01/03 闸门批准的明显微小 (合成数据集设计第 3.2 节)
    }
    params = deps.library.resolve_params(
        ctx.template, ctx.engagement, state, None, traits=DatasetTraits(), profile=None
    )
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


def with_d04_line(agent: FieldworkAgent, **line: int) -> None:
    """D04 的候选行 (Numbat 窗口内的分录行) 另加一行金额为 line 的行, 如 credit_cents=700000."""
    rules = agent.deps.rules
    assert isinstance(rules, FakeRuleEngine)
    row = {**NUMBAT_D04_ROWS[0], "id": "GJ-INT-ACC-1", "debit_cents": 0, "credit_cents": 0, **line}
    rules.results["CSH-02"] = lambda run: RuleResult(
        exceptions=[], rows={"D02": NUMBAT_D02_ROWS, "D03": NUMBAT_D03_ROWS, "D04": [*NUMBAT_D04_ROWS, row]}
    )


@pytest.mark.parametrize(
    ("accrued", "line", "expected"),
    [
        (700000, {}, 700000),  # 超过明显微小 (600000), 窗口内没有 700000 的行
        (-700000, {}, -700000),  # 客户欠银行: 金额带符号, 按绝对值比
        (600000, {}, None),  # 等于明显微小不算
        (700000, {"credit_cents": 700000}, None),  # 窗口内有贷方 700000 的行: 视为已计提
        (-700000, {"debit_cents": 700000}, None),  # 借方同样算
        (None, {}, None),  # 回函没提应计利息
    ],
)
async def test_d04_accrued_interest_not_booked(accrued: int | None, line: dict[str, int], expected: int | None) -> None:
    """R105 验收 2: BA-02 余额相符 (C-BA-02 v3 仍为 agreed); 回函应计利息绝对值超过明显微小且 D04 的行里没有等额的
    行时出 X-CSH-02-1-D04-1 (金额带符号, detail 记搜索窗口, 证据为应计利息所在页, 种类与可靠性取回函当前的文件登记:
    这里把 BA-02 的回函登记为副本, 与抽取时指针上的 external_original 不同), BA-02 进判断 (差额 0), 建
    T-J-CSH-02-1-BA-02-v1; 否则只有 BA-01 的 D01, BA-02 不判断."""
    agent, ctx, engine = setup([extracted("BA-01", 120840000, 0), extracted("BA-02", 40000000, 1, accrued)])
    copy = NUMBAT_REPLIES[1].model_copy(update={"reliability": Reliability.external_copy})
    financial = agent.deps.financial_store
    assert isinstance(financial, FakeFinancialStore)
    financial.document_refs = [NUMBAT_REPLIES[0], copy]  # 文件登记以存储为准, 编排状态里的一并换
    ctx.state["documents"] = [NUMBAT_REPLIES[0], copy]
    if line:
        with_d04_line(agent, **line)
    patch = await agent.run(ctx)
    assert patch["confirmations"][1].result is ConfirmationResult.agreed
    d04s = [x for x in patch["exceptions"] if x.rule_id == "D04"]
    if expected is None:
        assert d04s == [] and [jctx.subject_id for _, jctx, _ in engine.calls] == ["BA-01"]
        return
    [d04] = d04s
    assert (d04.id, d04.severity, d04.source_ids, d04.amount_cents) == (
        "X-CSH-02-1-D04-1",
        "medium",
        ["BA-02"],
        expected,
    )
    assert d04.detail == {
        "accrued_interest_cents": expected,
        "search_from": "2026-06-25",
        "search_to": "2026-06-30",
        "matched_line_ids": [],
    }
    [pointer] = d04.evidence
    assert (pointer.source_id, pointer.locator, pointer.page) == (NUMBAT_REPLIES[1].id, "accrued_interest_cents", 1)
    assert (pointer.type, pointer.reliability) == (EvidenceType.external, Reliability.external_copy)
    calls = {jctx.subject_id: jctx for _, jctx, _ in engine.calls}
    assert sorted(calls) == ["BA-01", "BA-02"]
    assert calls["BA-02"].data["difference_cents"] == 0
    assert [x.id for x in calls["BA-02"].rule_findings] == ["X-CSH-02-1-D04-1"]
    assert "T-J-CSH-02-1-BA-02-v1" in [t.id for t in patch["review_tasks"]]


async def test_d04_numbered_by_account_and_judged_once_with_d01() -> None:
    """R105 验收 3: 两个账户都有 D04 时按账户编号 (BA-01 为 D04-1, BA-02 为 D04-2); BA-01 同时有 D01, 只判断一次,
    规则例外依次为 CSH-05 的 B01, D01, D04."""
    confirmations = [extracted("BA-02", 40000000, 1, 700000), extracted("BA-01", 120840000, 0, 800000)]
    agent, ctx, engine = setup(confirmations)
    patch = await agent.run(ctx)
    assert [(x.id, x.source_ids) for x in patch["exceptions"] if x.rule_id in ("D01", "D04")] == [
        ("X-CSH-02-1-D01-1", ["BA-01"]),
        ("X-CSH-02-1-D04-1", ["BA-01"]),
        ("X-CSH-02-1-D04-2", ["BA-02"]),
    ]
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == ["BA-01", "BA-02"]
    findings = {jctx.subject_id: [x.id for x in jctx.rule_findings] for _, jctx, _ in engine.calls}
    assert findings == {
        "BA-01": ["X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1", "X-CSH-02-1-D04-1"],
        "BA-02": ["X-CSH-02-1-D04-2"],
    }


def accrued_draft(amount: int) -> str:
    """BA-02 只有 D04 时的一份判断输出 (合成): 认定应计利息未入账为错报, 引回函 accrued_interest_cents 与 ASA450-P05."""
    reply = NUMBAT_REPLIES[1]
    field = {
        "type": "external",
        "source_type": "document",
        "source_id": reply.id,
        "locator": "accrued_interest_cents",
        "page": 1,
        "reliability": reply.reliability.value,
    }
    standard = {"type": "standard", "source_type": "standard", "source_id": "ASA450-P05", "locator": "para 5"}
    draft = {
        "conclusion": "The bank confirms AUD 7,000.00 of interest earned to 30 June 2026 that is not recorded.",
        "reasoning_chain": [
            {"kind": "fact", "text": "The reply confirms accrued interest of 700000 cents.", "evidence": [field]},
            {
                "kind": "standard",
                "text": "Misstatements other than clearly trivial are accumulated.",
                "evidence": [standard],
            },
            {
                "kind": "inference",
                "text": "Interest income is understated by the accrued interest.",
                "from_steps": [0, 1],
            },
        ],
        "evidence": [field, standard],
        "counter_argument": "A GL entry outside the search window recording this interest would show it was accrued.",
        "proposed_action": "misstatement",
        "misstatement_draft": {
            "type": "factual",
            "account": "Interest income",
            "assertion": "Completeness",
            "amount_cents": amount,
            "direction": "understated",
        },
        "self_confidence": 0.8,
    }
    return json.dumps(draft)


@pytest.mark.parametrize(("amount", "level"), [(700000, None), (650000, "reference")])
async def test_accrued_interest_only_prompt_and_draft_amount(amount: int, level: str | None) -> None:
    """R106 验收 2: BA-01 余额相符, BA-02 余额相符而只有 D04 (应计利息 700000). 提示词渲染一次, 按节查口径: Task 写
    余额相符, 应计利息, 搜索区间 (闭区间的起止日, 不写天数), 明显微小的门槛 (超过它才建 D04) 与 D04 例外, 不写
    "differs from the book balance"; Criteria 第 4 条: 应计利息的定义 (截至期末已产生, 尚未记入账户), 全部科目的
    相同金额搜索已由规则做完, 有 D04 即未入账; proposed_action: close 须没有 D04, 应计利息的错报写法 (factual, 银行欠
    客户为 Interest income, 客户欠银行为 Interest expense, Completeness, understated, 金额为 D04 去掉符号), 同时有
    D01 与 D04 时的取舍 (差异没被调节项解释时草稿记余额差异, 结论另说应计利息未入账; 否则草稿记应计利息). 认定错报
    的输出引回函 accrued_interest_cents, 金额 700000 (按 Q12 绝对值等于归因例外的金额) 一次通过; 650000 引用级失败
    (不重试)."""
    books = [
        TrialBalanceRow(period="current", account_code="1010", balance_cents=120840000),
        TrialBalanceRow(period="current", account_code="1020", balance_cents=40000000),
    ]
    confirmations = [extracted("BA-01", 120840000, 0), extracted("BA-02", 40000000, 1, 700000)]
    agent, ctx, trail = setup(confirmations, books=books, real_engine=True)
    llm = ScriptedLLM({"BA-02": [accrued_draft(amount)]})
    agent.deps.judgments.llm = llm  # type: ignore[attr-defined]
    patch = await agent.run(ctx)
    [request] = [r for r in llm.requests if r.procedure_id == "CSH-02"]
    prompt = request.messages[0].content
    task = section(prompt, "Task")
    assert "agrees with the book balance" in task and "differs from the book balance" not in task
    [d04] = [part for part in task.split("\n\n") if "X-CSH-02-1-D04-1" in part]
    for text in ("700000 cents", "2026-06-25", "2026-06-30", "inclusive", "600000 cents", "clearly trivial", "exceeds"):
        assert text in d04, text
    assert "days" not in d04  # 区间写起止日, 不写天数 (闭区间含 6 个日历日)
    criteria = section(prompt, "Criteria")
    [accrual] = [line for line in criteria.splitlines() if line.startswith("4.")]
    for text in ("accrued_interest_cents", "not yet credited", "all accounts", "same amount", "not among the Inputs"):
        assert text in accrual, text
    assert "has not been accrued" in accrual
    actions = section(prompt, "proposed_action").splitlines()
    [close] = [line for line in actions if line.startswith("- `close`")]
    assert re.search(r"\bno\b[^.]*D04", close)  # 没有 D04 才能 close
    [accrued] = [line for line in actions if line.startswith("- `misstatement`") and "D04" in line]
    income, expense = accrued.split('"Interest expense"')
    assert '"Interest income"' in income and "bank owes the client" in income and "client owes the bank" in expense
    for text in ("`type` factual", "Completeness", "understated", "without its sign"):
        assert text in accrued, text
    [both] = [line for line in actions if "D01" in line and "D04" in line]
    unexplained, explained = both.split("otherwise")
    assert "do not explain" in unexplained and "balance difference" in unexplained
    assert "accrued interest has not been recorded" in unexplained and "accrued interest" in explained
    [call] = [e for e in trail.entries if e.kind is TrailKind.llm_call and e.procedure_id == "CSH-02"]
    assert (call.payload["fixture_key"], call.payload["attempt"], call.payload["level_failed"]) == ("BA-02", 1, level)
    if level is None:
        [judgment] = patch["judgments"]
        assert judgment.misstatement_draft is not None and judgment.misstatement_draft.amount_cents == 700000
        assert judgment.exception_ids == ["X-CSH-02-1-D04-1"]
    else:
        assert patch["judgments"] == []


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
    assert [r["id"] for r in data["period_end_entries"]] == [  # D03 中 BA-01 的行, 不带 BA-02 的
        "CR-SI-2026-0518-1",
        "CP-AP-2026-06-B-2",
        "CP-FRT-2026-06-2",
        "CP-OPEX-2026-06-2",
        "CR-SI-2026-0547-1",
    ]
    assert [x.id for x in jctx.rule_findings] == ["X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1"]
    assert jctx.task["params"] == {  # Numbat 没有数据集声明, 作废对的格式与模板为空
        "period_end": "2026-06-30",
        "subsequent_days": 10,
        "lookback_days": 5,
        "clearly_trivial_cents": 600000,
        "reversal_reference_pattern": None,
        "reversal_entry_no_template": None,
    }
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


@pytest.mark.parametrize(
    ("confirmations", "fresh"),
    [
        (NUMBAT_CONFIRMATIONS, "X-CSH-02-1-D01-1"),  # BA-01 余额不符, 没有 CSH-05 的例外
        ([extracted("BA-01", 125840000, 0), extracted("BA-02", 40000000, 1, 700000)], "X-CSH-02-1-D04-1"),  # 只有 D04
    ],
)
async def test_failure_marks_fresh_exception_on_its_first_version(
    confirmations: list[Confirmation], fresh: str
) -> None:
    """需求 R322 验收 1: 没有前置例外时, 判断失败的账户的第一条规则例外就是本次刚建的 D01 或 D04. 片段只给它的 v1
    (置 unattributed, 不另出 v2), 任务以 v1 为目标; 经合并校验 (run_agent), 片段的例外与任务经 save_outputs 写进
    FakeStore 不报 VersionConflict, 存的就是这一版 (函证的前两版不在这个存储里, 不写)."""
    outcomes = {account: failed("unattributed") for account in ("BA-01", "BA-02")}
    agent, ctx, _ = setup(confirmations, outcomes=outcomes, exceptions=[])
    patch, writer = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A6, patch["procedure_runs"][0].failure_reason
    assert [(x.id, x.version, x.disposition) for x in patch["exceptions"]] == [
        (fresh, 1, ExceptionDisposition.unattributed)
    ]
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, task.target_id, task.target_version) == (
        f"T-{fresh}-v1",
        TaskKind.unattributed,
        fresh,
        1,
    )
    store = agent.deps.output_store
    async with store.transaction(EID):
        await save_outputs(
            store, EID, {"exceptions": patch["exceptions"], "review_tasks": patch["review_tasks"]}, writer
        )
    stored = await store.get(EID, AuditException, fresh)
    assert stored is not None and (stored.version, stored.disposition) == (1, ExceptionDisposition.unattributed)


async def test_auto_close_attributes_fresh_exception_on_its_first_version() -> None:
    """需求 R322 (judgments_patch 的 fresh 同样用于自动关闭): 模板去掉 always_review, BA-01 判断 L3 accept 置信度 0.9
    时自动关闭, 它解释的本次 D01 attributed 写在 v1 上 (attributed_by 为该判断), 不建任务."""
    outcomes = {"BA-01": passed(autonomy_level=AutonomyLevel.L3)}
    agent, ctx, _ = setup(outcomes=outcomes, exceptions=[])
    template = ctx.template.model_copy(update={"always_review": False})
    ctx = RunContext(ctx.engagement, ctx.procedure, template, ctx.run, ctx.state)
    patch = await agent.run(ctx)
    assert [(j.id, j.review_status) for j in patch["judgments"]] == [("J-CSH-02-1-BA-01", ReviewStatus.confirmed)]
    assert [(x.id, x.version, x.disposition, x.attributed_by) for x in patch["exceptions"]] == [
        ("X-CSH-02-1-D01-1", 1, ExceptionDisposition.attributed, "J-CSH-02-1-BA-01")
    ]
    assert patch["review_tasks"] == []


XERO_D04 = "X-CSH-02-1-D04-1"  # Xero BA-091 回函应计利息未入账 (Xero演示数据集设计)


async def assert_d04_unattributed(ran: Ran) -> None:
    """Xero BA-091 判断失败后: 本次的 X-CSH-02-1-D04-1 在状态与存储里都只有 v1 (unattributed), 以它为目标的
    T-X-CSH-02-1-D04-1-v1 待决 (带对象 BA-091); 存储里没有 v2."""
    state, store = ran.state, ran.deps.output_store
    eid = state["engagement_id"]
    [d04] = [x for x in state["exceptions"] if x.id == XERO_D04]
    assert (d04.version, d04.disposition, d04.source_ids) == (1, ExceptionDisposition.unattributed, ["BA-091"])
    [task] = [t for t in state["review_tasks"] if t.target_id == XERO_D04]
    assert (task.id, task.kind, task.status, task.subject_id) == (
        f"T-{XERO_D04}-v1",
        TaskKind.unattributed,
        TaskStatus.pending,
        "BA-091",
    )
    stored = await store.get(eid, AuditException, XERO_D04)
    assert stored is not None and (stored.version, stored.disposition) == (1, ExceptionDisposition.unattributed)


def flaky_ba091(case: XeroCase) -> FlakyLLM:
    """副本的运行副本, BA-091 的 CSH-02 判断前 3 次 (MAX_ATTEMPTS) 都不是 JSON: 该对象判断失败, 转 unattributed."""
    return FlakyLLM(LayeredMockLLMClient([case.supplements / "llm"]), {("CSH-02", "BA-091"): 3})


@pytest.mark.external
async def test_xero_failure_with_own_d04(xero_case: Any) -> None:
    """需求 R322 验收 1 (R250 的探针): Xero T0 副本 (假存储) 上 BA-091 判断失败, 第一条规则例外为本次的 D04, 写入
    不报 VersionConflict (见 assert_d04_unattributed). 连库同一情形见 test_xero_injections_db."""
    case = xero_case("T0")
    await assert_d04_unattributed(await run_programs(await offline_deps(case, llm=flaky_ba091(case)), ["CSH-02"]))


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


def section(prompt: str, heading: str) -> str:
    """提示词中 "## heading" 一节 (到下一个 "## " 为止)."""
    return prompt.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]


async def test_ba01_fixture_passes_real_engine() -> None:
    """BA-01 (余额差异, 没有应计利息) 的固定输出经真实判断引擎一次通过 (R48 验收 5); 提示词 (R106 验收 1) 的 Task 按
    余额差异写, 没有应计利息一段; Inputs 里期末前分录的小标题写闭区间的起止; Standards 列出 ASA505-P14 与
    ASA450-P05."""
    agent, ctx, trail = setup(real_engine=True)
    patch = await agent.run(ctx)
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    prompt = calls[0].payload["messages"][0]["content"]
    task = section(prompt, "Task")
    assert "differs from the book balance" in task and "Difference, confirmed minus book: -5000000 cents" in task
    assert "accrued interest" not in task
    inputs = section(prompt, "Inputs").splitlines()
    # D03 的窗口为闭区间 [期末 - lookback_days, 期末], 写起止与 "both dates inclusive", 不写 "N days up to"
    [entries] = [line for line in inputs if line.startswith("GL entries on the bank account")]
    assert "5 days before 2026-06-30 to 2026-06-30" in entries and "both dates inclusive" in entries
    standards = [line for line in inputs if line.startswith("- ASA")]
    assert [line.split(" ", 2)[1] for line in standards] == ["ASA505-P14", "ASA450-P05"]
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
