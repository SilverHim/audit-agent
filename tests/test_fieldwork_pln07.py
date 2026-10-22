"""需求 R371: PLN-07 初步分析的三步 (大模型选基准与比例 → 规则算命中 → 大模型复筛).

功能 planning 打开时走混合流程, 关闭时保持原来的纯规则结果. 本模块核对:
- Numbat 纯规则结果不变, 混合流程仍以应收账款 1100 为重点关注;
- Xero 演示 (亏损) 选收入与 1%, 9 个命中科目各有结论, 含 711 的补充观察;
- 大模型第 1 步或第 3 步失败都退回本期收入 × 0.5% 的规则结果并标 fallback;
- 两个输出模型的引用与语义校验, 以及旧数据读回的空缺省;
- 第 2 步的命中能与 A01 口径的独立复算对上.
"""

import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.base import RunContext
from backend.agents.risk_assessor import RiskAssessorAgent
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import (
    AnalyticsAdditionDraft,
    AnalyticsBasisDraft,
    AnalyticsScreeningDecision,
    AnalyticsScreeningDraft,
)
from backend.contracts.models import (
    AnalyticsResult,
    AnalyticsScreening,
    Procedure,
    ProcedureRun,
    RunStatus,
    RunTrigger,
)
from backend.contracts.ports import LLMResponse
from backend.contracts.records import Account, DatasetTraits, TrialBalanceRow
from backend.contracts.services import ValidationContext
from backend.contracts.state import initial_state
from backend.services.analytic_benchmarks import load_benchmarks
from backend.services.procedure_library import run_params
from tests.fakes import (
    ENGAGEMENT,
    KNOWLEDGE,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_PROFILE,
    FakeFinancialStore,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    make_deps,
)
from tests.xero_fakes import (
    DEMO_FEATURES,
    XERO_A01_CHANGES,
    XERO_RULES,
    make_xero_deps,
    xero_engagement,
    xero_financial_store,
)

REPO = Path(__file__).resolve().parents[1]
LLM = REPO / "tests" / "fixtures" / "llm"
LLM_XERO = REPO / "tests" / "fixtures" / "llm_xero"
NUMBAT_PLANNING = frozenset({"critic", "planning"})
NUMBAT_BASELINE = frozenset({"critic"})
PERIOD_PRIOR = date(2024, 7, 1)
# Numbat 的第 2 步命中: 只有 1100 (合成数据集设计第 3.1 节); 期初为 150,000,000 分, 变动 52,500,000 分
NUMBAT_HIT_AMOUNT = 52_500_000

# 复算 A01 用的 Numbat 试算表: 只放本用例需要的科目, 分类齐全, 金额同设计第 3.1 节
NUMBAT_ANALYTIC_ACCOUNTS = [
    Account(code="1100", name="Trade receivables", type="asset", category="receivables"),
    Account(code="3200", name="Retained earnings", type="equity", category="retained_earnings"),
    Account(code="4100", name="Sales revenue", type="revenue", category="revenue"),
    Account(code="5100", name="Cost of goods sold", type="expense", category="cost_of_sales"),
]
NUMBAT_ANALYTIC_ROWS = [
    TrialBalanceRow(period="current", account_code="1100", balance_cents=202_500_000),
    TrialBalanceRow(period="prior", account_code="1100", balance_cents=150_000_000),
    TrialBalanceRow(period="current", account_code="4100", balance_cents=-1_200_000_000),
    TrialBalanceRow(period="prior", account_code="4100", balance_cents=-1_040_000_000),
    TrialBalanceRow(period="current", account_code="5100", balance_cents=840_000_000),
    TrialBalanceRow(period="prior", account_code="5100", balance_cents=722_800_000),
    TrialBalanceRow(period="current", account_code="3200", balance_cents=-376_100_000),
    TrialBalanceRow(period="prior", account_code="3200", balance_cents=-326_920_000),
]


def numbat_analytics_store() -> FakeFinancialStore:
    """带 PLn-07 第 1 步所需两期试算表与证据行的 Numbat 假存储."""
    records = {
        ("trial_balance", row.account_code): row.model_dump() for row in NUMBAT_ANALYTIC_ROWS if row.period == "current"
    }
    return FakeFinancialStore(trial_balance=NUMBAT_ANALYTIC_ROWS, accounts=NUMBAT_ANALYTIC_ACCOUNTS, records=records)


def numbat_deps(
    financial: FakeFinancialStore | None = None,
    llm: Any = None,
    features: frozenset[str] = NUMBAT_PLANNING,
) -> Any:
    """Numbat 的依赖: 规则结果用 NUMBAT_RULES 的缺省假引擎, 大模型按参数 (缺省读 tests/fixtures/llm)."""
    return make_deps(
        FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC}),
        FakeTrail(),
        financial_store=financial or numbat_analytics_store(),
        llm=llm if llm is not None else MockLLMClient(LLM),
        features=features,
    )


async def context(deps: Any, engagement: Any = ENGAGEMENT, prior_start: date | None = PERIOD_PRIOR) -> RunContext:
    """PLN-07 第 1 次执行的上下文, 参数按程序模板解析 (含上期起始日)."""
    template: ProcedureTemplate = deps.library.load("PLN-07")
    procedure = Procedure(
        id="PLN-07",
        phase=template.phase,
        template_commit=deps.library.commit,
        autonomy_level=template.autonomy_level,
    )
    state = initial_state(engagement, [procedure], engagement.client_profile_version)
    profile = await deps.engagement_store.client_profile(engagement.client_id, engagement.client_profile_version)
    traits = deps.datasets.dataset_traits(engagement.dataset) if engagement.dataset else DatasetTraits()
    if prior_start is not None:
        traits = traits.model_copy(update={"prior_period_start": prior_start})
    params = run_params(deps.library, template, procedure, engagement, state, traits=traits, profile=profile)
    run = ProcedureRun(
        id="RUN-PLN-07-1",
        procedure_id="PLN-07",
        run_no=1,
        trigger=RunTrigger.initial,
        params_snapshot=params,
        started_at=datetime.now(UTC),
        status=RunStatus.running,
    )
    return RunContext(engagement=engagement, procedure=procedure, template=template, run=run, state=state)


def fixture_keys(trail: FakeTrail) -> list[str]:
    """本模块关心的大模型固定输出的键, 按调用顺序."""
    return [str(entry.payload["fixture_key"]) for entry in trail.entries if entry.kind.value == "llm_call"]


# ── 纯规则: 关闭 planning 时与原来一样 ──


async def test_numbat_pure_rule_analytics_unchanged_without_planning() -> None:
    """功能 planning 关闭: 不调大模型, 分析结果与新增字段的空缺省, 例外仍只有 1100."""
    deps = numbat_deps(features=NUMBAT_BASELINE)
    agent = RiskAssessorAgent(deps)

    patch = await agent.run(await context(deps))
    analytics = patch["analytics"]

    assert (analytics.id, analytics.version) == ("ANALYTICS", 1)
    assert analytics.ratios == {"gross_margin_current": 0.3, "gross_margin_prior": 0.305, "revenue_growth": 0.1538}
    assert [x.id for x in patch["exceptions"]] == ["X-PLN-07-1-A01-1"]
    assert analytics.exception_ids == ["X-PLN-07-1-A01-1"]
    assert analytics.recommendation is None
    assert analytics.screening == AnalyticsScreening()
    assert (analytics.fallback, analytics.fallback_reason) == (False, None)
    assert fixture_keys(deps.trail) == []
    assert deps.rules.calls[0][2]["min_change_revenue_pct"] == 0.005


async def test_old_analytics_json_reads_back_with_empty_hybrid_fields() -> None:
    """旧数据没有新增键: recommendation 为空, screening 为空, fallback 为假 (需求 R371 验收 5)."""
    analytics = AnalyticsResult.model_validate(
        {"id": "ANALYTICS", "version": 1, "ratios": {}, "exception_ids": [], "monthly": []}
    )

    assert analytics.recommendation is None
    assert analytics.screening == AnalyticsScreening()
    assert analytics.screening.focus == [] and analytics.screening.additions == []
    assert analytics.screening.excluded == [] and analytics.screening.observations == []
    assert analytics.fallback is False and analytics.fallback_reason is None


# ── Numbat 混合流程: 1100 仍是重点关注, 其余结果不变 ──


async def test_numbat_hybrid_focuses_receivables_and_keeps_rule_results() -> None:
    """功能 planning 打开: 用 revenue 1% 的固定输出, 1100 仍是重点关注, 比率/月度/例外与纯规则相同."""
    deps = numbat_deps()
    agent = RiskAssessorAgent(deps)

    patch = await agent.run(await context(deps))
    analytics = patch["analytics"]
    screening = analytics.screening

    assert (analytics.fallback, analytics.fallback_reason) == (False, None)
    assert analytics.recommendation is not None
    assert (
        analytics.recommendation.basis,
        analytics.recommendation.base_amount_cents,
        analytics.recommendation.percentage,
        analytics.recommendation.threshold_cents,
    ) == ("revenue", 1_200_000_000, 0.01, 12_000_000)
    assert screening.hit_account_codes == ["1100"]
    assert [(item.account_code, item.amount_cents) for item in screening.focus] == [("1100", NUMBAT_HIT_AMOUNT)]
    assert screening.additions == [] and screening.excluded == [] and screening.observations == []
    assert "35.00%" in screening.focus[0].reason
    assert (screening.focus[0].evidence[0].source_type, screening.focus[0].evidence[0].source_id) == (
        "trial_balance",
        "1100",
    )
    assert analytics.ratios == {"gross_margin_current": 0.3, "gross_margin_prior": 0.305, "revenue_growth": 0.1538}
    assert [x.id for x in patch["exceptions"]] == ["X-PLN-07-1-A01-1"]
    assert analytics.exception_ids == ["X-PLN-07-1-A01-1"]
    assert fixture_keys(deps.trail) == ["basis", "screen"]
    # 第 2 步用大模型选的 1% 重跑 A01; 第 1 次是缺省 0.5%
    assert [call[2]["min_change_revenue_pct"] for call in deps.rules.calls] == [0.005, 0.01]


def test_analytic_benchmarks_config_marks_the_a8_examples_and_conventions() -> None:
    """配置注明出处: A8 只给税前利润 5%, 收入或总费用 1% 三个例子, 其余范围标惯例 (需求 R371 验收 1)."""
    config = load_benchmarks(KNOWLEDGE)

    examples = {
        benchmark.basis: benchmark.example_pct for benchmark in config.bases if benchmark.example_pct is not None
    }
    assert examples == {"revenue": 0.01, "profit_before_tax": 0.05, "total_expenses": 0.01}
    assert all(benchmark.convention for benchmark in config.bases)
    revenue = config.get("revenue")
    assert revenue is not None and revenue.contains(0.01) and not revenue.contains(0.0101)
    assert "A8" in config.source and "A4" in config.source


# ── Xero 演示: 亏损选收入, 9 个命中各有结论, 含 711 的补充观察 ──


def recalculate_a01(
    financial: FakeFinancialStore,
    threshold_cents: int,
    *,
    include_profit_or_loss: bool = False,
    change_pct: float = 0.2,
) -> list[tuple[str, int]]:
    """按 PLN-07 A01 的口径独立复算命中: 资产负债类对期初, 损益类只在两期等长时比较; 变动率与门槛同 A01.

    期初 = 上期末余额; 留存收益 (category retained_earnings 的代码最小科目) 的期初另加上期全部损益类余额. 返回按科目
    代码排序的 (代码, 变动金额).
    """
    accounts = {account.code: account for account in financial.account_rows}
    current = {row.account_code: row.balance_cents for row in financial.trial_balance_rows if row.period == "current"}
    prior = {row.account_code: row.balance_cents for row in financial.trial_balance_rows if row.period == "prior"}
    carried = sum(prior.get(code, 0) for code, account in accounts.items() if account.type in ("revenue", "expense"))
    retained = min(
        (code for code, account in accounts.items() if account.category == "retained_earnings"), default=None
    )
    hits: list[tuple[str, int]] = []
    for code in sorted(set(current) | set(prior)):
        account = accounts.get(code)
        if account is None:
            continue
        if account.type in ("revenue", "expense") and not include_profit_or_loss:
            continue
        opening = prior.get(code, 0) + (carried if code == retained else 0)
        change = current.get(code, 0) - opening
        if change != 0 and abs(change) >= threshold_cents and abs(change) >= change_pct * abs(opening):
            hits.append((code, change))
    return hits


def xero_deps() -> tuple[Any, FakeFinancialStore]:
    """演示配置的 Xero 依赖: 规则结果是连库跑出的 XERO_RULES, 大模型读正本 fixture."""
    financial = xero_financial_store()
    deps = make_xero_deps(
        financial_store=financial,
        rules=FakeRuleEngine(XERO_RULES),
        llm=MockLLMClient(LLM_XERO),
        features=DEMO_FEATURES,
    )
    return deps, financial


@pytest.mark.external
async def test_xero_hybrid_picks_revenue_one_percent_and_screens_all_nine_hits() -> None:
    """验收 7: 亏损时按 ASA 320 A5 选收入, 1%; 9 个命中各有结论, 840/091/090 与现金存在性关联, 711 有补充观察."""
    deps, financial = xero_deps()
    agent = RiskAssessorAgent(deps)

    patch = await agent.run(await context(deps, xero_engagement(), prior_start=date(2025, 7, 1)))
    analytics = patch["analytics"]
    screening = analytics.screening

    assert (analytics.fallback, analytics.fallback_reason) == (False, None)
    assert analytics.recommendation is not None
    recommendation = analytics.recommendation
    assert (recommendation.basis, recommendation.percentage) == ("revenue", 0.01)
    assert (recommendation.base_amount_cents, recommendation.threshold_cents) == (3_243_100, 32_431)
    assert "A5" in recommendation.rationale and "A8" in recommendation.rationale
    assert 0 < recommendation.self_confidence <= 1

    assert screening.hit_account_codes == ["090", "091", "610", "710", "711", "800", "820", "825", "840"]
    assert [item.account_code for item in screening.focus] == ["090", "091", "610", "710", "711", "800", "840"]
    assert [item.account_code for item in screening.excluded] == ["820", "825"]
    assert len(screening.focus) + len(screening.excluded) == 9
    assert all(item.reason.strip() for item in [*screening.focus, *screening.excluded])
    assert {item.account_code for item in screening.focus if item.account_code in {"090", "091", "840"}} == {
        "090",
        "091",
        "840",
    }
    assert [item.account_code for item in screening.additions] == ["960"]
    [observation] = screening.observations
    assert observation.account_codes == ["710", "711"]
    assert "825.00" in observation.reason and "750.00" in observation.reason
    assert len(analytics.exception_ids) == 9
    assert fixture_keys(deps.trail) == ["basis", "screen"]
    assert deps.rules.calls[-1][2]["min_change_revenue_pct"] == 0.01

    # 第 2 步的命中与 A01 口径的独立复算一致 (不依赖规则引擎给出的 9 条固定输出)
    computed = recalculate_a01(financial, 32_431)
    by_code = {exception.source_ids[0]: exception.amount_cents for exception in patch["exceptions"]}
    assert computed == [(code, by_code[code]) for code, _ in computed]
    assert [code for code, _ in computed] == screening.hit_account_codes
    # 用缺省 0.5% 的阈值复算时与 XERO_RULES 的 9 条固定输出也一致 (规则 SQL 的既有口径)
    expected = [(code, int(current) - int(opening)) for code, _, current, _, opening, _ in XERO_A01_CHANGES]
    assert recalculate_a01(financial, 16_215, include_profit_or_loss=False) == expected


# ── 退回规则: 第 1 步或第 3 步失败都退回收入 × 0.5% ──


async def test_basis_failure_falls_back_to_revenue_half_percent() -> None:
    """第 1 步固定输出缺失, 三次尝试后重试用尽: 退回原规则, 标 fallback, 保留 1100."""
    deps = numbat_deps(llm=MockLLMClient(REPO / "tests" / "fixtures" / "no-such-llm"))
    agent = RiskAssessorAgent(deps)

    patch = await agent.run(await context(deps))
    analytics = patch["analytics"]

    assert (analytics.fallback, analytics.fallback_reason is not None) == (True, True)
    assert analytics.fallback_reason is not None and analytics.fallback_reason.startswith("Step 1")
    assert analytics.recommendation is None
    assert analytics.screening.threshold_cents == 6_000_000  # 1,200,000,000 × 0.5%
    assert analytics.screening.hit_account_codes == ["1100"]
    assert [item.account_code for item in analytics.screening.focus] == ["1100"]
    assert analytics.screening.additions == [] and analytics.screening.excluded == []
    assert [x.id for x in patch["exceptions"]] == ["X-PLN-07-1-A01-1"]
    assert fixture_keys(deps.trail) == ["basis"] * 3
    assert len(deps.rules.calls) == 1  # 没有用大模型的基准重跑


async def test_screen_failure_falls_back_to_original_rule(tmp_path: Path) -> None:
    """第 3 步固定输出缺失: 即使第 1 步成功也退回原规则, 门槛回到收入 × 0.5%."""
    root = tmp_path / "llm"
    (root / "PLN-07").mkdir(parents=True)
    shutil.copy(LLM / "PLN-07" / "basis.json", root / "PLN-07" / "basis.json")
    deps = numbat_deps(llm=MockLLMClient(root))
    agent = RiskAssessorAgent(deps)

    patch = await agent.run(await context(deps))
    analytics = patch["analytics"]

    assert analytics.fallback is True
    assert analytics.fallback_reason is not None and analytics.fallback_reason.startswith("Step 3")
    assert analytics.recommendation is None
    assert analytics.screening.threshold_cents == 6_000_000
    assert [item.account_code for item in analytics.screening.focus] == ["1100"]
    assert fixture_keys(deps.trail) == ["basis", *(["screen"] * 3)]  # 第 3 步三次尝试后重试用尽
    assert [call[2]["min_change_revenue_pct"] for call in deps.rules.calls] == [0.005, 0.01]  # 第 2 步已重跑


# ── 输出模型的引用与语义两级校验 ──


async def basis_report(deps: Any, draft: AnalyticsBasisDraft, template: ProcedureTemplate | None = None) -> Any:
    """按 PLN-07 第 1 步的输出模型校验一份草稿, 返回 ValidationReport."""
    model = template or deps.library.load("PLN-07")
    checked = model.model_copy(update={"llm": LLMSpec(prompt="PLN-07-basis.md.j2", output_model="AnalyticsBasisDraft")})
    response = LLMResponse[AnalyticsBasisDraft](
        output=draft,
        raw=draft.model_dump_json(),
        model="test",
        input_tokens=0,
        output_tokens=0,
    )
    return await deps.judgments.validator.validate(
        ENGAGEMENT.id,
        checked,
        response,
        ValidationContext(standards_of="PLN-07", client_profile=NUMBAT_PROFILE),
    )


async def screening_report(deps: Any, draft: AnalyticsScreeningDraft) -> Any:
    """按 PLN-07 第 3 步的输出模型校验一份草稿, 命中为 1100."""
    template = deps.library.load("PLN-07").model_copy(
        update={"llm": LLMSpec(prompt="PLN-07-screen.md.j2", output_model="AnalyticsScreeningDraft")}
    )
    response = LLMResponse[AnalyticsScreeningDraft](
        output=draft,
        raw=draft.model_dump_json(),
        model="test",
        input_tokens=0,
        output_tokens=0,
    )
    return await deps.judgments.validator.validate(
        ENGAGEMENT.id,
        template,
        response,
        ValidationContext(
            standards_of="PLN-07",
            client_profile=NUMBAT_PROFILE,
            analytics_hits=["1100"],
        ),
    )


def evidence(*source_ids: str) -> list[dict[str, Any]]:
    """指向试算表行的证据指针 (Numbat 假存储的当前行)."""
    return [
        {
            "type": "voucher",
            "source_type": "trial_balance",
            "source_id": source_id,
            "locator": "balance_cents",
            "reliability": "internal_original",
        }
        for source_id in source_ids
    ]


async def test_basis_and_screening_fixtures_pass_validation() -> None:
    """两份 Numbat 固定输出本身通过引用与语义校验."""
    deps = numbat_deps()
    basis = AnalyticsBasisDraft.model_validate_json((LLM / "PLN-07" / "basis.json").read_text(encoding="utf-8"))
    screen = AnalyticsScreeningDraft.model_validate_json((LLM / "PLN-07" / "screen.json").read_text(encoding="utf-8"))

    basis_result = await basis_report(deps, basis)
    screen_result = await screening_report(deps, screen)

    assert basis_result.level_failed is None and screen_result.level_failed is None
    assert basis_result.output is not None and screen_result.output is not None


async def test_basis_rejects_percentage_outside_the_benchmark_range() -> None:
    """比例超出该基准范围判语义失败 (需求 R371 验收 1)."""
    deps = numbat_deps()
    draft = AnalyticsBasisDraft(
        basis="revenue",
        percentage=0.5,
        rationale="revenue is stable",
        evidence=evidence("4100"),  # type: ignore[arg-type]
        counter_argument="profit before tax could be used",
        self_confidence=0.6,
    )

    report = await basis_report(deps, draft)

    assert report.level_failed == "semantic"
    assert any("不在" in error and "范围" in error for error in report.errors)


async def test_basis_rejects_non_positive_benchmark() -> None:
    """税前亏损作基准判语义失败 (ASA 320 A5, 需求 R371 验收 1)."""
    accounts = [
        NUMBAT_ANALYTIC_ACCOUNTS[2],
        NUMBAT_ANALYTIC_ACCOUNTS[3],
    ]
    rows = [
        TrialBalanceRow(period="current", account_code="4100", balance_cents=-100_000),
        TrialBalanceRow(period="current", account_code="5100", balance_cents=500_000),
    ]
    financial = FakeFinancialStore(
        trial_balance=rows,
        accounts=accounts,
        records={("trial_balance", "4100"): rows[0].model_dump()},
    )
    deps = numbat_deps(financial=financial)
    draft = AnalyticsBasisDraft(
        basis="profit_before_tax",
        percentage=0.05,
        rationale="profit before tax",
        evidence=evidence("4100"),  # type: ignore[arg-type]
        counter_argument="revenue could be used",
        self_confidence=0.6,
    )

    report = await basis_report(deps, draft)

    assert report.level_failed == "semantic"
    assert any("不为正" in error for error in report.errors)


async def test_basis_rejects_unresolvable_evidence_pointer() -> None:
    """证据指针解析不了判引用失败 (需求 R371 验收 1)."""
    deps = numbat_deps()
    draft = AnalyticsBasisDraft(
        basis="revenue",
        percentage=0.01,
        rationale="revenue is stable",
        evidence=evidence("9999"),  # type: ignore[arg-type]
        counter_argument="profit before tax could be used",
        self_confidence=0.6,
    )

    report = await basis_report(deps, draft)

    assert report.level_failed == "reference"
    assert any("9999" in error for error in report.errors)


async def test_screening_requires_exactly_one_decision_per_hit() -> None:
    """少一条或多一条 decision 都判语义失败 (需求 R371 验收 3)."""
    deps = numbat_deps()
    empty = await screening_report(deps, AnalyticsScreeningDraft(decisions=[]))
    assert empty.level_failed == "semantic"
    assert any("没有 decision" in error for error in empty.errors)

    extra = AnalyticsScreeningDraft(
        decisions=[
            AnalyticsScreeningDecision(
                account_code="1100",
                verdict="focus",
                reason="large increase",
                risk_or_assertion="Existence of receivables",
                evidence=evidence("1100"),  # type: ignore[arg-type]
            ),
            AnalyticsScreeningDecision(
                account_code="4100",
                verdict="exclude",
                reason="not a rule hit",
                risk_or_assertion="Revenue occurrence",
                evidence=evidence("4100"),  # type: ignore[arg-type]
            ),
        ]
    )
    report = await screening_report(deps, extra)
    assert report.level_failed == "semantic"
    assert any("不是规则命中" in error for error in report.errors)


async def test_screening_rejects_addition_that_is_a_rule_hit() -> None:
    """additions 不能是规则命中的科目, 也不能重复 (需求 R371 验收 3)."""
    deps = numbat_deps()
    draft = AnalyticsScreeningDraft(
        decisions=[
            AnalyticsScreeningDecision(
                account_code="1100",
                verdict="focus",
                reason="large increase",
                risk_or_assertion="Existence of receivables",
                evidence=evidence("1100"),  # type: ignore[arg-type]
            )
        ],
        additions=[
            AnalyticsAdditionDraft(
                account_code="1100",
                reason="already a hit",
                risk_or_assertion="Existence of receivables",
            )
        ],
    )

    report = await screening_report(deps, draft)

    assert report.level_failed == "semantic"
    assert any("additions 不能是规则命中" in error for error in report.errors)


async def test_screening_rejects_unresolvable_evidence() -> None:
    """decision 的证据指针解析不了判引用失败; 没有证据也判引用失败 (需求 R371 验收 3)."""
    deps = numbat_deps()
    draft = AnalyticsScreeningDraft(
        decisions=[
            AnalyticsScreeningDecision(
                account_code="1100",
                verdict="focus",
                reason="large increase",
                risk_or_assertion="Existence of receivables",
                evidence=evidence("9999"),  # type: ignore[arg-type]
            )
        ]
    )

    report = await screening_report(deps, draft)

    assert report.level_failed == "reference"
    assert any("9999" in error for error in report.errors)
