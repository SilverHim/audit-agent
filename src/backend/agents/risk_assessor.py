"""A4: PLN-07, PLN-01.

返回字段: analytics, materiality_candidates, audit_objectives, risk_register, exceptions, procedure_runs.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.models import AgentId, AnalyticsResult, MaterialityCandidate, MaterialityCandidates
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


def scaled(base_cents: int, pct: float) -> int:
    """基准金额 × 比例, 用 Decimal 四舍五入到整数分. 比例经 str 转换, 0.005 不带二进制误差."""
    return int((Decimal(base_cents) * Decimal(str(pct))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def materiality_candidates(
    rows: Sequence[Mapping[str, Any]], params: Mapping[str, Any]
) -> tuple[list[MaterialityCandidate], tuple[float, float], float]:
    """按 PLN-01 M01 的行 (basis, base_amount_cents) 与参数算候选, 顺序同 params.candidates.

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
            )
        )
    low, high = params["performance_pct_range"]
    return candidates, (low, high), params["clearly_trivial_pct"]


class RiskAssessorAgent(ProcedureAgent[RuleResult]):
    """A4: 执行 PLN-07, PLN-01, 做初步分析并算重要性候选. 两条程序都经规则引擎取数."""

    agent_id: ClassVar[AgentId] = AgentId.A4  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"PLN-07", "PLN-01"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> RuleResult:
        """执行程序 YAML 中的规则, 参数取本次执行的参数快照."""
        return await self.deps.rules.run(ctx.engagement.id, ctx.template, ctx.run, ctx.run.params_snapshot)

    async def interpret(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        """按程序组装片段, 执行记录置成功终态."""
        interpreters: dict[str, Callable[[RunContext, RuleResult], StatePatch]] = {
            "PLN-07": self.analytics,
            "PLN-01": self.materiality,
        }
        patch = interpreters[ctx.procedure.id](ctx, findings)
        return patch | {"procedure_runs": [finish_run(ctx.run, datetime.now(UTC))]}

    def analytics(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        """PLN-07: A01 的变动例外原样返回, 由 A02 的取数算比率, 出分析结果下一版."""
        previous = ctx.state["analytics"]
        result = AnalyticsResult(
            id="ANALYTICS",
            version=previous.version + 1 if previous is not None else 1,
            ratios=analytics_ratios(findings.rows.get("A02", [])),
            exception_ids=[x.id for x in findings.exceptions],
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
            objectives, risks = self.deps.datasets.planning_presets(ctx.engagement.dataset)
            patch["audit_objectives"] = objectives
            patch["risk_register"] = risks
        return patch
