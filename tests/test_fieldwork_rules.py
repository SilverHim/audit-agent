"""A6 的纯规则程序 REV-08, CSH-05: 调规则引擎取例外, 原样返回 (合成数据集设计第 7 节第 7, 12 行)."""

from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.errors import RuleError
from backend.contracts.models import AgentId, ProcedureRun, RiskLevel, RunStatus, TaskKind
from backend.contracts.records import DatasetTraits
from backend.contracts.services import RuleResult
from backend.orchestration.nodes import GraphNodes
from tests.fakes import (
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    make_ctx,
    make_deps,
    make_run,
    numbat_csh05_exception,
    numbat_rev08_exception,
)


def setup(procedure_id: str, rules: FakeRuleEngine | None = None) -> tuple[FieldworkAgent, RunContext]:
    """A6 与某条程序第 1 次执行的上下文; 参数按特别风险解析 (Numbat 收入目标为 significant)."""
    deps = make_deps(FakeRegistry(), FakeTrail(), rules=rules)
    ctx = make_ctx(deps, procedure_id)
    params = deps.library.resolve_params(
        ctx.template, ctx.engagement, ctx.state, RiskLevel.significant, traits=DatasetTraits()
    )
    run = ctx.run.model_copy(update={"params_snapshot": params})
    return FieldworkAgent(deps), RunContext(ctx.engagement, ctx.procedure, ctx.template, run, ctx.state)


def fixed(procedure_id: str, result: RuleResult, seen: list[ProcedureRun] | None = None) -> FakeRuleEngine:
    """对该程序总是返回同一个结果对象的规则引擎; seen 记下收到的执行记录."""

    def make(run: ProcedureRun) -> RuleResult:
        if seen is not None:
            seen.append(run)
        return result

    return FakeRuleEngine({procedure_id: make})


@pytest.mark.parametrize(
    ("procedure_id", "build"), [("REV-08", numbat_rev08_exception), ("CSH-05", numbat_csh05_exception)]
)
async def test_rule_program_returns_exceptions_unchanged(procedure_id: str, build: Any) -> None:
    result = RuleResult(exceptions=[build(make_run(procedure_id))], rows={})
    agent, ctx = setup(procedure_id, fixed(procedure_id, result))
    patch = await agent.run(ctx)
    [exception] = patch["exceptions"]
    assert exception is result.exceptions[0]
    [run] = patch["procedure_runs"]
    assert run.id == f"RUN-{procedure_id}-1" and run.status is RunStatus.succeeded and run.ended_at is not None
    assert set(patch) == {"exceptions", "procedure_runs"}


async def test_numbat_rule_results() -> None:
    agent, ctx = setup("REV-08")
    [rev08] = (await agent.run(ctx))["exceptions"]
    assert (rev08.id, rev08.rule_id, rev08.source_ids, rev08.amount_cents) == (
        "X-REV-08-1-R01-1",
        "R01",
        ["SI-2026-0612"],
        14200000,
    )
    agent, ctx = setup("CSH-05")
    [csh05] = (await agent.run(ctx))["exceptions"]
    assert (csh05.id, csh05.rule_id, csh05.source_ids, csh05.amount_cents) == (
        "X-CSH-05-1-B01-1",
        "B01",
        ["BA-01"],
        -5000000,
    )


@pytest.mark.parametrize("procedure_id", ["REV-08", "CSH-05"])
async def test_rules_get_run_and_params_snapshot(procedure_id: str) -> None:
    seen: list[ProcedureRun] = []
    rules = fixed(procedure_id, RuleResult(exceptions=[], rows={}), seen)
    agent, ctx = setup(procedure_id, rules)
    await agent.run(ctx)
    assert seen == [ctx.run] and seen[0] is ctx.run
    assert rules.calls == [(procedure_id, ctx.run.id, ctx.run.params_snapshot)]
    if procedure_id == "REV-08":
        assert ctx.run.params_snapshot == {"window_days": 20, "period_end": ctx.engagement.period_end}


async def test_no_hits_still_succeeds() -> None:
    agent, ctx = setup("REV-08", fixed("REV-08", RuleResult(exceptions=[], rows={})))
    patch = await agent.run(ctx)
    assert patch["exceptions"] == [] and patch["procedure_runs"][0].status is RunStatus.succeeded


def raising(error: Exception) -> FakeRuleEngine:
    def make(run: ProcedureRun) -> RuleResult:
        raise error

    return FakeRuleEngine({"CSH-05": make})


async def test_rule_error_propagates() -> None:
    error = RuleError("B01 timed out")
    agent, ctx = setup("CSH-05", raising(error))
    with pytest.raises(RuleError) as caught:
        await agent.run(ctx)
    assert caught.value is error


async def test_rule_error_becomes_failed_run_by_a1() -> None:
    agent, ctx = setup("CSH-05", raising(RuleError("B01 timed out")))
    patch, writer = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.failed and run.failure_reason == "RuleError: B01 timed out"
    assert patch["review_tasks"][0].kind is TaskKind.failed
