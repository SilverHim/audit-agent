"""标准科目分类: 名称规则兜底, Numbat 科目表的映射结果, 程序配置按分类取科目."""

import csv
from pathlib import Path

import pytest
import yaml

from backend.contracts.records import Account
from backend.services.account_mapping import CATEGORIES, classify_by_name, with_category

ROOT = Path(__file__).resolve().parents[1]
NUMBAT = {
    "1010": "cash",
    "1020": "cash",
    "1100": "receivables",
    "1150": "receivables",
    "1200": "inventory",
    "1300": "prepayments",
    "1500": "property_plant_equipment",
    "1510": "property_plant_equipment",
    "2100": "payables",
    "2200": "accruals",
    "2300": "employee_provisions",
    "2400": "tax_payable",
    "3100": "share_capital",
    "3200": "retained_earnings",
    "4100": "revenue",
    "4200": "other_income",
    "5100": "cost_of_sales",
    "6100": "employee_expenses",
    "6200": "operating_expenses",
    "6300": "operating_expenses",
    "6400": "depreciation_amortisation",
    "6500": "operating_expenses",
    "8100": "income_tax",
}


def test_numbat_accounts_all_mapped() -> None:
    """Numbat 的 23 个科目按名称规则全部归类; 收入, 销售成本, 所得税各只有一个科目, 程序按分类取数与原先按代码一致."""
    with (ROOT / "data/demo/accounts.csv").open(encoding="utf-8") as stream:
        accounts = [with_category(Account.model_validate(r)) for r in csv.DictReader(stream)]
    assert {a.code: a.category for a in accounts} == NUMBAT


@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("Petty Cash", "asset", "cash"),
        ("Loan from director", "liability", "borrowings"),
        ("PAYG Withholding Payable", "liability", "tax_payable"),
        ("Superannuation Payable", "liability", "employee_provisions"),
        ("Goodwill", "asset", "intangible_assets"),
        ("Dividend income", "revenue", "other_income"),
        ("Consulting fees", "revenue", "revenue"),
        ("Interest expense", "expense", "finance_costs"),
        ("Owner contributions", "equity", "other_equity"),
        ("Clearing Account", "liability", None),
        ("Suspense", "asset", None),
    ],
)
def test_classify_by_name(name: str, kind: str, expected: str | None) -> None:
    assert classify_by_name(name, kind) == expected


def test_with_category_keeps_existing() -> None:
    account = Account(code="X", name="Sales", type="revenue", category="other_income")
    assert with_category(account) is account


def test_procedures_take_accounts_by_category() -> None:
    """程序配置不写科目代码: 分类参数的取值都是标准分类, SQL 按 accounts.category 过滤."""
    for path in (ROOT / "src/backend/knowledge/procedures").glob("*.yaml"):
        template = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert not any(key.endswith("_account_codes") for key in template.get("params", {})), path.name
        for key, value in template.get("params", {}).items():
            if key.endswith("_categories"):
                assert set(value) <= set(CATEGORIES), (path.name, key)
                assert any(f":{key}" in rule["sql"] for rule in template["rules"]), (path.name, key)
