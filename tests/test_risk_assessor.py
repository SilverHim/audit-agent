"""A4 RiskAssessorAgent: PLN-07 初步分析与 PLN-01 重要性候选 (合成数据集设计第 3.2 节, 第 7 节第 4, 5 行)."""

from decimal import Decimal
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.risk_assessor import RiskAssessorAgent, analytics_ratios, scaled
from backend.contracts.models import (
    AgentId,
    AnalyticsResult,
    ExceptionDisposition,
    GateVerdict,
    MaterialityCandidates,
    ProcedureStatus,
    RiskItem,
    RiskLevel,
    RunStatus,
)
from backend.contracts.services import RuleResult
from backend.orchestration.nodes import apply_patch
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_CANDIDATES,
    NUMBAT_M01_ROWS,
    NUMBAT_OBJECTIVES,
    NUMBAT_RISKS,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    make_ctx,
    make_deps,
    make_runner,
    numbat_p1_agents,
    numbat_pln07_exception,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision
from tests.test_p1 import p1_setup

EID = ENGAGEMENT.id
NUMBAT_RATIOS = {"gross_margin_current": 0.3, "gross_margin_prior": 0.305, "revenue_growth": 0.1538}


def setup(
    procedure_id: str, rules: FakeRuleEngine | None = None
) -> tuple[RiskAssessorAgent, RunContext, FakeRuleEngine]:
    rules = rules or FakeRuleEngine()
    deps = make_deps(FakeRegistry(), FakeTrail(), rules=rules)
    return RiskAssessorAgent(deps), make_ctx(deps, procedure_id), rules


def with_state(ctx: RunContext, **values: Any) -> RunContext:
    return RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, {**ctx.state, **values})  # type: ignore[typeddict-item]


def only(procedure_id: str, result: RuleResult) -> FakeRuleEngine:
    return FakeRuleEngine({procedure_id: lambda run: result})


# ── PLN-07 ─────────────────────────────────────────


async def test_pln07_runs_rules_with_template_run_and_params_snapshot() -> None:
    agent, ctx, rules = setup("PLN-07")
    await agent.run(ctx)
    assert rules.calls == [("PLN-07", "RUN-PLN-07-1", ctx.run.params_snapshot)]
    assert ctx.run.params_snapshot["change_pct"] == 0.2


async def test_pln07_analytics_and_exception() -> None:
    agent, ctx, _ = setup("PLN-07")
    patch = await agent.run(ctx)
    assert patch["analytics"] == AnalyticsResult(
        id="ANALYTICS", ratios=NUMBAT_RATIOS, exception_ids=["X-PLN-07-1-A01-1"]
    )
    [exception] = patch["exceptions"]
    assert exception == numbat_pln07_exception(ctx.run)
    assert (exception.source_ids, exception.amount_cents) == (["1100"], 52500000)
    assert exception.disposition is ExceptionDisposition.open
    [run] = patch["procedure_runs"]
    assert run.id == "RUN-PLN-07-1" and run.status is RunStatus.succeeded and run.ended_at is not None
    assert set(patch) == {"analytics", "exceptions", "procedure_runs"}


async def test_pln07_next_version_when_state_has_analytics() -> None:
    agent, ctx, _ = setup("PLN-07")
    earlier = AnalyticsResult(id="ANALYTICS", ratios={}, exception_ids=[])
    patch = await agent.run(with_state(ctx, analytics=earlier))
    assert patch["analytics"].version == 2 and patch["analytics"].ratios == NUMBAT_RATIOS


@pytest.mark.parametrize(
    "rows",
    [
        [{"period": "current", "revenue_cents": 1200000000, "cost_of_sales_cents": 840000000}],  # 没有上期行
        [
            {"period": "current", "revenue_cents": 1200000000, "cost_of_sales_cents": 840000000},
            {"period": "prior", "revenue_cents": 0, "cost_of_sales_cents": 0},  # 上期收入为 0
        ],
        [
            {"period": "current", "revenue_cents": 1200000000, "cost_of_sales_cents": 840000000},
            {"period": "prior", "revenue_cents": None, "cost_of_sales_cents": None},  # 上期无数
        ],
    ],
)
async def test_pln07_without_prior_revenue_skips_prior_ratios(rows: list[dict[str, Any]]) -> None:
    agent, ctx, _ = setup("PLN-07", only("PLN-07", RuleResult(exceptions=[], rows={"A02": rows})))
    patch = await agent.run(ctx)
    assert patch["analytics"].ratios == {"gross_margin_current": 0.3}
    assert patch["analytics"].exception_ids == [] and patch["exceptions"] == []


def test_ratios_accept_sql_decimals_and_round_half_up() -> None:
    rows = [
        {"period": "current", "revenue_cents": Decimal(30000), "cost_of_sales_cents": Decimal(19999)},
        {"period": "prior", "revenue_cents": Decimal(16000), "cost_of_sales_cents": Decimal(0)},
    ]
    # 毛利 10001 / 30000 = 0.333366 → 0.3334; 增长 14000 / 16000 = 0.875; 上期毛利 1
    assert analytics_ratios(rows) == {
        "gross_margin_current": 0.3334,
        "gross_margin_prior": 1.0,
        "revenue_growth": 0.875,
    }
    assert analytics_ratios([{"period": "current", "revenue_cents": 20000, "cost_of_sales_cents": 19999}]) == {
        "gross_margin_current": 0.0001  # 0.00005 四舍五入进位
    }
    assert analytics_ratios([]) == {}


# ── PLN-01 ─────────────────────────────────────────


async def test_pln01_candidates_follow_params_order() -> None:
    agent, ctx, rules = setup("PLN-01")
    patch = await agent.run(ctx)
    assert rules.calls == [("PLN-01", "RUN-PLN-01-1", ctx.run.params_snapshot)]
    assert patch["materiality_candidates"] == MaterialityCandidates(
        id="MATERIALITY", candidates=NUMBAT_CANDIDATES, performance_pct_range=(0.5, 0.75), clearly_trivial_pct=0.05
    )
    assert [(c.basis, c.low_cents, c.high_cents) for c in patch["materiality_candidates"].candidates] == [
        ("revenue", 6000000, 12000000),
        ("total_assets", 8000000, 16000000),
        ("profit_before_tax", 7500000, 7500000),
    ]
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


async def test_pln01_uses_params_snapshot_not_template() -> None:
    agent, ctx, _ = setup("PLN-01")
    params = ctx.run.params_snapshot | {
        "candidates": [{"basis": "revenue", "pct_low": 0.015, "pct_high": 0.02}],
        "performance_pct_range": [0.6, 0.7],
        "clearly_trivial_pct": 0.03,
    }
    run = ctx.run.model_copy(update={"params_snapshot": params})
    patch = await agent.run(RunContext(ctx.engagement, ctx.procedure, ctx.template, run, ctx.state))
    result = patch["materiality_candidates"]
    assert [(c.basis, c.low_cents, c.high_cents) for c in result.candidates] == [("revenue", 18000000, 24000000)]
    assert (result.performance_pct_range, result.clearly_trivial_pct) == ((0.6, 0.7), 0.03)


def test_scaled_rounds_half_up_on_decimal_percent() -> None:
    assert scaled(1300, 0.005) == 7  # 6.5 四舍五入进位, 不按银行家舍入取 6
    assert scaled(100, 0.015) == 2  # 0.015 的二进制近似略小于 0.015, 按字符串转 Decimal 才得 1.5 → 2
    assert scaled(Decimal(150000000), 0.05) == 7500000  # type: ignore[arg-type]


async def test_pln01_missing_basis_row_raises() -> None:
    rows = [r for r in NUMBAT_M01_ROWS if r["basis"] != "total_assets"]
    agent, ctx, _ = setup("PLN-01", only("PLN-01", RuleResult(exceptions=[], rows={"M01": rows})))
    with pytest.raises(LookupError, match="total_assets"):
        await agent.run(ctx)


async def test_pln01_brings_presets_when_risk_register_empty() -> None:
    agent, ctx, _ = setup("PLN-01")
    patch = await agent.run(ctx)
    assert patch["audit_objectives"] == NUMBAT_OBJECTIVES and patch["risk_register"] == NUMBAT_RISKS
    assert {o.version for o in patch["audit_objectives"]} | {r.version for r in patch["risk_register"]} == {1}


async def test_pln01_keeps_existing_register_and_bumps_version() -> None:
    agent, ctx, _ = setup("PLN-01")
    revised = [NUMBAT_RISKS[0].model_copy(update={"version": 2, "level": "high"}), *NUMBAT_RISKS[1:]]
    earlier = MaterialityCandidates(id="MATERIALITY", candidates=[])
    patch = await agent.run(
        with_state(ctx, risk_register=revised, audit_objectives=NUMBAT_OBJECTIVES, materiality_candidates=earlier)
    )
    assert "audit_objectives" not in patch and "risk_register" not in patch
    assert patch["materiality_candidates"].version == 2
    assert patch["materiality_candidates"].candidates == NUMBAT_CANDIDATES


# ── 接入 p1_analytics 节点 ─────────────────────────────────────────


async def test_real_a4_in_p1_analytics_reaches_risk_gate() -> None:
    def agents(deps: Any) -> dict[str, Any]:
        a4 = RiskAssessorAgent(deps)
        return {"DOC-04": numbat_p1_agents(deps)["DOC-04"], "PLN-07": a4, "PLN-01": a4}

    runner, store = make_runner(
        registry=FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC}), agents=agents
    )
    await runner.start(EID)
    await wait_idle(runner, EID)
    s = Started(runner, store, runner.deps.engagement_store)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    state = snapshot.values
    assert state["analytics"].ratios == NUMBAT_RATIOS and state["analytics"].exception_ids == ["X-PLN-07-1-A01-1"]
    assert state["materiality_candidates"].candidates == NUMBAT_CANDIDATES
    assert state["risk_register"] == NUMBAT_RISKS and state["audit_objectives"] == NUMBAT_OBJECTIVES
    pln = {p.id: p for p in state["procedure_pack"] if p.id in {"PLN-07", "PLN-01"}}
    assert pln["PLN-07"].status is ProcedureStatus.completed and pln["PLN-01"].status is ProcedureStatus.planned


async def test_rerun_keeps_revised_risks_and_recomputes_candidates() -> None:
    """闸门改过风险等级后 p1_analytics 重跑: 风险清单不被预置覆盖, 分析与候选出第 2 版, 候选按新取数重算."""
    nodes, store, state = p1_setup()
    a4 = RiskAssessorAgent(nodes.deps)
    nodes.agents.update({"PLN-07": a4, "PLN-01": a4})
    state = apply_patch(state, await nodes.p1_analytics(state))
    revised = state["risk_register"][0].model_copy(update={"version": 2, "level": RiskLevel.high})
    await store.put(EID, [revised], actor=AgentId.A1)
    state["risk_register"] = [revised, *state["risk_register"][1:]]
    rules = nodes.deps.rules
    assert isinstance(rules, FakeRuleEngine)
    doubled = [{**r, "base_amount_cents": Decimal(r["base_amount_cents"] * 2)} for r in NUMBAT_M01_ROWS]
    rules.results["PLN-01"] = lambda run: RuleResult(exceptions=[], rows={"M01": doubled})
    second = await nodes.p1_analytics(state)
    assert "risk_register" not in second and "audit_objectives" not in second
    assert second["analytics"].version == second["materiality_candidates"].version == 2
    assert second["analytics"].exception_ids == ["X-PLN-07-2-A01-1"]
    assert [c.low_cents for c in second["materiality_candidates"].candidates] == [12000000, 16000000, 15000000]
    stored = await store.get(EID, RiskItem, revised.id)
    assert stored is not None and stored.version == 2
