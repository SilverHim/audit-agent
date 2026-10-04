"""A6 REV-03 收入合同条款评价: 每份合同一条判断与一条 normal 任务 (合成数据集设计第 7 节第 8 行)."""

from dataclasses import replace
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.models import (
    Phase,
    ProposedAction,
    ReviewStatus,
    RunStatus,
    StepKind,
    TaskKind,
    TrailKind,
)
from backend.contracts.services import JudgmentOutcome
from tests.fakes import (
    NUMBAT_CONTRACTS,
    NUMBAT_OBJECTIVES,
    NUMBAT_RISKS,
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeTrail,
    make_ctx,
    make_deps,
    numbat_contracts,
)
from tests.test_fieldwork_judging import judgment

CONTRACT_IDS = ["CON-001", "CON-002", "CON-003"]
EXTRACTED = [d.model_copy(update={"status": "extracted"}) for d in NUMBAT_CONTRACTS]


def setup(
    engine: Any = None, contracts: list[Any] | None = None, documents: list[Any] | None = None
) -> tuple[FieldworkAgent, RunContext, FakeTrail]:
    """A6 与 REV-03 第 1 次执行: 财务数据存储有 DOC-04 抽出的合同 (缺省为 Numbat 的 3 份), 状态有抽取过的文件.

    engine 缺省为真实判断引擎 (大模型为 mock 模式, 读 tests/fixtures/llm/REV-03).
    """
    trail = FakeTrail()
    financial = FakeFinancialStore(
        list(NUMBAT_CONTRACTS), contracts=numbat_contracts() if contracts is None else contracts
    )
    deps = make_deps(FakeRegistry(), trail, financial_store=financial)
    if engine is not None:
        deps = replace(deps, judgments=engine)
    ctx = make_ctx(deps, "REV-03")
    procedure = ctx.procedure.model_copy(
        update={"phase": Phase.P2_Executing, "objective_ids": ["OBJ-REV-OCC", "OBJ-REV-CUT"]}
    )
    params = deps.library.resolve_params(ctx.template, ctx.engagement, ctx.state, None)
    state = ctx.state | {
        "procedure_pack": [procedure],
        "audit_objectives": NUMBAT_OBJECTIVES,
        "risk_register": NUMBAT_RISKS,
        "documents": EXTRACTED if documents is None else documents,
    }
    run = ctx.run.model_copy(update={"params_snapshot": params})
    return FieldworkAgent(deps), RunContext(ctx.engagement, procedure, ctx.template, run, state), trail  # type: ignore[arg-type]


def fake_engine() -> FakeJudgmentEngine:
    return FakeJudgmentEngine(
        {
            c: lambda jctx, run: JudgmentOutcome(judgment=judgment(jctx, run), failure=None, attempts=1, trail_ids=[])
            for c in CONTRACT_IDS
        }
    )


async def test_subjects_are_all_contracts_sorted() -> None:
    engine = fake_engine()
    contracts = list(reversed(numbat_contracts()))
    agent, ctx, _ = setup(engine, contracts=contracts)
    await agent.run(ctx)
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == CONTRACT_IDS
    for (_, jctx, _), contract, document in zip(engine.calls, numbat_contracts(), EXTRACTED, strict=True):
        assert jctx.data == {"contract": contract.model_dump(mode="json"), "document": document.model_dump(mode="json")}
        assert jctx.fixture_key == contract.id and jctx.rule_findings == []
        assert jctx.task["params"] == {"period_end": "2026-06-30"}


async def test_fake_engine_gives_three_judgments_and_normal_tasks() -> None:
    agent, ctx, _ = setup(fake_engine())
    patch = await agent.run(ctx)
    assert [j.id for j in patch["judgments"]] == [f"J-REV-03-1-{c}" for c in CONTRACT_IDS]
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [
        (f"T-J-REV-03-1-{c}-v1", TaskKind.normal) for c in CONTRACT_IDS
    ]
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


async def test_fixtures_pass_real_engine_in_one_attempt() -> None:
    agent, ctx, trail = setup()
    patch = await agent.run(ctx)
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert len(calls) == 3 and {e.payload["attempt"] for e in calls} == {1}
    assert all(e.payload["level_failed"] is None for e in calls)
    for entry in calls:
        prompt = entry.payload["messages"][0]["content"]
        assert "- AASB15-P31 (AASB 15 para 31): An entity shall recognise revenue when (or as)" in prompt
        assert "- AASB15-P38 (AASB 15 para 38): If a performance obligation is not satisfied over time" in prompt
    judgments = {j.subject_id: j for j in patch["judgments"]}
    assert sorted(judgments) == CONTRACT_IDS
    for j in judgments.values():
        assert j.proposed_action is ProposedAction.accept and j.misstatement_draft is None
        assert j.review_status is ReviewStatus.pending and j.exception_ids == []
    assert [t.id for t in patch["review_tasks"]] == [f"T-J-REV-03-1-{c}-v1" for c in CONTRACT_IDS]


@pytest.mark.parametrize("cid", ["CON-001", "CON-002"])
async def test_signature_contracts_cite_t1_and_standards(cid: str) -> None:
    agent, ctx, _ = setup()
    patch = await agent.run(ctx)
    [j] = [j for j in patch["judgments"] if j.subject_id == cid]
    pointers = [p for step in j.reasoning_chain for p in step.evidence]
    term = [p for p in pointers if p.source_type == "document"]
    assert [(p.source_id, p.locator, p.page) for p in term] == [(f"DOC-CON00{cid[-1]}000000", f"{cid}-T1", 2)]
    assert {p.source_id for p in pointers if p.source_type == "standard"} == {"AASB15-P31", "AASB15-P38"}
    assert j.reasoning_chain[-1].kind is StepKind.inference


async def test_con003_names_transfer_point_return_right_and_rebate() -> None:
    agent, ctx, _ = setup()
    patch = await agent.run(ctx)
    [j] = [j for j in patch["judgments"] if j.subject_id == "CON-003"]
    assert "CON-003-T2" in j.conclusion and "distribution centre" in j.conclusion
    assert "CON-003-T3" in j.conclusion and "30-day return right" in j.conclusion
    assert "CON-003-T4" in j.conclusion and "rebate" in j.conclusion
    locators = {p.locator for step in j.reasoning_chain for p in step.evidence if p.source_type == "document"}
    assert locators == {"CON-003-T2", "CON-003-T3", "CON-003-T4"}


async def test_document_missing_from_state_raises() -> None:
    agent, ctx, _ = setup(fake_engine(), documents=EXTRACTED[:2])
    with pytest.raises(LookupError, match="CON-003"):
        await agent.run(ctx)


async def test_no_contracts_still_succeeds() -> None:
    agent, ctx, _ = setup(fake_engine(), contracts=[])
    patch = await agent.run(ctx)
    assert patch["judgments"] == [] and patch["review_tasks"] == []
    assert patch["procedure_runs"][0].status is RunStatus.succeeded
