"""A3 DOC-06: 读回函文本层, 抽取回函余额与截至日写入函证; 单账户失败交人 (合成数据集设计第 6.4-6.6 节)."""

import json
import re
from datetime import date
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.evidence import legacy_key
from backend.agents.ingest import IngestAgent
from backend.contracts.data import DataInput
from backend.contracts.models import (
    Confirmation,
    Decision,
    Phase,
    ReceiptChannel,
    RunStatus,
    TargetType,
    TaskKind,
    TrailKind,
)
from backend.core.config import get_settings
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_RECORDS,
    NUMBAT_REPLIES,
    NUMBAT_REPLY_PAGES,
    FakeDataStore,
    FakeFinancialStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    ScriptedLLM,
    api_client,
    legacy_sends,
    make_ctx,
    make_deps,
    make_runner,
    numbat_confirmation_key,
)

EID = ENGAGEMENT.id
FIXTURES = get_settings().LLM_FIXTURES_ROOT / "DOC-06"
ACCOUNTS = ["BA-01", "BA-02"]


def sent(account: str, reply: str | None, channel: ReceiptChannel | None = ReceiptChannel.direct_email) -> Confirmation:
    """CSH-04 建的函证第 1 版."""
    return Confirmation(
        id=f"C-{account}",
        bank_account_id=account,
        confirming_party="bank",
        idempotency_key=numbat_confirmation_key(EID, account),
        sent_at=NOW,
        reply_document_id=reply,
        receipt_channel=channel,
    )


CONFIRMATIONS = [sent("BA-01", NUMBAT_REPLIES[0].id), sent("BA-02", NUMBAT_REPLIES[1].id)]


def raw(account: str, **changes: Any) -> str:
    return json.dumps(json.loads((FIXTURES / f"{account}.json").read_text()) | changes)


def setup(
    llm: Any = None,
    rerun: list[str] | None = None,
    confirmations: list[Confirmation] | None = None,
    documents: list[Any] | None = None,
) -> tuple[IngestAgent, RunContext, FakeTrail]:
    """A3 与 DOC-06 第 1 次执行: 状态有 CSH-04 建的两份函证与回函文件; llm 缺省为 mock 模式 (读 DOC-06 fixture)."""
    trail = FakeTrail()
    financial = FakeFinancialStore(list(NUMBAT_REPLIES), NUMBAT_RECORDS)
    deps = make_deps(FakeRegistry(), trail, financial_store=financial, llm=llm)
    ctx = make_ctx(deps, "DOC-06")
    procedure = ctx.procedure.model_copy(update={"phase": Phase.P2_Executing, "rerun_subject_ids": rerun or []})
    state = ctx.state | {
        "confirmations": CONFIRMATIONS if confirmations is None else confirmations,
        "documents": list(NUMBAT_REPLIES) if documents is None else documents,
    }
    return IngestAgent(deps), RunContext(ctx.engagement, procedure, ctx.template, ctx.run, state), trail  # type: ignore[arg-type]


def scripted(**replies: list[str]) -> ScriptedLLM:
    base = {a: [raw(a)] for a in ACCOUNTS}
    return ScriptedLLM(base | {k.replace("_", "-"): v for k, v in replies.items()})


# ── 对象与请求 ─────────────────────────────────────────


async def test_subjects_and_requests() -> None:
    no_reply = sent("BA-03", None)  # 还没回函的不抽
    llm = scripted()
    agent, ctx, _ = setup(llm, confirmations=[*CONFIRMATIONS, no_reply])
    await agent.run(ctx)
    assert sorted(r.fixture_key for r in llm.requests) == ACCOUNTS
    for req in llm.requests:
        assert (req.procedure_id, req.run_id) == ("DOC-06", "RUN-DOC-06-1")
        assert '"title": "ConfirmationExtraction"' in req.system
        prompt = req.messages[0].content
        doc = NUMBAT_REPLIES[ACCOUNTS.index(req.fixture_key)]
        assert f"document ID {doc.id}, 2 pages." in prompt
        assert f'<page number="1">\n{NUMBAT_REPLY_PAGES[doc.id][0]}\n</page>' in prompt


async def test_legacy_key_confirmations_follow_bank_account_changes() -> None:
    """R274-16: 识别信息进键之前发的函 (旧写法的键): 发函之后银行账户表没变过, 仍是当前的, DOC-06 照常抽取; 变过了,
    旧键不再成立, 不再处理 (不拿旧回函与当前账户比, 等 CSH-04 按新键重发)."""
    legacy = [c.model_copy(update={"idempotency_key": legacy_key(EID, c.bank_account_id)}) for c in CONFIRMATIONS]
    for changed, expected in ((False, ACCOUNTS), (True, [])):
        llm = scripted()
        agent, ctx, _ = setup(llm, confirmations=legacy)
        await legacy_sends(agent.deps, EID, ACCOUNTS)
        data = agent.deps.data
        assert isinstance(data, FakeDataStore)
        data.record(DataInput.trial_balance)  # 别的数据变了不算
        if changed:
            data.record(DataInput.bank_accounts)
        patch = await agent.run(ctx)
        assert sorted(r.fixture_key for r in llm.requests) == expected, changed
        assert [c.bank_account_id for c in patch["confirmations"]] == expected


async def test_rerun_only_listed_account() -> None:
    llm = scripted()
    agent, ctx, _ = setup(llm, rerun=["BA-01"])
    patch = await agent.run(ctx)
    assert [r.fixture_key for r in llm.requests] == ["BA-01"]
    assert [c.id for c in patch["confirmations"]] == ["C-BA-01"]


# ── mock 输出 ─────────────────────────────────────────


async def test_mock_extraction_updates_confirmations() -> None:
    agent, ctx, trail = setup()
    patch = await agent.run(ctx)
    c1, c2 = patch["confirmations"]
    assert (c1.id, c1.version, c1.confirmed_balance_cents, c1.balance_date) == (
        "C-BA-01",
        2,
        120840000,
        date(2026, 6, 30),
    )
    assert (c2.id, c2.version, c2.confirmed_balance_cents) == ("C-BA-02", 2, 40000000)
    pages = {p.locator: p.page for p in c1.evidence}
    assert pages == {
        "confirming_party": 1,
        "account_no": 1,
        "confirmed_balance_cents": 1,
        "balance_date": 1,
        "receipt_channel": 2,
    }
    assert c1.receipt_channel is ReceiptChannel.direct_email and c1.idempotency_key == CONFIRMATIONS[0].idempotency_key
    assert [(d.id, d.status) for d in patch["documents"]] == [(d.id, "extracted") for d in NUMBAT_REPLIES]
    assert patch["review_tasks"] == [] and patch["procedure_runs"][0].status is RunStatus.succeeded
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert len(calls) == 2 and {e.procedure_id for e in calls} == {"DOC-06"}
    assert all(e.payload["notes"] == [] for e in calls)
    # 需求 R104: fixture 没写应计利息的键, 按空处理, 一次通过 (不算结构失败); 指针仍 5 条
    assert all(e.payload["level_failed"] is None for e in calls)
    assert (c1.accrued_interest_cents, c2.accrued_interest_cents) == (None, None)
    assert (len(c1.evidence), len(c2.evidence)) == (5, 5)


# ── 失败与提示 ─────────────────────────────────────────


def assert_ba01_handed_to_human(agent: IngestAgent, patch: dict[str, Any]) -> None:
    [task] = patch["review_tasks"]
    assert task.id == "T-RUN-DOC-06-1-BA-01-v1" and task.kind is TaskKind.failed
    assert (task.target_type, task.subject_id) == (TargetType.ProcedureRun, "BA-01")
    assert agent.deps.queue.allowed(task) == frozenset({Decision.rerun})
    assert {d.id: d.status for d in patch["documents"]} == {
        NUMBAT_REPLIES[0].id: "needs_manual",
        NUMBAT_REPLIES[1].id: "extracted",
    }
    assert [c.id for c in patch["confirmations"]] == ["C-BA-02"]  # BA-01 的函证不更新
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


async def test_account_number_mismatch_fails_that_account() -> None:
    agent, ctx, trail = setup(scripted(BA_01=[raw("BA-01", account_no="99999999"), raw("BA-01")]))
    patch = await agent.run(ctx)
    assert_ba01_handed_to_human(agent, patch)
    [entry] = [e for e in trail.entries if e.payload.get("fixture_key") == "BA-01"]
    assert entry.payload["level_failed"] == "semantic"  # 语义失败不重试
    assert "99999999" in entry.payload["errors"][0] and "12345601" in entry.payload["errors"][0]


async def test_receipt_channel_mismatch_only_noted() -> None:
    agent, ctx, trail = setup(scripted(BA_01=[raw("BA-01", receipt_channel="portal_via_client")]))
    patch = await agent.run(ctx)
    assert patch["review_tasks"] == []
    c1 = patch["confirmations"][0]
    assert c1.receipt_channel is ReceiptChannel.direct_email  # 以回执为准
    [entry] = [e for e in trail.entries if e.payload.get("fixture_key") == "BA-01"]
    assert entry.payload["level_failed"] is None
    assert entry.payload["notes"] == ["回函写明的渠道 portal_via_client 与回执记录的 direct_email 不符, 以回执为准"]


async def test_retries_exhausted_fails_that_account() -> None:
    agent, ctx, _ = setup(scripted(BA_01=["{}", "{}", "{}"]))
    assert_ba01_handed_to_human(agent, await agent.run(ctx))


async def test_pointer_page_beyond_document_fails_that_account() -> None:
    bad = json.loads(raw("BA-01"))
    bad["evidence"][0]["page"] = 3
    agent, ctx, _ = setup(scripted(BA_01=[json.dumps(bad)]))
    assert_ba01_handed_to_human(agent, await agent.run(ctx))


# ── 回函应计利息 (需求 R104) ─────────────────────────────────────────


def with_accrued(account: str, cents: int | None, page: int | None = 1) -> str:
    """该账户 fixture 的输出加上应计利息 (测试内联, fixture 不改); page 为空时不加它的指针."""
    data = json.loads(raw(account))
    data["accrued_interest_cents"] = cents
    if page is not None:
        pointer = {**data["evidence"][0], "locator": "accrued_interest_cents", "page": page}
        data["evidence"].append(pointer)
    return json.dumps(data)


@pytest.mark.parametrize("cents", [0, 4853])  # 0: 回函写 nil
async def test_accrued_interest_is_written_to_the_confirmation(cents: int) -> None:
    """抽出的应计利息 (带第 1 页的指针) 写入函证; 回函余额与截至日同 mock, 另一账户仍为空."""
    agent, ctx, _ = setup(scripted(BA_01=[with_accrued("BA-01", cents)]))
    c1, c2 = (await agent.run(ctx))["confirmations"]
    assert (c1.accrued_interest_cents, c1.confirmed_balance_cents, c1.balance_date) == (
        cents,
        120840000,
        date(2026, 6, 30),
    )
    assert (c2.accrued_interest_cents, c2.confirmed_balance_cents) == (None, 40000000)
    # 指针随之写进函证 (CSH-02 的 D04 以它为证据, 需求 R105): 6 条, 应计利息一条在第 1 页
    assert len(c1.evidence) == 6
    assert [(p.locator, p.page) for p in c1.evidence if p.locator == "accrued_interest_cents"] == [
        ("accrued_interest_cents", 1)
    ]


@pytest.mark.parametrize("page", [None, 3])  # 没有该指针; 指针页码超出页数 (2 页)
async def test_accrued_interest_without_a_valid_pointer_fails_that_account(page: int | None) -> None:
    """应计利息 4853 却没有 locator 为 accrued_interest_cents 的有效指针: 引用失败不重试 (只调 1 次), BA-01 交人,
    函证不更新, 另一账户照常."""
    llm = scripted(BA_01=[with_accrued("BA-01", 4853, page), raw("BA-01")])
    agent, ctx, _ = setup(llm)
    assert_ba01_handed_to_human(agent, await agent.run(ctx))
    assert [r.fixture_key for r in llm.requests].count("BA-01") == 1


# 应计利息口径的关键词: 每条在应计利息一节里须有一个分句同时含这几个词 (不分大小写), 不比逐字原文
ACCRUED_RULES = {
    "银行欠客户为正": ("positive", "bank owes"),
    "客户欠银行为负": ("negative", "customer owes"),
    "nil 为 0": ("0", "nil"),
    "没提为 null": ("null", "not mention"),
    "不取已入账的利息": ("not", "already credited"),
    "不取以后应付的整期利息": ("not", "later date"),
}


async def test_prompt_states_how_to_read_accrued_interest() -> None:
    """渲染一次 DOC-06 提示词, 截出应计利息一节 (到下一个字段为止): 按分句查各条口径的关键词; evidence 一条另要求
    给 accrued_interest_cents 的指针."""
    llm = scripted()
    agent, ctx, _ = setup(llm, confirmations=CONFIRMATIONS[:1])
    await agent.run(ctx)
    [request] = llm.requests
    lines = request.messages[0].content.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("- `accrued_interest_cents`"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("- `"))
    clauses = [c.casefold() for line in lines[start:end] for c in re.split(r"[.;]\s", line)]
    missing = [rule for rule, words in ACCRUED_RULES.items() if not any(all(w in c for w in words) for c in clauses)]
    assert missing == []
    assert lines[end].startswith("- `evidence`") and "accrued_interest_cents" in lines[end]


async def test_confirmation_list_says_whether_the_reply_was_extracted() -> None:
    """GET /confirmations 另带 reply_extracted (后端判定, 证据页据此显示应计利息 "未提及" 或 "—"): DOC-06 抽取过的
    (evidence 指向当前回函) 为真; DOC-06 失败后审计师手填了余额 (有余额而没有指针) 与还没回函的为假."""
    agent, ctx, _ = setup()
    extracted = (await agent.run(ctx))["confirmations"][0]  # C-BA-01 第 2 版, 回函没提应计利息
    manual = CONFIRMATIONS[1].model_copy(update={"version": 2, "confirmed_balance_cents": 40000000})
    pending = sent("BA-03", None)
    runner, _ = make_runner(
        output_store=FakeOutputStore([CONFIRMATIONS[0], extracted, CONFIRMATIONS[1], manual, pending])
    )
    async with api_client(runner) as client:
        rows = (await client.get(f"/api/v1/engagements/{EID}/confirmations")).json()
    found = {r["id"]: (r["version"], r["accrued_interest_cents"], r["reply_extracted"]) for r in rows}
    assert found == {"C-BA-01": (2, None, True), "C-BA-02": (2, None, False), "C-BA-03": (1, None, False)}


async def test_reply_missing_from_state_raises() -> None:
    agent, ctx, _ = setup(scripted(), documents=NUMBAT_REPLIES[1:])
    with pytest.raises(LookupError, match="C-BA-01"):
        await agent.run(ctx)
