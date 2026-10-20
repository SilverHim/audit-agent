"""A4 RiskAssessorAgent: PLN-07 初步分析与 PLN-01 重要性候选 (合成数据集设计第 3.2 节, 第 7 节第 4, 5 行); PLN-15
总体审计计划 (需求 R204, Xero 演示, 设计稿 v2.1 第 2.7, 6.6 节)."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic_core import to_jsonable_python

from backend.adapters.llm import MockLLMClient
from backend.agents.audit_plan import trial_balance
from backend.agents.base import AgentDeps, RunContext
from backend.agents.registry import build_agents
from backend.agents.risk_assessor import RiskAssessorAgent, analytics_ratios, monthly_figures, scaled
from backend.contracts.models import (
    AgentId,
    AnalyticsResult,
    Decision,
    EngagementTerms,
    ExceptionDisposition,
    GateDecision,
    GateId,
    GateVerdict,
    MaterialityCandidates,
    MonthlyFigures,
    Phase,
    PlanStatus,
    ProcedureStatus,
    RiskItem,
    RiskLevel,
    RunStatus,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import LLMClient
from backend.contracts.services import RuleResult
from backend.contracts.state import StatePatch
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import GraphNodes, apply_patch, build_run
from backend.orchestration.runner import GraphRunner
from backend.services.judgment_engine import MAX_ATTEMPTS
from tests.external import needs
from tests.fakes import (
    DOC_EL,
    ENGAGEMENT,
    NOW,
    NUMBAT_A03_ROWS,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_CANDIDATES,
    NUMBAT_M01_ROWS,
    NUMBAT_OBJECTIVES,
    NUMBAT_RISKS,
    PLAN_REJECT_ID,
    PLAN_REJECT_REASON,
    PLANNING_FEATURES,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    ScriptedLLM,
    engagement_terms,
    make_ctx,
    make_deps,
    make_runner,
    numbat_p1_agents,
    numbat_pln07_exception,
    wait_idle,
)
from tests.fixture_sets import XERO
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_output_validator import PLAN_PROCEDURES
from tests.test_p1 import p1_setup
from tests.xero_fakes import (
    DEMO_FEATURES,
    XERO17_MATERIALITY,
    make_xero_deps,
    xero_engagement,
    xero_profile,
    xero_state,
    xero_traits,
)

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
        id="ANALYTICS",
        ratios=NUMBAT_RATIOS,
        exception_ids=["X-PLN-07-1-A01-1"],
        monthly=[MonthlyFigures.model_validate(row) for row in NUMBAT_A03_ROWS],
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


async def test_pln07_monthly_table_from_a03_rows() -> None:
    """需求 R103 验收 1: Numbat 的 ANALYTICS v1.monthly 为 12 行, 首月 2025-07 88000000 / 77321600 / 9566600, 末月
    2026-06 150000000 / 188462400 / 12767400; 各列合计同本期试算表. A03 只出取数, 不出例外."""
    agent, ctx, _ = setup("PLN-07")
    monthly = (await agent.run(ctx))["analytics"].monthly
    assert len(monthly) == 12
    assert monthly[0] == MonthlyFigures(
        month=date(2025, 7, 1), revenue_cents=88000000, expense_cents=77321600, employee_expense_cents=9566600
    )
    assert monthly[-1] == MonthlyFigures(
        month=date(2026, 6, 1), revenue_cents=150000000, expense_cents=188462400, employee_expense_cents=12767400
    )
    assert [m.month for m in monthly] == [date(2025 + (6 + n) // 12, (6 + n) % 12 + 1, 1) for n in range(12)]
    totals = (sum(m.revenue_cents for m in monthly), sum(m.expense_cents for m in monthly))
    assert totals == (1200000000, 1096400000) and sum(m.employee_expense_cents for m in monthly) == 118000000


async def test_pln07_without_a03_rows_has_no_monthly_table() -> None:
    """需求 R103 验收 3: 没有 A03 行时 monthly 为空, 比率与例外照常."""
    result = RuleResult(
        exceptions=[], rows={"A02": [{"period": "current", "revenue_cents": 10, "cost_of_sales_cents": 7}]}
    )
    agent, ctx, _ = setup("PLN-07", only("PLN-07", result))
    analytics = (await agent.run(ctx))["analytics"]
    assert analytics.monthly == [] and analytics.ratios == {"gross_margin_current": 0.3}


def test_monthly_figures_sort_by_month_and_count_nulls_as_zero() -> None:
    """需求 R103 验收 3: 行按月升序 (SQL 已排序, 这里不依赖), 某列为空 (那个月没有该类分录) 记 0; SQL 的 SUM 是
    Decimal, 转成整数分."""
    rows = [
        {"month": date(2026, 2, 1), "revenue_cents": Decimal(500), "expense_cents": None, "employee_expense_cents": 0},
        {
            "month": date(2026, 1, 1),
            "revenue_cents": None,
            "expense_cents": Decimal(300),
            "employee_expense_cents": None,
        },
    ]
    assert monthly_figures(rows) == [
        MonthlyFigures(month=date(2026, 1, 1), revenue_cents=0, expense_cents=300, employee_expense_cents=0),
        MonthlyFigures(month=date(2026, 2, 1), revenue_cents=500, expense_cents=0, employee_expense_cents=0),
    ]
    assert monthly_figures([]) == []


def test_old_analytics_json_reads_empty_monthly() -> None:
    """需求 R103 验收 3: 不含 monthly 键的旧 AnalyticsResult JSON (检查点, gate_materials 表) 读回为空; 新结果经 JSON
    与检查点序列化读回相等."""
    from backend.orchestration.checkpoint import checkpoint_serde

    old = {"id": "ANALYTICS", "version": 1, "ratios": NUMBAT_RATIOS, "exception_ids": ["X-PLN-07-1-A01-1"]}
    assert AnalyticsResult.model_validate_json(json.dumps(old)).monthly == []
    new = AnalyticsResult(
        id="ANALYTICS", ratios={}, exception_ids=[], monthly=[MonthlyFigures.model_validate(NUMBAT_A03_ROWS[0])]
    )
    assert AnalyticsResult.model_validate_json(new.model_dump_json()) == new
    serde = checkpoint_serde()
    assert serde.loads_typed(serde.dumps_typed({"analytics": new}))["analytics"] == new


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


@pytest.mark.parametrize(
    ("basis", "base", "expected"),
    [
        ("profit_before_tax", -150000000, "loss"),  # 税前亏损
        ("profit_before_tax", 0, "not_positive"),
        ("revenue", 0, "not_positive"),
        ("total_assets", -1, "not_positive"),
    ],
)
async def test_pln01_marks_non_positive_basis_not_applicable(basis: str, base: int, expected: str) -> None:
    """需求 R351: 基准金额不为正的候选照常列出, 金额照算, 标本期不适用 (税前利润为负记 loss, 其余 not_positive);
    其余两项不标. 防把它从材料里删掉, 或只认税前利润."""
    rows = [r | {"base_amount_cents": base} if r["basis"] == basis else r for r in NUMBAT_M01_ROWS]
    agent, ctx, _ = setup("PLN-01", only("PLN-01", RuleResult(exceptions=[], rows={"M01": rows})))
    patch = await agent.run(ctx)
    found = {c.basis: c for c in patch["materiality_candidates"].candidates}
    assert [c.basis for c in found.values()] == ["revenue", "total_assets", "profit_before_tax"]
    assert {b: c.not_applicable for b, c in found.items()} == {
        b: expected if b == basis else None for b in ("revenue", "total_assets", "profit_before_tax")
    }
    assert (found[basis].base_amount_cents, found[basis].low_cents) == (base, scaled(base, found[basis].pct_low))


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


# ── PLN-15 总体审计计划 (需求 R204, 功能 planning) ──

PLAN_FEATURES = PLANNING_FEATURES | {"acceptance"}  # PLN-15 的骨架另看 TERMS
XERO_PLAN = XERO.fixtures / "PLN-15" / "r1.json"  # Xero 的固定输出正本 (验收 3)
XERO_PLAN_R2 = XERO.fixtures / "PLN-15" / "r2.json"  # 第 2 修订: 回应演示的驳回理由 (需求 R316)
JOURNALS = ["GEN-02", "GEN-05"]  # 功能 journals 装配的 P2 固定集 (需求 R146)
CASH, REVENUE = ["OBJ-CASH-EXI"], ["OBJ-REV-OCC", "OBJ-REV-CUT"]
# 验收 1: Xero 装配后的 P2 程序 (PLAN_PROCEDURES, 按程序编号) → 关联目标
PLAN_OBJECTIVES = dict(zip(PLAN_PROCEDURES, [CASH, CASH, CASH, [], ["OBJ-REV-CUT"], REVENUE, REVENUE], strict=True))
PLAN_PARAMS = {  # 验收 1 列出的参数 (其余参数取建 Run 时的解析结果, 见 test_pln15_xero_skeleton)
    "CSH-02": {"period_end": "2026-09-30", "subsequent_days": 10, "lookback_days": 5},
    "CSH-04": {"as_of": "2026-09-30"},
    "CSH-05": {"period_end": "2026-09-30"},
    "DOC-06": {},
    "REV-02": {"window_days": 20, "threshold_cents": 16216, "period_end": "2026-09-30"},
    "REV-03": {"period_end": "2026-09-30"},
    "REV-08": {"window_days": 20, "period_end": "2026-09-30"},
}


def plan_reject(target_id: str, reason: str = PLAN_REJECT_REASON, decision_id: str = PLAN_REJECT_ID) -> GateDecision:
    """PLN-15 对 target_id 第 1 版的驳回."""
    return GateDecision(
        id=decision_id,
        gate=GateId.PLN_15,
        target_id=target_id,
        target_version=1,
        decision=GateVerdict.reject,
        reason=reason,
        decided_by="U-1",
        decided_at=NOW,
    )


async def plan_context(
    llm: LLMClient | None = None, features: frozenset[str] = PLAN_FEATURES, **state: Any
) -> tuple[RunContext, AgentDeps, FakeTrail]:
    """Xero 项目 PLN-01/03 通过 (收入 1%, 50%, 5%) 并装配 P2 程序包后 PLN-15 的下一次执行: 状态为 xero_state 加
    闸门决定与 TERMS (功能 acceptance 打开时; 关闭时为空), 程序包取 assemble_pack 的结果, 再按 state 改 (如带上
    一修订, 驳回与执行记录); 执行记录同 GraphNodes 用 build_run 建. 大模型缺省读 Xero 的正本 (llm_xero)."""
    trail = FakeTrail()
    deps = make_xero_deps(trail=trail, llm=llm or MockLLMClient(XERO.fixtures), features=features)
    base = xero_state() | {
        "gate_decisions": [pln_decision(GateVerdict.approve, materiality=XERO17_MATERIALITY)],
        "engagement_terms": engagement_terms() if "acceptance" in features else None,
    }
    assembled = await GraphNodes(deps, 2).assemble_pack(base)  # type: ignore[arg-type]
    current: Any = base | {"procedure_pack": assembled["procedure_pack"]} | state
    engagement = xero_engagement()
    ctx = make_ctx(deps, "PLN-15", engagement=engagement)
    run = build_run(
        deps.library,
        ctx.template,
        ctx.procedure,
        engagement,
        current,
        NOW,
        traits=xero_traits(),
        profile=xero_profile(),
    )
    return RunContext(engagement, ctx.procedure, ctx.template, run, current), deps, trail


async def drafted(ctx: RunContext, deps: AgentDeps) -> StatePatch:
    """A4 执行 PLN-15, 片段经 check_patch 校验 (A4, 本程序的 outputs, 同 GraphNodes.run_agent)."""
    patch = await RiskAssessorAgent(deps).run(ctx)
    check_patch(
        patch,
        writer=AgentId.A4,
        procedure_id="PLN-15",
        run_id=ctx.run.id,
        state=ctx.state,
        outputs=ctx.template.outputs,
    )
    return patch


def plan_raw(**changes: Any) -> str:
    """Xero 正本的原文, 按 changes 改顶层字段."""
    return json.dumps(json.loads(XERO_PLAN.read_text(encoding="utf-8")) | changes)


def llm_calls(trail: FakeTrail) -> list[TrailEntry]:
    return [e for e in trail.entries if e.kind is TrailKind.llm_call]


@needs("q1_snapshot", "q1_supplements")
async def test_pln15_xero_skeleton() -> None:
    """验收 1: PLAN-1 v1 draft (revision 1, RUN-PLN-15-1); items 按程序编号排序, 目标同装配, 参数为建 Run 时的解析
    结果 (含所列各项), template_commit 为 library.commit; 重要性取闸门选定的与状态中的三项; 特别风险为两条收入目标;
    适用框架与约定书取 TERMS (约定书取出具日指针的文件, 没有时取框架指针的); 确认三项为空. 功能 acceptance 关闭时
    (没有 TERMS) 框架与约定书为空, 其余相同."""
    ctx, deps, _ = await plan_context()
    [plan] = (await drafted(ctx, deps))["audit_plans"]
    assert (plan.id, plan.version, plan.status, plan.revision, plan.run_id) == (
        "PLAN-1",
        1,
        PlanStatus.draft,
        1,
        "RUN-PLN-15-1",
    )
    assert {i.procedure_id: i.objective_ids for i in plan.items} == PLAN_OBJECTIVES
    assert [i.procedure_id for i in plan.items] == PLAN_PROCEDURES
    pack = {p.id: p for p in ctx.state["procedure_pack"]}
    for item in plan.items:
        template = deps.library.load(item.procedure_id)
        run = build_run(
            deps.library,
            template,
            pack[item.procedure_id],
            ctx.engagement,
            ctx.state,
            NOW,
            traits=xero_traits(),
            profile=xero_profile(),
        )
        assert item.params == to_jsonable_python(run.params_snapshot)
        assert {k: item.params[k] for k in PLAN_PARAMS[item.procedure_id]} == PLAN_PARAMS[item.procedure_id]
        assert (item.name, item.template_commit) == (template.name, deps.library.commit)
    assert plan.materiality_choice == XERO17_MATERIALITY
    assert (plan.materiality_cents, plan.performance_materiality_cents, plan.clearly_trivial_cents) == (
        32431,
        16216,
        1622,
    )
    assert plan.significant_risk_objective_ids == ["OBJ-REV-OCC", "OBJ-REV-CUT"]
    assert (plan.period_start, plan.period_end) == (date(2026, 7, 1), date(2026, 9, 30))
    assert (plan.applicable_framework, plan.engagement_letter_document_id) == ("tier2_gpfs", DOC_EL)
    assert (plan.eqr_required, plan.eqr_basis, plan.auditor_expert_not_required) == (None, None, None)
    assert (plan.approved_by, plan.addresses_decision_id, plan.revision_reason) == (None, None, None)

    ctx, deps, _ = await plan_context(features=PLAN_FEATURES - {"acceptance"})
    [bare] = (await drafted(ctx, deps))["audit_plans"]
    assert (bare.applicable_framework, bare.engagement_letter_document_id) == (None, None)
    framework = {"applicable_framework", "engagement_letter_document_id"}
    assert bare.model_dump(exclude=framework) == plan.model_dump(exclude=framework)

    # 约定书取出具日指针 (letter_evidence) 的文件, 没有时取框架指针所在的文件; 两者指向不同文件以区分先后
    terms = engagement_terms()
    moved = terms.framework_evidence[0].model_copy(update={"source_id": "DOC-FRAMEWORK"})
    for letter, expected in ((terms.letter_evidence, DOC_EL), ([], "DOC-FRAMEWORK")):
        changed = EngagementTerms.model_validate(
            {**terms.model_dump(), "letter_evidence": letter, "framework_evidence": [moved]}
        )
        ctx, deps, _ = await plan_context(engagement_terms=changed)
        [found] = (await drafted(ctx, deps))["audit_plans"]
        assert found.engagement_letter_document_id == expected


@needs("q1_snapshot", "q1_supplements")
async def test_pln15_request_prompt_and_trail() -> None:
    """验收 2: 请求为 PLN-15, RUN-PLN-15-1, fixture_key r1; 提示词含 7 条程序的编号与参数, 重要性三项, tier2_gpfs,
    期间 3 个月, Sam Whitfield 的 s293 指示与本期试算表各科目, 不含修订段; 1 行 llm_call (A4); 执行成功, 片段只有
    计划与执行记录."""
    llm = ScriptedLLM({"r1": [XERO_PLAN.read_text(encoding="utf-8")]})
    ctx, deps, trail = await plan_context(llm)
    patch = await drafted(ctx, deps)
    [request] = llm.requests
    assert (request.procedure_id, request.run_id, request.fixture_key) == ("PLN-15", "RUN-PLN-15-1", "r1")
    prompt = "\n".join(m.content for m in request.messages)
    for item in patch["audit_plans"][0].items:
        assert f"- {item.procedure_id}: objectives:" in prompt
        assert f"parameters: {json.dumps(item.params, sort_keys=True)}" in prompt
    for amount in ("overall materiality 32431 cents", "performance materiality 16216 cents", "trivial threshold 1622"):
        assert amount in prompt
    assert '"framework": "tier2_gpfs"' in prompt and "2026-07-01 to 2026-09-30 (3 months)" in prompt
    assert '"signed_by": "Sam Whitfield"' in prompt and "CA2001-S293 (Corporations Act 2001 para s293)" in prompt
    rows = await trial_balance(deps, ctx.engagement.id)
    assert len(rows) > 20 and all(
        f"- {r['code']} {r['name']} ({r['category']}): {r['balance_cents']}" in prompt for r in rows
    )
    assert "- 200 Sales (revenue): -3243100" in prompt
    assert "## Revision" not in prompt
    [row] = llm_calls(trail)
    assert (row.actor, row.run_id, row.payload["level_failed"]) == (AgentId.A4, "RUN-PLN-15-1", None)
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-PLN-15-1", RunStatus.succeeded)
    assert set(patch) == {"audit_plans", "procedure_runs"}  # 不改程序 (闸门程序的状态归节点, Q5)


@needs("q1_snapshot", "q1_supplements")
@pytest.mark.parametrize("extra", [[], JOURNALS], ids=["journals_off", "journals_on"])
async def test_pln15_xero_fixture_passes_once(extra: list[str]) -> None:
    """验收 3: Xero 正本一次通过校验 (1 行 llm_call), 叙述按设计稿第 6.6, 2.7 节: 总体策略在首, 各段, 计划的程序各一条
    说明, 时间表 (外勤 10-19 至 10-22, 报告日 10-23, 送达股东 2027-01-31), 不需要项目质量复核及其依据. 正本的说明有
    9 条 (需求 R316): 功能 journals 关闭时计划 7 个程序, 多出的 GEN-05, GEN-02 两条不收进计划; 打开时 (演示配置)
    计划 9 个程序, 9 条都收."""
    ctx, deps, trail = await plan_context(features=PLAN_FEATURES | ({"collab", "journals"} if extra else set()))
    [plan] = (await drafted(ctx, deps))["audit_plans"]
    assert [i.procedure_id for i in plan.items] == sorted([*PLAN_PROCEDURES, *extra])
    assert len(llm_calls(trail)) == 1
    text = {s.section: s.text for s in plan.narrative}
    assert [s.section for s in plan.narrative] == [
        "overall_strategy",
        "scope",
        "initial_audit",
        "significant_risks",
        "going_concern",
        "other_significant_balances",
        "materiality",
        "timing",
        "reporting",
    ]
    expected = {
        "scope": ["s323D(2A)", "1 July 2026 to 30 September 2026", "s293", "Sam Whitfield"],
        "initial_audit": ["first audit", "ASA 510 para 6", "ASA 710 para 14", "ASA 300 para 13"],
        "significant_risks": ["presumed fraud risk", "ASA 240 para 32", "ASA 240 para 33(a)", "sole director"],
        "going_concern": ["ASA 570 para 10", "(090) was overdrawn throughout the quarter", "net liabilities"],
        "other_significant_balances": [
            "(477)",
            "(825)",
            "(826)",
            "840 Historical Adjustment",
            "(710)",
            "(711)",
            "(610)",
            "(800)",
            "(820)",
            "opening balances",
        ],
        "materiality": ["$324.31", "$162.16", "$16.22"],
        "timing": ["19 to 22 October 2026", "23 October 2026", "31 January 2027"],
        "reporting": ["s315(4)", "31 January 2027"],
    }
    assert {name: [p for p in phrases if p not in text[name]] for name, phrases in expected.items()} == {
        name: [] for name in expected
    }
    assert [n.procedure_id for n in plan.procedure_notes] == [i.procedure_id for i in plan.items]
    assert [(m.due, m.milestone.split(",")[0]) for m in plan.timetable] == [
        (date(2026, 10, 19), "Fieldwork starts"),
        (date(2026, 10, 22), "Fieldwork completed"),
        (date(2026, 10, 23), "Auditor's report dated"),
        (date(2027, 1, 31), "Financial report and auditor's report given to members (s315(4))"),
    ]
    assert plan.eqr_recommendation.required is False
    assert all(p in plan.eqr_recommendation.basis for p in ("not listed", "law or regulation", "ASQM 1 para 34(f)"))


@pytest.mark.parametrize("fixture", [XERO_PLAN, XERO_PLAN_R2], ids=["r1", "r2"])
@needs("q1_snapshot", "q1_supplements")
async def test_plan_fixtures_cover_the_demo_procedures(fixture: Path) -> None:
    """守卫 (需求 R316): 演示配置 (.env.example 的 FEATURES) 下 Xero 装配出的 P2 程序, 都在 PLN-15 固定输出 r1, r2 的
    程序说明里. 防的是演示配置打开新功能后 (如关联方, 持续经营的程序接入时, 需求 R147, R192) 固定输出缺这些程序的说明,
    计划叙述校验不过, PLN-15 落为失败任务, 演示停在计划审批前; 接入时在 r1, r2 补说明 (多出的说明不收进计划, 不必删)."""
    ctx, _, _ = await plan_context(features=DEMO_FEATURES)
    assembled = {p.id for p in ctx.state["procedure_pack"] if p.phase is Phase.P2_Executing}
    noted = {n["procedure_id"] for n in json.loads(fixture.read_text(encoding="utf-8"))["procedure_notes"]}
    assert assembled and sorted(assembled - noted) == []


@needs("q1_snapshot", "q1_supplements")
@pytest.mark.parametrize(
    ("reply", "level"),
    [
        ("not json", "syntax"),
        ('{"overall_strategy": "only"}', "structure"),
        (plan_raw(standards_cited=["ASA315-P12"]), "reference"),
        (plan_raw(timetable=[{"milestone": "Planning", "due": "2026-09-01"}]), "semantic"),
    ],
    ids=["syntax", "structure", "reference", "semantic"],
)
async def test_pln15_failed_check_fails_the_run(reply: str, level: str) -> None:
    """验收 4, 5: 任一级不过都回传重试 (引用与语义级自需求 R348 起), 至 3 次仍失败: A4 抛 ValueError (含失败的级别),
    经 A1 的 run_agent 转为 RUN-PLN-15-1 failed 与 T-RUN-PLN-15-1-v1 (failed, 只允许 rerun), 不写计划."""
    ctx, deps, trail = await plan_context(ScriptedLLM({"r1": [reply] * MAX_ATTEMPTS}))
    agent = RiskAssessorAgent(deps)
    patch, writer, _ = await GraphNodes(deps, 2).run_agent(agent, ctx)
    assert writer is AgentId.A1 and "audit_plans" not in patch
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-PLN-15-1", RunStatus.failed)
    assert run.failure_reason is not None and run.failure_reason.startswith(
        f"ValueError: PLN-15 第 1 修订未通过 {level}"
    )
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, task.subject_id) == ("T-RUN-PLN-15-1-v1", TaskKind.failed, None)
    assert deps.queue.allowed(task) == frozenset({Decision.rerun})
    assert [e.payload["attempt"] for e in llm_calls(trail)] == list(range(1, MAX_ATTEMPTS + 1))


@needs("q1_snapshot", "q1_supplements")
async def test_pln15_revision_answers_the_rejection() -> None:
    """验收 6: 状态有 PLAN-1 v1 (第 1 次执行) 与 PLN-15 的驳回 (对别的计划 PLAN-9 一条, 对 PLAN-1 两条, 最近一条为
    D-P15R): RUN-PLN-15-2 出 PLAN-2 v1 draft (revision 2, 回应 D-P15R, 理由取其 reason), PLAN-1 出 v2 superseded (记本次
    执行, 其余不变); 请求的 fixture_key 为 r2, 提示词含 "## Revision 2" 与该理由. 上一修订已是 superseded 时只写新修订.
    只有针对别的计划的驳回时抛 LookupError, 不起草. 第 2 修订的正本 r2.json (需求 R316 验收 3) 经真实校验一次通过:
    timing 段与时间表写明两份银行询证函随外勤开始于 10-19 发出, 外勤结束 10-22 前收回, 都不早于期末; 其余各段与
    时间表以外的字段同 r1."""
    first_ctx, first_deps, _ = await plan_context()
    first = await drafted(first_ctx, first_deps)
    [plan1], [run1] = first["audit_plans"], first["procedure_runs"]
    other = plan_reject("PLAN-9", "Not this plan", "D-X")
    earlier = plan_reject(plan1.id, "An earlier reason", "D-A")
    gates = [*first_ctx.state["gate_decisions"], other, earlier, plan_reject(plan1.id)]
    llm = ScriptedLLM({"r2": [XERO_PLAN_R2.read_text(encoding="utf-8")] * 2})
    ctx, deps, trail = await plan_context(llm, audit_plans=[plan1], procedure_runs=[run1], gate_decisions=gates)
    patch = await drafted(ctx, deps)
    plan2, retired = patch["audit_plans"]
    assert [e.payload["level_failed"] for e in llm_calls(trail)] == [None]
    timing = next(s.text for s in plan2.narrative if s.section == "timing")
    assert all(p in timing for p in ("19 October 2026", "two bank confirmation requests", "22 October 2026"))
    assert [(m.due, m.milestone.split(" (")[0]) for m in plan2.timetable if "Bank confirmation" in m.milestone] == [
        (date(2026, 10, 19), "Bank confirmation requests sent for both accounts"),
        (date(2026, 10, 22), "Bank confirmation replies received, before fieldwork is completed"),
    ]
    r1_json, r2_json = (json.loads(p.read_text(encoding="utf-8")) for p in (XERO_PLAN, XERO_PLAN_R2))
    del r1_json["sections"]["timing"]
    del r2_json["sections"]["timing"]
    assert {k: v for k, v in r1_json.items() if k != "timetable"} == {
        k: v for k, v in r2_json.items() if k != "timetable"
    }
    assert (plan2.id, plan2.version, plan2.status, plan2.revision, plan2.run_id) == (
        "PLAN-2",
        1,
        PlanStatus.draft,
        2,
        "RUN-PLN-15-2",
    )
    assert (plan2.addresses_decision_id, plan2.revision_reason) == (PLAN_REJECT_ID, PLAN_REJECT_REASON)
    assert (retired.id, retired.version, retired.status, retired.run_id) == (
        "PLAN-1",
        2,
        PlanStatus.superseded,
        "RUN-PLN-15-2",
    )
    unchanged = {"version", "status", "run_id"}
    assert retired.model_dump(exclude=unchanged) == plan1.model_dump(exclude=unchanged)
    [request] = llm.requests
    prompt = "\n".join(m.content for m in request.messages)
    assert request.fixture_key == "r2" and "## Revision 2" in prompt and PLAN_REJECT_REASON in prompt

    ctx, deps, _ = await plan_context(llm, audit_plans=[retired], procedure_runs=[run1], gate_decisions=gates)
    assert [p.id for p in (await drafted(ctx, deps))["audit_plans"]] == ["PLAN-2"]

    gates = [*first_ctx.state["gate_decisions"], other]
    ctx, deps, _ = await plan_context(llm, audit_plans=[plan1], procedure_runs=[run1], gate_decisions=gates)
    with pytest.raises(LookupError, match="PLN-15 对 PLAN-1 的驳回"):
        await RiskAssessorAgent(deps).run(ctx)


@needs("q1_snapshot", "q1_supplements")
@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"gate_decisions": []}, "PLN-01/03 通过时选定的重要性"),
        ({"materiality_cents": None}, "PLN-01/03 确定的重要性三项"),  # P2 程序的参数不引用它, 由骨架报出
    ],
)
async def test_pln15_requires_decided_materiality(state: dict[str, Any], message: str) -> None:
    """没有带重要性的 PLN-01/03 通过决定, 或状态中缺重要性三项之一时抛 ValueError (由 A1 按执行失败处理), 不调大模型."""
    llm = ScriptedLLM({})
    ctx, deps, _ = await plan_context(llm, **state)
    with pytest.raises(ValueError, match=message):
        await RiskAssessorAgent(deps).run(ctx)
    assert llm.requests == []


def test_planning_off_keeps_a4_to_its_two_programs() -> None:
    """验收 9: 功能 planning 关闭 (基线) 时注册表不映射 PLN-15, A4 只负责 PLN-07, PLN-01; 打开时 PLN-15 也归 A4."""
    off = build_agents(make_deps(FakeRegistry(), FakeTrail()))
    assert sorted(pid for pid, a in off.items() if isinstance(a, RiskAssessorAgent)) == ["PLN-01", "PLN-07"]
    on = build_agents(make_deps(FakeRegistry(), FakeTrail(), features=PLAN_FEATURES))
    assert sorted(pid for pid, a in on.items() if isinstance(a, RiskAssessorAgent)) == ["PLN-01", "PLN-07", "PLN-15"]
