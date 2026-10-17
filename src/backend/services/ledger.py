"""总账余额的期初口径, 由 PostgresFinancialDataStore.gl_balance 与 Xero 补齐分录共用, 保证两边一致.

期初余额 = 上期试算表余额: 资产, 负债, 权益类照搬; 收入, 费用类归零, 其合计 (上期损益) 转入唯一的留存收益科目.
留存收益科目按标准分类 retained_earnings 识别, 不写死科目代码.
"""

from collections.abc import Mapping

PROFIT_AND_LOSS = frozenset({"revenue", "expense"})  # 期末结转的科目类别


def retained_earnings_code(categories: Mapping[str, str | None]) -> str | None:
    """标准分类为留存收益的科目代码; 没有为 None, 多于一个抛 ValueError (无法确定上期损益转入哪里)."""
    codes = sorted(code for code, category in categories.items() if category == "retained_earnings")
    if len(codes) > 1:
        raise ValueError(f"留存收益科目不唯一: {codes}")
    return codes[0] if codes else None


def opening_balances(
    prior: Mapping[str, int], kinds: Mapping[str, str], categories: Mapping[str, str | None]
) -> dict[str, int]:
    """各科目的期初余额 (分, 借正). 上期有损益却找不到唯一的留存收益科目时抛 ValueError, 不留在损益科目里."""
    profit = sum(value for code, value in prior.items() if kinds.get(code) in PROFIT_AND_LOSS)
    opening = {code: value for code, value in prior.items() if kinds.get(code) not in PROFIT_AND_LOSS}
    retained = retained_earnings_code(categories)
    if profit:
        if retained is None:
            raise ValueError("上期有损益, 但没有标准分类为留存收益的科目, 无法结转")
        opening[retained] = opening.get(retained, 0) + profit
    return opening
