"""A4: PLN-07, PLN-01, PLN-15 (功能 planning, 见 agents/audit_plan).

返回字段: analytics, materiality_candidates, audit_objectives, risk_register, exceptions, audit_plans, procedure_runs.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar

from backend.agents.audit_plan import draft_plans, plan_patch
from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.models import (
    AgentId,
    AnalyticsResult,
    AuditPlan,
    MaterialityCandidate,
    MaterialityCandidates,
    MaterialityInapplicable,
    MonthlyFigures,
)
from backend.contracts.services import RuleResult
from backend.contracts.state import StatePatch

RATIO_PLACES = Decimal("0.0001")  # 比率保留四位小数


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
    依据 ASA 320 第 A5 段 (税前利润不宜作基准时改用收入等其他基准); 不为正的基准乘比例得不出重要性."""
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


Findings = RuleResult | list[AuditPlan]  # PLN-07, PLN-01 的规则结果; PLN-15 要写入的计划 (新修订在首)


class RiskAssessorAgent(ProcedureAgent[Findings]):
    """A4: 执行 PLN-07, PLN-01, 做初步分析并算重要性候选, 两条程序都经规则引擎取数; 功能 planning 打开时另执行
    PLN-15 (起草总体审计计划, agents/audit_plan; 未启用时注册表不映射). 按程序编号分派."""

    agent_id: ClassVar[AgentId] = AgentId.A4  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"PLN-07", "PLN-01", "PLN-15"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> Findings:
        """PLN-15 交 audit_plan.draft_plans 起草计划; 其余执行程序 YAML 中的规则, 参数取本次执行的参数快照."""
        if ctx.procedure.id == "PLN-15":
            return await draft_plans(self.deps, ctx)
        return await self.deps.rules.run(ctx.engagement.id, ctx.template, ctx.run, ctx.run.params_snapshot)

    async def interpret(self, ctx: RunContext, findings: Findings) -> StatePatch:
        """按程序组装片段, 执行记录置成功终态 (规则程序没有执行的规则记进 rule_outcomes, 需求 R130)."""
        if not isinstance(findings, RuleResult):
            return plan_patch(ctx, findings)
        interpreters: dict[str, Callable[[RunContext, RuleResult], StatePatch]] = {
            "PLN-07": self.analytics,
            "PLN-01": self.materiality,
        }
        patch = interpreters[ctx.procedure.id](ctx, findings)
        return patch | {"procedure_runs": [finish_run(ctx.run, datetime.now(UTC), findings.outcomes)]}

    def analytics(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        """PLN-07: A01 的变动例外原样返回, 由 A02 的取数算比率, A03 的取数出月度表, 出分析结果下一版."""
        previous = ctx.state["analytics"]
        result = AnalyticsResult(
            id="ANALYTICS",
            version=previous.version + 1 if previous is not None else 1,
            ratios=analytics_ratios(findings.rows.get("A02", [])),
            exception_ids=[x.id for x in findings.exceptions],
            monthly=monthly_figures(findings.rows.get("A03", [])),
        )
        return {"analytics": result, "exceptions": list(findings.exceptions)}

    def materiality(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        """PLN-01: 由 M01 的基准金额算三基准候选, 出重要性候选下一版.

        状态中还没有风险清单时, 一并带出数据集预置的审计目标与风险清单 (第 1 版); 已有时不带, 以免覆盖闸门改过的版本.
        """
        candidates, performance, trivial = materiality_candidates(findings.rows.get("M01", []), ctx.run.params_snapshot)
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
