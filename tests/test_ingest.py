"""A3 DOC-04: 对每份合同调大模型抽取要素与条款, 经 save_contract 写入; 单份失败交人 (合成数据集设计第 6.6 节)."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.ingest import IngestAgent
from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import (
    Decision,
    GateVerdict,
    ProcedureRun,
    ProcedureStatus,
    Reliability,
    ReviewRecord,
    ReviewTask,
    RunStatus,
    TargetType,
    TaskKind,
    TaskStatus,
    TermType,
)
from backend.core.config import get_settings
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    ScriptedLLM,
    make_ctx,
    make_deps,
    make_runner,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision

EID = ENGAGEMENT.id
FIXTURES = get_settings().LLM_FIXTURES_ROOT
CONTRACT_IDS = ["CON-001", "CON-002", "CON-003"]


def raw(cid: str, **changes: Any) -> str:
    """DOC-04 fixture 的原文; changes 覆盖顶层字段."""
    data = json.loads((FIXTURES / "DOC-04" / f"{cid}.json").read_text(encoding="utf-8"))
    return json.dumps(data | changes)


def setup(
    replies: dict[str, list[str]] | None = None, rerun: list[str] | None = None, llm: Any = None
) -> tuple[IngestAgent, RunContext, FakeFinancialStore, Any]:
    """A3 与 DOC-04 第 1 次执行的上下文. replies 缺省为三份合同各一次合法输出; rerun 为 rerun_subject_ids."""
    if llm is None:
        llm = ScriptedLLM(replies if replies is not None else {c: [raw(c)] for c in CONTRACT_IDS})
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS))
    deps = make_deps(FakeRegistry(), FakeTrail(), financial_store=financial, llm=llm)
    ctx = make_ctx(deps, "DOC-04")
    if rerun:
        procedure = ctx.procedure.model_copy(update={"rerun_subject_ids": rerun})
        ctx = RunContext(ctx.engagement, procedure, ctx.template, ctx.run, ctx.state)
    return IngestAgent(deps), ctx, financial, llm


# ── 成功 ─────────────────────────────────────────


async def test_three_contracts_saved() -> None:
    agent, ctx, financial, _ = setup()
    patch = await agent.run(ctx)
    assert sorted(financial.save_calls) == CONTRACT_IDS
    con001 = financial.saved_contracts["CON-001"]
    expected = ContractExtraction.model_validate_json(raw("CON-001"))
    assert (con001.document_id, con001.extracted_by_run) == (NUMBAT_CONTRACTS[0].id, "RUN-DOC-04-1")
    assert (con001.counterparty, con001.counterparty_abn, con001.amount_cents, con001.sign_date) == (
        expected.counterparty,
        expected.counterparty_abn,
        expected.amount_cents,
        expected.sign_date,
    )
    # 指针与模型输出一致, 可靠性按文件登记补上 (校验器按来源改写)
    assert [p.model_copy(update={"reliability": None}) for p in con001.evidence] == expected.evidence
    assert {p.reliability for p in con001.evidence} == {Reliability.external_copy}
    assert [t.id for t in con001.terms] == ["CON-001-T1", "CON-001-T2", "CON-001-T3"]
    assert [(t.term_type, t.text, t.page) for t in con001.terms] == [
        (t.term_type, t.text, t.page) for t in expected.terms
    ]
    assert set(patch) == {"documents", "review_tasks", "procedure_runs"}


async def test_terms_numbered_by_page_then_output_order() -> None:
    terms = [
        {"term_type": "termination", "text": "11.1 ...", "page": 3},
        {"term_type": "control_transfer", "text": "5.1 ...", "page": 2},
        {"term_type": "payment", "text": "4.1 ...", "page": 2},
    ]
    replies = {c: [raw(c, terms=terms) if c == "CON-001" else raw(c)] for c in CONTRACT_IDS}
    agent, ctx, financial, _ = setup(replies)
    await agent.run(ctx)
    assert [(t.id, t.term_type) for t in financial.saved_contracts["CON-001"].terms] == [
        ("CON-001-T1", TermType.control_transfer),
        ("CON-001-T2", TermType.payment),
        ("CON-001-T3", TermType.termination),
    ]


async def test_patch_documents_extracted_and_run_succeeded() -> None:
    agent, ctx, _, _ = setup()
    patch = await agent.run(ctx)
    assert [(d.id, d.status) for d in patch["documents"]] == [(d.id, "extracted") for d in NUMBAT_CONTRACTS]
    assert patch["review_tasks"] == []
    [run] = patch["procedure_runs"]
    assert run.id == "RUN-DOC-04-1" and run.status is RunStatus.succeeded and run.ended_at is not None


async def test_llm_requests() -> None:
    agent, ctx, _, llm = setup()
    await agent.run(ctx)
    assert sorted(r.fixture_key for r in llm.requests) == CONTRACT_IDS
    for req in llm.requests:
        assert (req.procedure_id, req.run_id) == ("DOC-04", "RUN-DOC-04-1")
        prompt = req.messages[0].content
        assert f"The audited entity is {NUMBAT_PROFILE.legal_name};" in prompt
        assert f'<page number="2">\n{req.fixture_key}.pdf page 2\n</page>' in prompt


# ── 失败 ─────────────────────────────────────────


def assert_con002_handed_to_human(agent: IngestAgent, patch: dict[str, Any], financial: FakeFinancialStore) -> None:
    assert "CON-002" not in financial.save_calls and sorted(financial.save_calls) == ["CON-001", "CON-003"]
    status = {d.id: d.status for d in patch["documents"]}
    assert status == {
        NUMBAT_CONTRACTS[0].id: "extracted",
        NUMBAT_CONTRACTS[1].id: "needs_manual",
        NUMBAT_CONTRACTS[2].id: "extracted",
    }
    [task] = patch["review_tasks"]
    assert task.id == "T-RUN-DOC-04-1-CON-002-v1" and task.kind is TaskKind.failed
    assert (task.target_type, task.target_id, task.target_version) == (TargetType.ProcedureRun, "RUN-DOC-04-1", 1)
    assert (task.subject_id, task.run_id) == ("CON-002", "RUN-DOC-04-1")
    assert agent.deps.queue.allowed(task) == frozenset({Decision.rerun})
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


async def test_structure_failures_hand_contract_to_human() -> None:
    replies = {c: [raw(c)] for c in CONTRACT_IDS} | {"CON-002": ["{}", "{}", "{}"]}
    agent, ctx, financial, llm = setup(replies)
    patch = await agent.run(ctx)
    assert [r.fixture_key for r in llm.requests].count("CON-002") == 3
    assert_con002_handed_to_human(agent, patch, financial)


@pytest.mark.parametrize("change", [{"page": 5}, {"source_id": NUMBAT_CONTRACTS[0].id}])
async def test_reference_failure_not_retried(change: dict[str, Any]) -> None:
    bad = json.loads(raw("CON-002"))
    bad["evidence"][0] |= change
    replies = {c: [raw(c)] for c in CONTRACT_IDS} | {"CON-002": [json.dumps(bad), raw("CON-002")]}
    agent, ctx, financial, llm = setup(replies)
    patch = await agent.run(ctx)
    assert [r.fixture_key for r in llm.requests].count("CON-002") == 1
    assert_con002_handed_to_human(agent, patch, financial)


async def test_rerun_only_listed_contract() -> None:
    agent, ctx, financial, llm = setup({"CON-002": [raw("CON-002")]}, rerun=["CON-002"])
    patch = await agent.run(ctx)
    assert [r.fixture_key for r in llm.requests] == ["CON-002"] and financial.save_calls == ["CON-002"]
    assert [d.id for d in patch["documents"]] == [NUMBAT_CONTRACTS[1].id]


# ── mock 客户端与节点 ─────────────────────────────────────────


async def test_mock_client_reads_r36_fixtures() -> None:
    agent, ctx, financial, _ = setup(llm=make_deps(FakeRegistry(), FakeTrail()).llm)
    patch = await agent.run(ctx)
    contracts = financial.saved_contracts
    assert sorted(contracts) == CONTRACT_IDS and patch["review_tasks"] == []
    assert sum(len(c.terms) for c in contracts.values()) == 11
    assert [(c.counterparty, c.amount_cents, str(c.sign_date)) for _, c in sorted(contracts.items())] == [
        ("Bilby Robotics Pty Ltd", 14200000, "2026-05-20"),
        ("Quoll Automation Pty Ltd", 9500000, "2026-05-25"),
        ("Galah Retail Electronics Pty Ltd", None, "2025-06-16"),
    ]
    assert [t.id for t in contracts["CON-003"].terms] == [f"CON-003-T{n}" for n in range(1, 6)]


async def test_real_a3_in_p1_ingest() -> None:
    runner, store = make_runner(
        registry=FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC}),
        agents=lambda deps: {"DOC-04": IngestAgent(deps)},
    )
    await runner.start(EID)
    await wait_idle(runner, EID)
    await Started(runner, store, runner.deps.engagement_store).submit.submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    assert {d.status for d in snapshot.values["documents"]} == {"extracted"}
    doc04 = next(p for p in snapshot.values["procedure_pack"] if p.id == "DOC-04")
    assert doc04.status is ProcedureStatus.completed
    financial = runner.deps.financial_store
    assert isinstance(financial, FakeFinancialStore) and sorted(financial.saved_contracts) == CONTRACT_IDS


# ── 并发与重跑 ─────────────────────────────────────────


async def test_exception_waits_for_other_contracts() -> None:
    """某份合同抛异常时, Agent 等其余合同都结束再上抛, 不留仍在运行的抽取 (否则可能在重跑之后写入旧结果)."""
    release = asyncio.Event()
    raised = asyncio.Event()
    agent, ctx, financial, _ = setup()
    original = agent.deps.evidence_store.pages

    async def pages(eid: str, document_id: str) -> list[str]:
        if document_id == NUMBAT_CONTRACTS[0].id:
            await release.wait()
        elif document_id == NUMBAT_CONTRACTS[1].id:
            raised.set()
            raise LookupError("CON-002 读不到")
        return await original(eid, document_id)

    agent.deps.evidence_store.pages = pages  # type: ignore[method-assign]
    running = asyncio.create_task(agent.run(ctx))
    await raised.wait()
    for _ in range(20):
        await asyncio.sleep(0)
    assert not running.done() and financial.save_calls == ["CON-003"]  # CON-001 还没抽完, Agent 不返回
    release.set()
    with pytest.raises(LookupError, match="CON-002"):
        await running
    assert sorted(financial.save_calls) == ["CON-001", "CON-003"]
    for _ in range(20):
        await asyncio.sleep(0)
    assert sorted(financial.save_calls) == ["CON-001", "CON-003"]  # 上抛之后不再有写入


async def begin(runner: GraphRunner, store: Any) -> Started:
    """启动并通过 ACC-01, 跑到 p1_ingest 之后停下 (等人或到风险闸门)."""
    await runner.start(EID)
    await wait_idle(runner, EID)
    session = Started(runner, store, runner.deps.engagement_store)
    await session.submit.submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    return session


async def rerun(session: Started, task: ReviewTask, number: int) -> None:
    await session.submit.submit(
        EID,
        ReviewRecord(
            id=f"D-RERUN-{number}",
            task_id=task.id,
            target_version=task.target_version,
            decision=Decision.rerun,
            reason="重新抽取",
            reviewed_by="U-1",
            decided_at=datetime.now(UTC),
        ),
    )
    await wait_idle(session.runner, EID)
    assert session.runner.run_error(EID) is None


async def test_failed_contracts_rerun_as_a_group_then_again() -> None:
    """第 1 轮 CON-002, CON-003 失败: 一个 rerun 整组重跑; 第 2 轮只剩 CON-002 失败, 再 rerun 后走到风险闸门."""
    llm = ScriptedLLM(
        {
            "CON-001": [raw("CON-001")],
            "CON-002": ["{}"] * 6 + [raw("CON-002")],
            "CON-003": ["{}"] * 3 + [raw("CON-003")],
        }
    )

    def agents(deps: Any) -> dict[str, Any]:
        deps.judgments.llm = llm
        return {"DOC-04": IngestAgent(deps)}

    runner, store = make_runner(agents=agents)
    session = await begin(runner, store)
    first = await store.latest(EID, ReviewTask, procedure_id="DOC-04", status=TaskStatus.pending)
    assert sorted(t.subject_id for t in first) == ["CON-002", "CON-003"]
    run1 = await store.get(EID, ProcedureRun, "RUN-DOC-04-1")
    assert run1 is not None and run1.status is RunStatus.succeeded
    await rerun(session, first[0], 1)
    [second] = await store.latest(EID, ReviewTask, procedure_id="DOC-04", status=TaskStatus.pending)
    assert (second.subject_id, second.target_id) == ("CON-002", "RUN-DOC-04-2")
    await rerun(session, second, 2)
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    financial = runner.deps.financial_store
    assert isinstance(financial, FakeFinancialStore)
    assert sorted(financial.save_calls) == CONTRACT_IDS
    assert {c.id: c.extracted_by_run for c in financial.saved_contracts.values()} == {
        "CON-001": "RUN-DOC-04-1",
        "CON-002": "RUN-DOC-04-3",
        "CON-003": "RUN-DOC-04-2",
    }


async def test_document_status_failure_recovers_on_resume() -> None:
    """合同已写入而文件状态的事务失败: 重试时重放 DOC-04, 合同照旧覆盖, 文件状态补上, 走到风险闸门."""
    runner, store = make_runner(agents=lambda deps: {"DOC-04": IngestAgent(deps)})
    evidence = runner.deps.evidence_store
    original = evidence.set_status
    failed = False

    async def set_status(eid: str, document_id: str, status: Any) -> None:
        nonlocal failed
        await original(eid, document_id, status)
        if not failed:
            failed = True
            raise RuntimeError("文件状态事务失败")

    evidence.set_status = set_status  # type: ignore[method-assign]
    await begin(runner, store)
    assert "文件状态事务失败" in (runner.run_error(EID) or "")
    financial = runner.deps.financial_store
    assert isinstance(financial, FakeFinancialStore)
    assert sorted(financial.saved_contracts) == CONTRACT_IDS
    assert {d.status for d in financial.document_refs} == {"received"}  # 事务回滚
    await runner.resume(EID)
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    assert {d.status for d in financial.document_refs} == {"extracted"}
    assert all(c.extracted_by_run == "RUN-DOC-04-1" for c in financial.saved_contracts.values())
