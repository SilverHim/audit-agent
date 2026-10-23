"""A4: PLN-07, PLN-01, PLN-15 (功能 planning, 见 agents/audit_plan).

返回字段: analytics, materiality_candidates, audit_objectives, risk_register, exceptions, audit_plans, procedure_runs.

PLN-07 在功能 planning 打开时走三步 (需求 R371): 大模型选基准与比例, 规则按 基准 × 比例 与 20% 变动率算命中科目, 大模型
逐个复筛并可补充. 关闭时与原来的纯规则行为相同. 第一步的推荐只作 PLN-01/03 闸门的预选; 审计师在闸门改选后不自动重算,
按现有流程把 PLN-07 当过期或在下次重算 (rerun) 时用新的选择重跑.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar, cast

from backend.agents.audit_plan import draft_plans, period_months, plan_patch
from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import AnalyticsBasisDraft, AnalyticsScreeningDraft
from backend.contracts.models import (
    AgentId,
    AnalyticsAddition,
    AnalyticsObservation,
    AnalyticsRecommendation,
    AnalyticsResult,
    AnalyticsScreening,
    AnalyticsScreeningItem,
    AuditPlan,
    ClientProfile,
    MaterialityCandidate,
    MaterialityCandidates,
    MaterialityInapplicable,
    MonthlyFigures,
)
from backend.contracts.services import RuleResult, ValidationContext
from backend.contracts.state import StatePatch
from backend.services.analytic_benchmarks import (
    COST_OF_SALES_CATEGORIES,
    INCOME_TAX_CATEGORIES,
    REVENUE_CATEGORIES,
    effective_revenue_percentage,
    load_benchmarks,
    trial_balance_rows,
    trial_balance_table,
)
from backend.services.analytic_benchmarks import totals_from_rows as sum_totals
from backend.services.features import PLANNING

RATIO_PLACES = Decimal("0.0001")  # 比率保留四位小数
BASIS_PROMPT = "PLN-07-basis.md.j2"  # 第 1 步的提示词
SCREEN_PROMPT = "PLN-07-screen.md.j2"  # 第 3 步的提示词
BASIS_MODEL = "AnalyticsBasisDraft"  # 第 1 步的输出模型
SCREEN_MODEL = "AnalyticsScreeningDraft"  # 第 3 步的输出模型


def to_cents(value: Any) -> int | None:
    """SQL 取出的金额转整数分: SUM 可能是 Decimal, 没有行时为 NULL (None)."""
    return None if value is None else int(value)


def ratio(numerator: int, denominator: int) -> float:
    """numerator / denominator, 四舍五入到四位小数."""
    return float((Decimal(numerator) / Decimal(denominator)).quantize(RATIO_PLACES, rounding=ROUND_HALF_UP))


def analytics_ratios(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """按 PLN-07 A02 的行 (period, revenue_cents, cost_of_sales_cents) 算比率.

    gross_margin_current, gross_margin_prior = (收入 - 销售成本) / 收入; revenue_growth = 本期收入 / 上期收入 - 1.
    缺某期的行或该期收入为 0 时, 用到该期收入作分母的比率不出.
    """
    by_period = {r["period"]: r for r in rows}
    revenue = {p: to_cents(r.get("revenue_cents")) for p, r in by_period.items()}
    ratios: dict[str, float] = {}
    for period in ("current", "prior"):
        rev = revenue.get(period)
        cost = to_cents(by_period[period].get("cost_of_sales_cents")) if period in by_period else None
        if rev and cost is not None:
            ratios[f"gross_margin_{period}"] = ratio(rev - cost, rev)
    current, prior = revenue.get("current"), revenue.get("prior")
    if current is not None and prior:
        ratios["revenue_growth"] = ratio(current - prior, prior)
    return ratios


def monthly_figures(rows: Sequence[Mapping[str, Any]]) -> list[MonthlyFigures]:
    """按 PLN-07 A03 的行 (month, revenue_cents, expense_cents, employee_expense_cents) 出月度表, 按月升序;
    金额为空记 0."""
    figures = [
        MonthlyFigures(
            month=r["month"],
            revenue_cents=to_cents(r.get("revenue_cents")) or 0,
            expense_cents=to_cents(r.get("expense_cents")) or 0,
            employee_expense_cents=to_cents(r.get("employee_expense_cents")) or 0,
        )
        for r in rows
    ]
    return sorted(figures, key=lambda f: f.month)


def scaled(base_cents: int, pct: float) -> int:
    """基准金额 × 比例, 用 Decimal 四舍五入到整数分. 比例经 str 转换, 0.005 不带二进制误差."""
    return int((Decimal(base_cents) * Decimal(str(pct))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def inapplicable(basis: str, base_cents: int) -> MaterialityInapplicable | None:
    """候选本期不适用的原因 (需求 R351): 基准金额为正时为空; 税前利润为负记 loss, 其余不为正的记 not_positive.
    loss 依据 ASA 320 第 A5 段 (税前利润波动时宜改用毛利或收入等其他基准); not_positive: 不为正的基准乘比例得不出
    重要性."""
    if base_cents > 0:
        return None
    return "loss" if basis == "profit_before_tax" and base_cents < 0 else "not_positive"


def materiality_candidates(
    rows: Sequence[Mapping[str, Any]], params: Mapping[str, Any]
) -> tuple[list[MaterialityCandidate], tuple[float, float], float]:
    """按 PLN-01 M01 的行 (basis, base_amount_cents) 与参数算候选, 顺序同 params.candidates. 基准金额不为正的
    照常列出, 标本期不适用 (inapplicable), 闸门不能以它通过 (gates.check_materiality).

    返回 (候选, 执行重要性比例范围, 明显微小比例). M01 缺某个基准的行时抛 LookupError.
    """
    bases = {r["basis"]: to_cents(r.get("base_amount_cents")) for r in rows}
    candidates = []
    for spec in params["candidates"]:
        base = bases.get(spec["basis"])
        if base is None:
            raise LookupError(f"PLN-01 M01 没有 {spec['basis']} 基准的金额")
        candidates.append(
            MaterialityCandidate(
                basis=spec["basis"],
                base_amount_cents=base,
                pct_low=spec["pct_low"],
                pct_high=spec["pct_high"],
                low_cents=scaled(base, spec["pct_low"]),
                high_cents=scaled(base, spec["pct_high"]),
                not_applicable=inapplicable(spec["basis"], base),
            )
        )
    low, high = params["performance_pct_range"]
    return candidates, (low, high), params["clearly_trivial_pct"]


@dataclass(frozen=True)
class ScreeningBuild:
    """PLN-07 第 3 步的结果与构成: 复筛视图, 写进状态的分析结果要用的规则结果."""

    screening: AnalyticsScreening
    recommendation: AnalyticsRecommendation | None
    fallback: bool
    fallback_reason: str | None


def llm_template(template: ProcedureTemplate, prompt: str, output_model: str) -> ProcedureTemplate:
    """PLN-07 的一条大模型调用所用的模板副本: 只换 llm 的提示词与输出模型, 程序编号与 owner 不变.

    PLN-07 YAML 保持 engine rule (节点先跑规则取数), 两次大模型调用的输出模型与提示词在代码里各取一份.
    """
    return template.model_copy(update={"llm": LLMSpec(prompt=prompt, output_model=output_model)})


def period_payload(start: date, end: date) -> dict[str, Any]:
    """提示词用的一个期间: 起止, 整月数与天数."""
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "months": period_months(start, end),
        "days": (end - start).days + 1,
    }


def prior_payload(start: date | None, current_start: date, current_end: date) -> dict[str, Any]:
    """提示词用的上期: 上期截止日为本期起始日前一天; 没有声明上期起始日时标 known 为假, 长度按本期等长处理."""
    current = period_payload(current_start, current_end)
    if start is None:
        return {"start": None, "end": None, "months": current["months"], "days": current["days"], "known": False}
    end = current_start - timedelta(days=1)
    return {**period_payload(start, end), "known": True}


def parameter_categories(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """参数里的分类列表, 缺省取配置的缺省值 (SQL 参数按字符串序列传)."""
    return tuple(value) if isinstance(value, (list, tuple)) and value else default


def benchmark_totals(accounts: Sequence[Any], rows: Sequence[Any], params: Mapping[str, Any]) -> dict[str, int]:
    """按参数中的标准分类汇总一期的六个基准金额 (分)."""
    return cast(
        dict[str, int],
        sum_totals(
            accounts,
            rows,
            revenue_categories=parameter_categories(params.get("revenue_categories"), REVENUE_CATEGORIES),
            cost_of_sales_categories=parameter_categories(
                params.get("cost_of_sales_categories"), COST_OF_SALES_CATEGORIES
            ),
            income_tax_categories=parameter_categories(params.get("income_tax_categories"), INCOME_TAX_CATEGORIES),
        ),
    )


def fallback_item(exception: Any, reason: str) -> AnalyticsScreeningItem:
    """退回规则时的一条重点关注: 规则例外原样列出, 注明退回原因."""
    account_code = exception.source_ids[0] if exception.source_ids else ""
    return AnalyticsScreeningItem(
        account_code=account_code,
        account_name=str(exception.detail.get("account_name") or account_code),
        amount_cents=exception.amount_cents,
        reason=f"Fallback to the rule result, not screened by the model: {reason}",
        risk_or_assertion="Rule A01 movement above the fallback threshold; auditor review required.",
        evidence=list(exception.evidence),
    )


def fallback_screening(rule: RuleResult, threshold_cents: int, reason: str) -> ScreeningBuild:
    """大模型失败或输出不合格时的退回 (需求 R371 验收 6): 保留本期收入 × 0.5% 的规则结果, 标 fallback."""
    items = [fallback_item(exception, reason) for exception in rule.exceptions]
    return ScreeningBuild(
        screening=AnalyticsScreening(
            threshold_cents=threshold_cents,
            hit_account_codes=[item.account_code for item in items],
            focus=items,
        ),
        recommendation=None,
        fallback=True,
        fallback_reason=reason,
    )


def screen_result(
    rule: RuleResult,
    draft: AnalyticsScreeningDraft,
    recommendation: AnalyticsRecommendation,
    accounts: Mapping[str, Any],
) -> ScreeningBuild:
    """把通过校验的复筛草稿组装成结果: 命中科目按重点/排除分组, 另有补充科目与补充观察."""
    exceptions = {exception.source_ids[0]: exception for exception in rule.exceptions if exception.source_ids}
    focus: list[AnalyticsScreeningItem] = []
    excluded: list[AnalyticsScreeningItem] = []
    for decision in draft.decisions:
        exception = exceptions.get(decision.account_code)
        amount = exception.amount_cents if exception is not None else None
        name = str(accounts[decision.account_code].name) if decision.account_code in accounts else decision.account_code
        item = AnalyticsScreeningItem(
            account_code=decision.account_code,
            account_name=name,
            amount_cents=amount,
            reason=decision.reason,
            risk_or_assertion=decision.risk_or_assertion,
            evidence=list(decision.evidence),
        )
        (focus if decision.verdict == "focus" else excluded).append(item)
    additions = [
        AnalyticsAddition(
            account_code=addition.account_code,
            account_name=str(accounts[addition.account_code].name)
            if addition.account_code in accounts
            else addition.account_code,
            reason=addition.reason,
            risk_or_assertion=addition.risk_or_assertion,
        )
        for addition in draft.additions
    ]
    observations = [
        AnalyticsObservation(
            account_codes=list(observation.account_codes),
            reason=observation.reason,
            risk_or_assertion=observation.risk_or_assertion,
            evidence=list(observation.evidence),
        )
        for observation in draft.observations
    ]
    return ScreeningBuild(
        screening=AnalyticsScreening(
            threshold_cents=recommendation.threshold_cents,
            hit_account_codes=[exception.source_ids[0] for exception in rule.exceptions if exception.source_ids],
            focus=focus,
            additions=additions,
            excluded=excluded,
            observations=observations,
        ),
        recommendation=recommendation,
        fallback=False,
        fallback_reason=None,
    )


def failure_reason(step: str, error: BaseException | None, level: str | None, errors: Sequence[str]) -> str:
    """大模型失败或输出不合格的原因 (写进 fallback_reason): 带步骤, 级别与错误的简短说明."""
    if error is not None:
        return f"{step} failed: {type(error).__name__}: {error}"
    detail = "; ".join(errors) or "no detail"
    return f"{step} failed validation ({level}): {detail}"


Findings = RuleResult | list[AuditPlan]  # PLN-07, PLN-01 的规则结果; PLN-15 要写入的计划 (新修订在首)


class RiskAssessorAgent(ProcedureAgent[Findings]):
    """A4: 执行 PLN-07, PLN-01, 做初步分析并算重要性候选, 两条程序都经规则引擎取数; 功能 planning 打开时另执行
    PLN-15 (起草总体审计计划, agents/audit_plan; 未启用时注册表不映射), 并让 PLN-07 走三步的混合流程. 按程序编号分派."""

    agent_id: ClassVar[AgentId] = AgentId.A4  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"PLN-07", "PLN-01", "PLN-15"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> Findings:
        """PLN-15 交 audit_plan.draft_plans 起草计划; 其余执行程序 YAML 中的规则, 参数取本次执行的参数快照.

        PLN-07 先按参数快照的缺省门槛跑一次规则 (也取 A02/A03 的行); 功能 planning 打开时, 解读阶段再按大模型选的
        基准与比例重跑 A01 并复筛.
        """
        if ctx.procedure.id == "PLN-15":
            return await draft_plans(self.deps, ctx)
        return await self.deps.rules.run(ctx.engagement.id, ctx.template, ctx.run, ctx.run.params_snapshot)

    async def interpret(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """按程序组装片段, 执行记录置成功终态 (规则程序没有执行的规则记进 rule_outcomes, 需求 R130)."""
        if not isinstance(findings, RuleResult):
            return plan_patch(ctx, findings)
        if ctx.procedure.id == "PLN-07":
            return await self.analytics(ctx, findings)
        patch = self.materiality(ctx, findings)
        return patch | {"procedure_runs": [finish_run(ctx.run, datetime.now(UTC), findings.outcomes)]}

    async def analytics(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        """PLN-07: 纯规则时 A01 的变动例外原样返回, 由 A02 的取数算比率, A03 的取数出月度表.

        功能 planning 打开时走三步 (需求 R371): 第 1 步大模型从配置的清单与比例范围中选基准与比例; 第 2 步用门槛 =
        基准 × 比例与 20% 变动率重跑规则, 全部命中进例外与被测总体; 第 3 步大模型逐个复筛并可补充. 两次大模型调用任
        一次失败或输出不合格且重试用尽时, 退回本期收入 × 0.5% 的规则结果并标 fallback (第一步成功第二步失败也退回,
        不保留用不上的推荐). 推荐只作 PLN-01/03 闸门的预选, 审计师在闸门改选后不自动重算, 按现有流程标过期或重跑.
        """
        rule, build = await self.analytics_rule_and_screening(ctx, findings)
        result = AnalyticsResult(
            id="ANALYTICS",
            version=(ctx.state["analytics"].version + 1) if ctx.state["analytics"] is not None else 1,
            ratios=analytics_ratios(rule.rows.get("A02", [])),
            exception_ids=[x.id for x in rule.exceptions],
            monthly=monthly_figures(rule.rows.get("A03", [])),
            recommendation=build.recommendation,
            screening=build.screening,
            fallback=build.fallback,
            fallback_reason=build.fallback_reason,
        )
        return {
            "analytics": result,
            "exceptions": list(rule.exceptions),
            "procedure_runs": [finish_run(ctx.run, datetime.now(UTC), rule.outcomes)],
        }

    async def analytics_rule_and_screening(
        self, ctx: RunContext, findings: RuleResult
    ) -> tuple[RuleResult, ScreeningBuild]:
        """PLN-07 解读用的规则结果与复筛: 功能 planning 关闭时原样返回规则结果, 空复筛; 打开时跑三步.

        返回 (最终规则结果, 复筛结果与退回标记). 最终规则结果在成功时为按选定门槛重跑的 A01 (A02/A03 同样取它), 退回
        时为第一次的纯规则结果.
        """
        if PLANNING not in self.deps.features:
            return findings, ScreeningBuild(AnalyticsScreening(), None, False, None)
        return await self.hybrid_analytics(ctx, findings)

    async def hybrid_analytics(self, ctx: RunContext, findings: RuleResult) -> tuple[RuleResult, ScreeningBuild]:
        """三步流程: 大模型选基准与比例 → 规则用选定的门槛重跑 → 大模型复筛.

        第 1 步所需的本期/上期汇总与 A03 月度走势在这里组装. 任一步大模型失败都退回第一次的规则结果 (本期收入 × 0.5%),
        并写明原因; 第 2 步重跑失败 (RuleError) 不算大模型失败, 原样上抛由节点按执行失败处理.
        """
        eid, params = ctx.engagement.id, ctx.run.params_snapshot
        profile = await self.client_profile(ctx)
        benchmarks = load_benchmarks(self.deps.library.root)
        accounts = await self.deps.financial_store.accounts(eid)
        current_rows = await trial_balance_rows(self.deps.financial_store, eid, "current")
        prior_rows = await trial_balance_rows(self.deps.financial_store, eid, "prior")
        current = benchmark_totals(accounts, current_rows, params)
        prior = benchmark_totals(accounts, prior_rows, params)
        monthly = monthly_figures(findings.rows.get("A03", []))
        prior_start = params.get("prior_period_start")
        if isinstance(prior_start, str):
            prior_start = date.fromisoformat(prior_start)
        start, end = ctx.engagement.period_start, ctx.engagement.period_end
        basis_variables = {
            "entity": profile.model_dump(mode="json"),
            "period_current": period_payload(start, end),
            "period_prior": prior_payload(prior_start if isinstance(prior_start, date) else None, start, end),
            "amounts_current": current,
            "amounts_prior": prior,
            "trial_balance": trial_balance_table(accounts, current_rows, prior_rows),
            "monthly": [figure.model_dump(mode="json") for figure in monthly],
            "benchmarks": [benchmark.model_dump(mode="json") for benchmark in benchmarks.bases],
        }
        fallback_threshold = scaled(max(current["revenue"], 0), 0.005)
        try:
            basis_call = await self.deps.judgments.call_llm(
                eid,
                llm_template(ctx.template, BASIS_PROMPT, BASIS_MODEL),
                ctx.run,
                basis_variables,
                ValidationContext(standards_of=ctx.template.id, client_profile=profile),
                AgentId.A4,
                "basis",
            )
        except Exception as error:  # 传输层失败, 固定输出缺失等都退回规则 (需求 R371 验收 6)
            return findings, fallback_screening(
                findings, fallback_threshold, failure_reason("Step 1 (basis and percentage)", error, None, [])
            )
        report = basis_call.report
        if report.level_failed is not None or report.output is None:
            return findings, fallback_screening(
                findings,
                fallback_threshold,
                failure_reason("Step 1 (basis and percentage)", None, report.level_failed, report.errors),
            )
        basis = cast(AnalyticsBasisDraft, report.output)
        base_cents = current[basis.basis]
        threshold = scaled(base_cents, basis.percentage)
        effective = effective_revenue_percentage(threshold, current["revenue"])
        if base_cents <= 0 or threshold <= 0 or effective is None:
            reason = (
                f"Step 2 (rule threshold) cannot express {basis.basis} at {basis.percentage} through rule A01: "
                f"base {base_cents} cents, revenue {current['revenue']} cents"
            )
            return findings, fallback_screening(findings, fallback_threshold, reason)
        selected = await self.deps.rules.run(
            eid,
            ctx.template,
            ctx.run,
            {**params, "min_change_revenue_pct": effective},
        )
        recommendation = AnalyticsRecommendation(
            basis=basis.basis,
            base_amount_cents=base_cents,
            percentage=basis.percentage,
            threshold_cents=threshold,
            rationale=basis.rationale,
            evidence=list(basis.evidence),
            counter_argument=basis.counter_argument,
            self_confidence=basis.self_confidence,
        )
        hits = [exception for exception in selected.exceptions if exception.source_ids]
        screen_variables = {
            "entity": profile.model_dump(mode="json"),
            "recommendation": {
                "basis": recommendation.basis,
                "percentage": recommendation.percentage,
                "base_amount_cents": recommendation.base_amount_cents,
                "threshold_cents": recommendation.threshold_cents,
                "rationale": recommendation.rationale,
            },
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "rule_hits": [
                {
                    "exception_id": exception.id,
                    "account_code": exception.source_ids[0],
                    "account_name": exception.detail.get("account_name"),
                    "amount_cents": exception.amount_cents,
                    **{
                        key: exception.detail.get(key)
                        for key in ("current_cents", "prior_cents", "opening_cents", "change_pct", "basis")
                    },
                    "evidence": [pointer.model_dump(mode="json") for pointer in exception.evidence],
                }
                for exception in hits
            ],
            "trial_balance": trial_balance_table(accounts, current_rows, prior_rows),
            "monthly": [figure.model_dump(mode="json") for figure in monthly],
        }
        try:
            screen_call = await self.deps.judgments.call_llm(
                eid,
                llm_template(ctx.template, SCREEN_PROMPT, SCREEN_MODEL),
                ctx.run,
                screen_variables,
                ValidationContext(
                    standards_of=ctx.template.id,
                    client_profile=profile,
                    analytics_hits=[exception.source_ids[0] for exception in hits],
                ),
                AgentId.A4,
                "screen",
            )
        except Exception as error:  # 第 3 步失败退回原规则, 不保留选定的门槛
            return findings, fallback_screening(
                findings, fallback_threshold, failure_reason("Step 3 (screening)", error, None, [])
            )
        screen_report = screen_call.report
        if screen_report.level_failed is not None or screen_report.output is None:
            return findings, fallback_screening(
                findings,
                fallback_threshold,
                failure_reason("Step 3 (screening)", None, screen_report.level_failed, screen_report.errors),
            )
        draft = cast(AnalyticsScreeningDraft, screen_report.output)
        by_code = {account.code: account for account in accounts}
        return selected, screen_result(selected, draft, recommendation, by_code)

    async def client_profile(self, ctx: RunContext) -> ClientProfile:
        """本项目采用版本的客户画像; 没有时抛 LookupError (三步流程的输入缺不了它)."""
        profile = await self.deps.engagement_store.client_profile(
            ctx.engagement.client_id, ctx.engagement.client_profile_version
        )
        if profile is None:
            raise LookupError(
                f"client profile {ctx.engagement.client_id} v{ctx.engagement.client_profile_version} not found"
            )
        return profile

    def materiality(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        """PLN-01: 由 M01 的基准金额算三基准候选, 出重要性候选下一版.

        状态中还没有风险清单时, 一并带出数据集预置的审计目标与风险清单 (第 1 版); 已有时不带, 以免覆盖闸门改过的版本.
        """
        candidates, performance, trivial = materiality_candidates(findings.rows.get("M01", []), ctx.run.params_snapshot)
        # 推荐的六种配置基准均可在人工闸门确认; 非推荐的旧候选保留原区间.
        analytics = ctx.state["analytics"]
        recommendation = analytics.recommendation if analytics and PLANNING in self.deps.features else None
        if recommendation is not None:
            config = load_benchmarks(self.deps.library.root).get(recommendation.basis)
            assert config is not None
            recommended = MaterialityCandidate(
                basis=recommendation.basis,
                base_amount_cents=recommendation.base_amount_cents,
                pct_low=config.pct_min,
                pct_high=config.pct_max,
                low_cents=scaled(recommendation.base_amount_cents, config.pct_min),
                high_cents=scaled(recommendation.base_amount_cents, config.pct_max),
            )
            candidates = [recommended if c.basis == recommended.basis else c for c in candidates]
            if not any(c.basis == recommended.basis for c in candidates):
                candidates.append(recommended)
        previous = ctx.state["materiality_candidates"]
        result = MaterialityCandidates(
            id="MATERIALITY",
            version=previous.version + 1 if previous is not None else 1,
            candidates=candidates,
            performance_pct_range=performance,
            clearly_trivial_pct=trivial,
        )
        patch: StatePatch = {"materiality_candidates": result}
        if not ctx.state["risk_register"]:
            dataset = ctx.engagement.dataset
            objectives, risks = (  # 没有数据集的项目 (新建的客户) 取通用模板
                self.deps.datasets.planning_presets(dataset)
                if dataset is not None
                else self.deps.library.planning_templates()
            )
            patch["audit_objectives"] = objectives
            patch["risk_register"] = risks
        return patch
