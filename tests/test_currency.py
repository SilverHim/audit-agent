"""项目币种的金额显示, 提示词过滤器与数据集来源 (需求 R366)."""

from pathlib import Path

import pytest

from backend.adapters.xero_dataset import Snapshot, base_currency, snapshot_currency
from backend.contracts.models import initial_currency, with_currency
from backend.services.currency import engagement_currency, format_money
from backend.services.procedure_library import prompt_env
from tests.fakes import ENGAGEMENT, FakeEngagementStore


@pytest.mark.parametrize(
    ("cents", "currency", "expected"),
    [
        (123456, "AUD", "AUD 1,234.56"),
        (-82500, "NZD", "NZD -825.00"),
        (0, "USD", "USD 0.00"),
        (123450, "JPY", "JPY 1,235"),
        (-123450, "JPY", "JPY -1,235"),
        (123456, "KWD", "KWD 1,234.560"),
    ],
)
def test_money_keeps_hundredth_storage_and_formats_currency(cents: int, currency: str, expected: str) -> None:
    """金额存储仍为主单位乘 100; 只有显示小数位按币种变, 包括半数向外舍入."""
    assert format_money(cents, currency) == expected


def test_prompt_money_filter_uses_context_and_explicit_currency() -> None:
    template = prompt_env().from_string('{{ amount | money }} / {{ amount | money("USD") }} / {{ absent | money }}')
    assert template.render(amount=82500, absent=None, currency="NZD") == "NZD 825.00 / USD 825.00 / null"
    assert prompt_env().from_string("{{ amount | money }}").render(amount=4853) == "AUD 48.53"


@pytest.mark.parametrize("value", [None, "", "usd", "AU", "AU33", 123])
def test_invalid_xero_currency_uses_default(value: object) -> None:
    assert base_currency({"BaseCurrency": value}) is None
    assert initial_currency(base_currency({"BaseCurrency": value})) == {"currency": "AUD", "currency_source": "default"}


async def test_project_currency_and_auditor_precedence() -> None:
    store = FakeEngagementStore(engagements=[ENGAGEMENT])
    lookup = engagement_currency(store)
    assert await lookup(ENGAGEMENT.id) == await lookup("missing") == "AUD"
    assert base_currency({"BaseCurrency": "NZD"}) == "NZD"
    before = await store.set_currency(ENGAGEMENT.id, "NZD", "xero")
    assert before == ENGAGEMENT and await lookup(ENGAGEMENT.id) == "NZD"
    await store.set_currency(ENGAGEMENT.id, "USD", "auditor")
    manual = await store.get(ENGAGEMENT.id)
    assert manual is not None
    assert with_currency(manual, "NZD", "xero") is manual
    assert await store.set_currency(ENGAGEMENT.id, "NZD", "xero") == manual
    assert await lookup(ENGAGEMENT.id) == "USD"


def test_snapshot_base_currency_is_read_without_customer_data() -> None:
    snapshot = Snapshot(Path("synthetic-currency"), files={"organisation": [{"BaseCurrency": "NZD"}]})
    assert snapshot_currency(snapshot) == "NZD"
    assert snapshot_currency(Snapshot(Path("empty-synthetic"), files={"organisation": []})) is None
