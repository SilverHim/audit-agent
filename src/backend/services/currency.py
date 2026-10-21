"""记账币种 (需求 R366, Issue #66): 金额的显示与币种变动的留痕.

金额照旧按记账币种的 "分" (×100 的整数, 字段名以 _cents 结尾) 存, 显示为 "AUD 1,234.56": 币种代码, 千分位, 该币种
通常的小数位 (多数两位; 日元等为零位, 巴林第纳尔等为三位, 见 MINOR_DIGITS). 提示词输入 (模板过滤器 money,
services.procedure_library), 审计助理的工具结果 (agents.concierge_chat.tools) 都用 format_money, 不各写一套.
"""

from collections.abc import Awaitable, Callable
from decimal import ROUND_HALF_UP, Decimal

from backend.contracts.models import DEFAULT_CURRENCY, Engagement, TrailEntry, TrailKind
from backend.contracts.ports import EngagementStore

# 小数位不是两位的 ISO 4217 币种 (ISO 4217 的 minor unit); 其余按两位
MINOR_DIGITS: dict[str, int] = {
    **dict.fromkeys(("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV"), 0),
    **dict.fromkeys(("XAF", "XOF", "XPF"), 0),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
}


def format_money(cents: int, currency: str = DEFAULT_CURRENCY) -> str:
    """金额 (分) 的显示: 币种代码, 空格, 千分位与该币种通常的小数位, 如 "AUD 1,234.56", "JPY 1,235" (四舍五入),
    负数为 "AUD -1,234.56"."""
    digits = MINOR_DIGITS.get(currency, 2)
    amount = (Decimal(cents) / 100).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f"{currency} {amount:,.{digits}f}"


async def default_currency(eid: str) -> str:
    """不按项目查时的记账币种: 澳元 (判断引擎没给查询函数时, 如测试)."""
    return DEFAULT_CURRENCY


def engagement_currency(store: EngagementStore) -> Callable[[str], Awaitable[str]]:
    """按项目查记账币种的函数 (组装根给判断引擎): 项目当前的 currency, 查不到项目为澳元."""

    async def lookup(eid: str) -> str:
        engagement = await store.get(eid)
        return engagement.currency if engagement is not None else DEFAULT_CURRENCY

    return lookup


def currency_trail(eid: str, actor: str, before: Engagement, after: str, source: str) -> TrailEntry:
    """记账币种的确认或修改的 data 留痕: payload 的 action 为 currency, from 与 to 为改前与改后的代码, source 为来源
    (auditor: 审计师在数据管理页确认, PUT currency; xero: Xero 同步取组织本位币, services.xero_link). 币种与来源都
    不变时调用方不写."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=actor,
        summary=f"记账币种 {before.currency} → {after} ({source})",
        payload={"action": "currency", "from": before.currency, "to": after, "source": source},
    )
