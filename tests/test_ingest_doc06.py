"""A3 DOC-06: 读回函文本层, 抽取回函余额与截至日写入函证; 单账户失败交人 (合成数据集设计第 6.4-6.6 节)."""

import json
from datetime import date
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.ingest import IngestAgent
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
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    ScriptedLLM,
    make_ctx,
    make_deps,
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
        idempotency_key=f"{EID}:CSH-04:{account}",
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


async def test_reply_missing_from_state_raises() -> None:
    agent, ctx, _ = setup(scripted(), documents=NUMBAT_REPLIES[1:])
    with pytest.raises(LookupError, match="C-BA-01"):
        await agent.run(ctx)
