"""PLN-07 初步分析可用的重要性基准清单与按标准分类的汇总 (需求 R371).

基准清单与比例范围在 knowledge/analytic_benchmarks.json, 出处以 ASA 320 第 A4-A8 段为准: A8 只给税前利润 5% 与收入或
总费用 1% 两个例子, 其余范围是惯例 (配置里逐项写明). 汇总口径与 PLN-01 M01, PLN-07 A01 一致: 试算表余额借方为正,
收入取贷减借 (取正), 税前利润剔除所得税, 净资产为资产与负债两类余额之和 (借方为正, 负债贷方为负).
"""

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from backend.contracts.models import AnalyticBasis, Model
from backend.contracts.ports import FinancialDataStore
from backend.contracts.records import Account, TrialBalanceRow

BENCHMARKS_FILE = "analytic_benchmarks.json"  # knowledge/ 下的文件名
REVENUE_CATEGORIES = ("revenue",)  # 收入取这些标准分类 (同 PLN-01 M01, PLN-07 A01 的缺省)
COST_OF_SALES_CATEGORIES = ("cost_of_sales",)  # 毛利的成本取这些标准分类 (同 PLN-07 A02)
INCOME_TAX_CATEGORIES = ("income_tax",)  # 税前利润剔除的所得税分类 (同 PLN-01 M01)


class Benchmark(Model):
    """一个可选基准及其比例范围."""

    basis: AnalyticBasis  # 基准
    pct_min: float = Field(gt=0, le=1)  # 比例下限
    pct_max: float = Field(gt=0, le=1)  # 比例上限
    example_pct: float | None = Field(default=None, gt=0, le=1)  # ASA 320 A8 给出的例子; 没有例子为空
    source: str  # 出处; 范围是惯例时写明
    convention: bool  # 比例范围是否只是惯例 (A8 给出的例子以外的范围为真)

    @model_validator(mode="after")
    def range_ordered(self) -> "Benchmark":
        """下限不大于上限, 例子在范围内."""
        if self.pct_min > self.pct_max:
            raise ValueError(f"{self.basis}: pct_min 大于 pct_max")
        if self.example_pct is not None and not self.pct_min <= self.example_pct <= self.pct_max:
            raise ValueError(f"{self.basis}: example_pct 不在比例范围内")
        if not self.source.strip():
            raise ValueError(f"{self.basis}: source 不能为空")
        return self

    def contains(self, percentage: float) -> bool:
        """比例是否在范围内 (含两端)."""
        return self.pct_min <= percentage <= self.pct_max


class BenchmarkConfig(Model):
    """knowledge/analytic_benchmarks.json 的内容."""

    source: str  # 整份清单的出处说明
    bases: list[Benchmark]  # 可选基准, 不重复

    @model_validator(mode="after")
    def unique_bases(self) -> "BenchmarkConfig":
        """同一基准只出现一次, 且不为空."""
        repeated = sorted({b.basis for b in self.bases if [x.basis for x in self.bases].count(b.basis) > 1})
        if repeated:
            raise ValueError(f"基准重复: {', '.join(repeated)}")
        if not self.bases:
            raise ValueError("基准清单不能为空")
        return self

    def get(self, basis: str) -> Benchmark | None:
        """按基准取配置; 不在清单内为 None."""
        return next((b for b in self.bases if b.basis == basis), None)


def load_benchmarks(root: Path) -> BenchmarkConfig:
    """读程序库 knowledge 目录下的基准清单. 文件缺或写法不对时抛 ValueError."""
    path = root / BENCHMARKS_FILE
    try:
        return BenchmarkConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"初步分析的基准清单 {path} 读不了: {e}") from e


def totals_from_rows(
    accounts: Iterable[Account],
    rows: Sequence[TrialBalanceRow],
    *,
    revenue_categories: Sequence[str] = REVENUE_CATEGORIES,
    cost_of_sales_categories: Sequence[str] = COST_OF_SALES_CATEGORIES,
    income_tax_categories: Sequence[str] = INCOME_TAX_CATEGORIES,
) -> dict[AnalyticBasis, int]:
    """按标准分类汇总一期的试算表, 返回六个基准的金额 (单位分), 口径同 PLN-01 M01.

    没有科目表里对应科目的试算表行不计 (同规则 SQL 的 JOIN accounts). 收入与毛利类基准不为正时照实给出, 由调用方
    决定是否选它.
    """
    chart = {a.code: a for a in accounts}
    revenue = cost = expenses = income_tax = assets = net_assets = 0
    for row in rows:
        account = chart.get(row.account_code)
        if account is None:
            continue
        amount = row.balance_cents
        if account.category in revenue_categories:
            revenue -= amount
        if account.category in cost_of_sales_categories:
            cost += amount
        if account.type == "expense":
            expenses += amount
            if account.category in income_tax_categories:
                income_tax += amount
        if account.type == "asset":
            assets += amount
        if account.type in ("asset", "liability"):
            net_assets += amount
    profit_before_tax = -(
        sum(
            row.balance_cents
            for row in rows
            if (account := chart.get(row.account_code)) is not None
            and account.type in ("revenue", "expense")
            and account.category not in income_tax_categories
        )
    )
    return {
        "revenue": revenue,
        "gross_profit": revenue - cost,
        "profit_before_tax": profit_before_tax,
        "total_expenses": expenses,
        "net_assets": net_assets,
        "total_assets": assets,
    }


async def trial_balance_rows(
    financial: FinancialDataStore, eid: str, period: Literal["current", "prior"]
) -> list[TrialBalanceRow]:
    """一期的试算表, 按科目代码排序 (存储不保证顺序)."""
    rows = await financial.trial_balance(eid, period)
    return sorted(rows, key=lambda row: row.account_code)


async def period_totals(
    financial: FinancialDataStore,
    eid: str,
    period: Literal["current", "prior"],
    *,
    revenue_categories: Sequence[str] = REVENUE_CATEGORIES,
    cost_of_sales_categories: Sequence[str] = COST_OF_SALES_CATEGORIES,
    income_tax_categories: Sequence[str] = INCOME_TAX_CATEGORIES,
) -> dict[AnalyticBasis, int]:
    """一期的六个基准金额 (单位分)."""
    accounts = await financial.accounts(eid)
    rows = await trial_balance_rows(financial, eid, period)
    return totals_from_rows(
        accounts,
        rows,
        revenue_categories=revenue_categories,
        cost_of_sales_categories=cost_of_sales_categories,
        income_tax_categories=income_tax_categories,
    )


def trial_balance_table(
    accounts: Iterable[Account], current: Sequence[TrialBalanceRow], prior: Sequence[TrialBalanceRow]
) -> list[dict[str, object]]:
    """两期试算表的逐科目明细 (提示词用): 按科目代码排序, 两期没有余额的科目该期为 None."""
    chart = {a.code: a for a in accounts}
    current_by_code = {row.account_code: row.balance_cents for row in current}
    prior_by_code = {row.account_code: row.balance_cents for row in prior}
    codes = sorted(set(current_by_code) | set(prior_by_code))
    table: list[dict[str, object]] = []
    for code in codes:
        account = chart.get(code)
        if account is None:  # 同汇总口径: 科目表里没有的试算表行不计
            continue
        table.append(
            {
                "source_type": "trial_balance",
                "source_id": code,
                "code": code,
                "name": account.name,
                "type": account.type,
                "category": account.category,
                "current_cents": current_by_code.get(code),
                "prior_cents": prior_by_code.get(code),
            }
        )
    return table


def effective_revenue_percentage(threshold_cents: int, revenue_cents: int) -> float | None:
    """把门槛换算成 A01 的 min_change_revenue_pct: 门槛 / 本期收入; 收入不为正时为 None.

    规则引擎 A01 的门槛参数仍按收入比例写, 换算后 SQL 的 "|变动| ≥ min_change_revenue_pct × 本期收入" 就等于选定基准
    × 选定的比例, 不必另写一套 Python 复算. 比例用 Decimal 算到 12 位, 避免浮点误差把关口算偏.
    """
    if revenue_cents <= 0:
        return None
    return round(threshold_cents / revenue_cents, 12)


def total_cents(totals: Mapping[AnalyticBasis, int], basis: AnalyticBasis) -> int:
    """六个基准金额中取一个 (字典按字面量建, 键总存在)."""
    return totals[basis]
