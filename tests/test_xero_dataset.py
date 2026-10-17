"""Xero 快照 → 财务表: 科目分类, 单据反推分录, 补齐分录, 付费 Journals, 发票, 银行与客户画像. 用自造的小快照."""

import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.adapters.xero_dataset import (
    Entry,
    Line,
    Snapshot,
    build_tables,
    client_profile,
    document_entries,
    map_bank_accounts,
    map_bank_transactions,
    plug_entry,
    xero_category,
)
from backend.persistence import stores

STAMP = "/Date(1785000000000+0000)/"  # 2026-07-25


def account(code: str, name: str, kind: str, reporting: str = "", system: str = "", **extra: Any) -> dict[str, Any]:
    row = {"AccountID": f"ID-{code}", "Code": code, "Name": name, "Type": kind, "ReportingCode": reporting}
    if system:
        row["SystemAccount"] = system
    return row | extra


ACCOUNTS = [
    account("090", "Business Bank", "BANK", "ASS", BankAccountNumber="06200012345678"),
    account("610", "Accounts Receivable", "CURRENT", "ASS.CUR.REC.TRA", "DEBTORS"),
    account("800", "Accounts Payable", "CURRLIAB", "LIA.CUR.PAY", "CREDITORS"),
    account("820", "GST", "CURRLIAB", "LIA.CUR.TAX.GST", "GST"),
    account("840", "Historical Adjustment", "CURRLIAB", "LIA.CUR", "HISTORICAL"),
    account("860", "Rounding", "CURRLIAB", "LIA.CUR", "ROUNDING"),
    account("855", "Clearing Account", "CURRLIAB", "LIA"),
    account("801", "Unpaid Expense Claims", "CURRLIAB", "LIA.CUR.PAY.EMP", "UNPAIDEXPCLM"),
    account("960", "Retained Earnings", "EQUITY", "EQU.RET", "RETAINEDEARNINGS"),
    account("200", "Sales", "REVENUE", "REV.TRA.GOO"),
    account("310", "Cost of Goods Sold", "DIRECTCOSTS", "EXP"),
    account("400", "Advertising", "EXPENSE", "EXP"),
    account("437", "Interest Expense", "EXPENSE", "EXP.INT"),
    account("477", "Wages and Salaries", "EXPENSE", "EXP.EMP.WAG"),
]
CONTACT = {"ContactID": "C1", "Name": "Alpha Pty"}


def doc(**fields: Any) -> dict[str, Any]:
    return {"UpdatedDateUTC": STAMP, "Contact": CONTACT, **fields}


INVOICES = [
    doc(
        InvoiceID="I1",
        InvoiceNumber="INV-1",
        Type="ACCREC",
        Status="PAID",
        DateString="2026-08-01T00:00:00",
        LineAmountTypes="Exclusive",
        Total=1100.0,
        LineItems=[{"AccountCode": "200", "LineAmount": 1000.0, "TaxAmount": 100.0, "Description": "Goods"}],
    ),
    doc(
        InvoiceID="I2",
        InvoiceNumber="INV-2",
        Type="ACCREC",
        Status="VOIDED",
        DateString="2026-08-02T00:00:00",
        LineAmountTypes="Exclusive",
        Total=50.0,
        LineItems=[{"AccountCode": "200", "LineAmount": 50.0, "TaxAmount": 0.0}],
    ),
    doc(
        InvoiceID="ab12cd34-0000",
        InvoiceNumber="",
        Type="ACCPAY",
        Status="AUTHORISED",
        DateString="2026-08-03T00:00:00",
        LineAmountTypes="Inclusive",
        Total=110.0,
        LineItems=[{"AccountCode": "400", "LineAmount": 110.0, "TaxAmount": 10.0}],
    ),
]
PAYMENTS = [
    {
        "PaymentID": "P1",
        "PaymentType": "ACCRECPAYMENT",
        "Status": "AUTHORISED",
        "Date": "/Date(1786320000000+0000)/",  # 2026-08-10
        "UpdatedDateUTC": STAMP,
        "Amount": 1100.0,
        "Reference": "DD 1",
        "Account": {"Code": "090"},
        "Invoice": {"InvoiceID": "I1", "InvoiceNumber": "INV-1", "Contact": CONTACT},
    }
]
BANK_TRANSACTIONS = [
    doc(
        BankTransactionID="B1",
        Type="SPEND",
        Status="AUTHORISED",
        DateString="2026-08-05T00:00:00",
        LineAmountTypes="Inclusive",
        Total=55.0,
        BankAccount={"Code": "090"},
        LineItems=[{"AccountCode": "400", "LineAmount": 55.0, "TaxAmount": 5.0}],
    ),
    doc(
        BankTransactionID="B2", Type="SPEND-TRANSFER", Status="AUTHORISED", DateString="2026-08-06T00:00:00", Total=9.0
    ),
]


def tb_report(balances: dict[str, int]) -> dict[str, Any]:
    """Xero 试算表报表结构, 余额为分 (借正)."""
    rows = [
        {
            "RowType": "Row",
            "Cells": [
                {"Value": f"Account ({code})"},
                {"Value": ""},
                {"Value": ""},
                {"Value": f"{value / 100:.2f}" if value > 0 else ""},
                {"Value": f"{-value / 100:.2f}" if value < 0 else ""},
            ],
        }
        for code, value in balances.items()
    ]
    header = {
        "RowType": "Header",
        "Cells": [{"Value": v} for v in ("Account", "Debit", "Credit", "YTD Debit", "YTD Credit")],
    }
    return {"Rows": [header, {"RowType": "Section", "Rows": rows}]}


# 单据反推: 610 0, 200 -1000.00, 820 -85.00, 400 150.00, 800 -110.00, 090 1045.00;
# 试算表另有期初余额 1000.00 (090 对 840)
CURRENT = {"090": 204500, "200": -100000, "820": -8500, "400": 15000, "800": -11000, "840": -100000}
PRIOR = {"090": 50000, "840": -50000}  # 上期末只有一半期初余额, 另一半 (1000.00 - 500.00) 在本期录入


def write_snapshot(root: Path, **extra: Any) -> Snapshot:
    root.mkdir(parents=True, exist_ok=True)
    resources: dict[str, Any] = {
        "accounts": ACCOUNTS,
        "contacts": [CONTACT | {"TaxNumber": "12 345 678 901"}],
        "invoices": INVOICES,
        "credit_notes": [],
        "payments": PAYMENTS,
        "bank_transactions": BANK_TRANSACTIONS,
        "bank_transfers": [],
        "manual_journals": [],
        "organisation": [
            {
                "OrganisationID": "3c1332bb-175c",
                "Name": "Demo Company (AU)",
                "LegalName": "Demo Company (AU)",
                "RegistrationNumber": "11 111 111 138",
                "OrganisationType": "COMPANY",
                "Class": "DEMO",
            }
        ],
        "trial_balance_2026-11-30": tb_report(CURRENT),
        "trial_balance_2026-06-30": tb_report(PRIOR),
        "dataset": {"period_start": "2026-07-01", "period_end": "2026-11-30", "prior_end": "2026-06-30"},
        **extra,
    }
    for name, data in resources.items():
        (root / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return Snapshot(root)


def test_xero_category_by_type_reporting_code_then_name() -> None:
    mapped = {a["Code"]: xero_category(a) for a in ACCOUNTS}
    assert mapped == {
        "090": "cash",  # 科目类型 BANK
        "610": "receivables",
        "800": "payables",
        "820": "tax_payable",  # 报告代码最长前缀 LIA.CUR.TAX
        "840": "other_current_liabilities",
        "860": "other_current_liabilities",
        "855": None,  # 报告代码只到 LIA, 名称也认不出
        "801": "employee_provisions",
        "960": "retained_earnings",
        "200": "revenue",
        "310": "cost_of_sales",  # 科目类型 DIRECTCOSTS, 报告代码只到 EXP
        "400": "operating_expenses",  # 名称兜底
        "437": "finance_costs",
        "477": "employee_expenses",
    }


def test_document_entries_are_balanced(tmp_path: Path) -> None:
    entries = {e.entry_no: e for e in document_entries(write_snapshot(tmp_path))}
    assert all(sum(x.amount for x in e.lines) == 0 for e in entries.values())
    assert "XI-I2" not in entries  # 作废的不记
    sale = {x.account_code: x.amount for x in entries["XI-I1"].lines}  # 分录号取 InvoiceID
    assert sale == {"200": -100000, "820": -10000, "610": 110000}
    assert entries["XI-I1"].source_doc == "INV-1"
    bill = {x.account_code: x.amount for x in entries["XI-ab12cd34-0000"].lines}  # 含税价: 净额 = 110 - 10
    assert bill == {"400": 10000, "820": 1000, "800": -11000}
    spend = {x.account_code: x.amount for x in entries["XB-B1"].lines}
    assert spend == {"400": 5000, "820": 500, "090": -5500}
    assert "XB-B2" not in entries  # 转账类银行收支由 BankTransfers 记


def test_supplier_invoice_numbers_may_repeat(tmp_path: Path) -> None:
    """采购发票的业务编号可重复 (Xero 不要求唯一): 分录 ID 取 InvoiceID, 不会主键冲突."""
    bill = copy.deepcopy(INVOICES[2]) | {"InvoiceID": "BILL-1", "InvoiceNumber": "001"}
    other = copy.deepcopy(bill) | {"InvoiceID": "BILL-2", "Contact": {"ContactID": "C2", "Name": "Other Supplier"}}
    ids = [r.id for r in build_tables(write_snapshot(tmp_path, invoices=[bill, other]))["journal_entries"]]
    assert len(ids) == len(set(ids))


def test_plug_entry_uses_gl_balance_basis() -> None:
    """期初 = 上期试算表 (损益转入留存收益), 只加本期分录; 本期以外的分录不计."""
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [
        Entry("OLD", date(2026, 5, 1), stamp, [Line("090", 500, None), Line("200", -500, None)], None, None, "x"),
        Entry("NOW", date(2026, 8, 1), stamp, [Line("090", 300, None), Line("200", -300, None)], None, None, "x"),
        Entry("LATER", date(2026, 12, 1), stamp, [Line("090", 99, None), Line("200", -99, None)], None, None, "x"),
    ]
    prior = {"090": 500, "200": -500}  # 上年收入 5.00
    current = {"090": 800, "200": -300, "960": -500}  # 已转入留存收益
    start, end = date(2026, 7, 1), date(2026, 11, 30)
    assert plug_entry(entries, ACCOUNTS, current, prior, start, end) is None
    plug = plug_entry(entries, ACCOUNTS, {**current, "090": 1800, "840": -1000}, prior, start, end)
    assert plug is not None and plug.is_adjustment and plug.posting_date == end
    assert {x.account_code: x.amount for x in plug.lines} == {"090": 1000, "840": -1000}


def test_plug_entry_refuses_without_retained_earnings() -> None:
    """认不出留存收益科目时不把上期损益留在损益科目里 (否则会凭空生成本期损益调整), 直接报错."""
    accounts = [a | {"Name": "Equity reserve", "ReportingCode": "EQU"} if a["Code"] == "960" else a for a in ACCOUNTS]
    accounts = [{k: v for k, v in a.items() if k != "SystemAccount"} if a["Code"] == "960" else a for a in accounts]
    with pytest.raises(ValueError, match="留存收益"):
        plug_entry(
            [], accounts, {"090": 500, "960": -500}, {"090": 500, "200": -500}, date(2026, 7, 1), date(2026, 11, 30)
        )


async def test_build_tables_without_journals_ties_to_gl_balance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单据反推加补齐分录后, 生产的 gl_balance (上期试算表 + 本期分录) 对每个科目都等于本期试算表."""
    tables = build_tables(write_snapshot(tmp_path))
    plug = {
        r.account_code: r.debit_cents - r.credit_cents for r in tables["journal_entries"] if r.entry_no == "XO-PLUG"
    }
    assert plug == {"090": 50000, "840": -50000} and all(
        r.is_adjustment for r in tables["journal_entries"] if r.entry_no == "XO-PLUG"
    )
    assert {code: await gl_balance(monkeypatch, tables, code) for code in CURRENT} == CURRENT
    assert all(r.created_by == "xero" and r.approved_by is None for r in tables["journal_entries"])
    assert {(r.period, r.account_code) for r in tables["trial_balance"]} >= {("current", "200"), ("prior", "840")}
    assert tables["delivery_notes"] == [] and tables["bank_transactions"] == []
    [invoice] = tables["sales_invoices"]  # 作废的 INV-2 不进
    assert (invoice.id, invoice.invoice_no) == ("SI-I1", "INV-1")
    assert invoice.customer_abn == "12345678901" and invoice.amount_cents == 110000
    assert invoice.journal_entry_id == "XI-I1-3"  # 记应收的那一行


async def test_api_journals_roll_prior_profit_into_xero_retained_earnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有付费分录时, gl_balance 按标准分类把上期损益转入 Xero 的留存收益科目 (不再写死 Numbat 的 3200)."""
    journal = {
        "JournalNumber": 1,
        "JournalDate": "/Date(1777593600000+0000)/",  # 2026-05-01, 上期
        "CreatedDateUTC": STAMP,
        "JournalLines": [{"AccountCode": "090", "NetAmount": 5}, {"AccountCode": "200", "NetAmount": -5}],
    }
    resources = {
        "journals": [journal],
        "trial_balance_2026-06-30": tb_report({"090": 500, "200": -500}),
        "trial_balance_2026-11-30": tb_report({"090": 500, "960": -500}),
    }
    tables = build_tables(write_snapshot(tmp_path, **resources))
    assert await gl_balance(monkeypatch, tables, "960") == -500
    assert await gl_balance(monkeypatch, tables, "200") == 0


def test_build_tables_with_api_journals(tmp_path: Path) -> None:
    journals = [
        {
            "JournalNumber": 1,
            "JournalDate": "/Date(1785542400000+0000)/",
            "CreatedDateUTC": STAMP,
            "Reference": "Alpha Pty",
            "SourceType": "ACCREC",
            "SourceID": "I1",
            "JournalLines": [
                {"AccountCode": "610", "NetAmount": 1100.0},
                {"AccountCode": "200", "NetAmount": -1000.0, "Description": "Goods"},
                {"AccountCode": "820", "NetAmount": -100.0},
            ],
        },
        {
            "JournalNumber": 2,
            "JournalDate": "/Date(1784160000000+0000)/",
            "CreatedDateUTC": STAMP,
            "Reference": "Conversion Balance",
            "JournalLines": [{"AccountCode": "090", "NetAmount": 1000.0}, {"AccountCode": "840", "NetAmount": -1000.0}],
        },
    ]
    tables = build_tables(write_snapshot(tmp_path, journals=journals))
    rows = {r.id: r for r in tables["journal_entries"]}
    assert set(rows) == {"XJ-1-1", "XJ-1-2", "XJ-1-3", "XJ-2-1", "XJ-2-2"}  # 有付费分录时不再反推, 也不补齐
    assert rows["XJ-1-1"].debit_cents == 110000 and rows["XJ-1-2"].credit_cents == 100000
    assert rows["XJ-1-1"].source_doc == "INV-1" and not rows["XJ-1-1"].is_adjustment
    assert rows["XJ-1-2"].description == "Goods" and rows["XJ-1-3"].description == "Alpha Pty"
    assert rows["XJ-2-1"].is_adjustment  # 期初转换余额是系统分录
    assert tables["sales_invoices"][0].journal_entry_id == "XJ-1-1"


def test_bank_transactions_from_statements(tmp_path: Path) -> None:
    line = {"payee": "Alpha", "reference": "R1", "isDeleted": False}
    statements = {
        "090": {
            "statements": [
                {
                    "startDate": "2026-07-01",
                    "startBalance": 10.0,
                    "statementLines": [
                        line | {"postedDate": "2026-08-02", "amount": 5.0},
                        line | {"postedDate": "2026-08-01", "amount": -2.0, "isDeleted": True},
                        line | {"postedDate": "2026-08-03", "amount": -3.5, "reference": ""},
                    ],
                }
            ]
        }
    }
    rows = map_bank_transactions(write_snapshot(tmp_path, bank_statements=statements))
    assert [(r.id, r.amount_cents, r.balance_after_cents, r.reference) for r in rows] == [
        ("BT-090-0001", 500, 1500, "R1"),
        ("BT-090-0002", -350, 1150, None),
    ]
    assert rows[0].bank_account_id == "BA-090"


def test_bank_accounts_and_client_profile(tmp_path: Path) -> None:
    [bank] = map_bank_accounts(ACCOUNTS)
    assert (bank.id, bank.bsb, bank.account_no, bank.gl_account_code) == ("BA-090", "062000", "12345678", "090")
    profile = client_profile(write_snapshot(tmp_path))
    assert profile.legal_name == "Demo Company (AU)" and profile.abn == "11111111138"
    assert profile.id == "CLIENT-XERO-3C1332BB" and profile.entity_type == "company"


@asynccontextmanager
async def no_transaction(*args: Any, **kwargs: Any) -> Any:
    yield None


async def gl_balance(monkeypatch: pytest.MonkeyPatch, tables: dict[str, list[Any]], code: str) -> int:
    """调生产的 PostgresFinancialDataStore.gl_balance (只替换数据库读写), 本期 2026-07-01 至 2026-11-30.

    取自 Codex 审核 X01 的探针: 按语句参数名模拟查询结果.
    """
    store = stores.PostgresFinancialDataStore(engine=object())  # type: ignore[arg-type]
    account_rows = [a.model_dump() for a in tables["accounts"]]

    async def rows(eid: str, statement: Any) -> list[dict[str, Any]]:
        params = statement.compile().params
        if "sum(" in str(statement):
            amount = sum(
                r.debit_cents - r.credit_cents
                for r in tables["journal_entries"]
                if r.account_code == params["account_code_1"]
                and params["posting_date_1"] <= r.posting_date <= params["posting_date_2"]
            )
            return [{"amount": amount}]
        return [r for r in account_rows if "code_1" not in params or r["code"] == params["code_1"]]

    async def trial_balance(eid: str, period: str) -> list[Any]:
        return [r for r in tables["trial_balance"] if r.period == period]

    async def engagement(self: Any, eid: str) -> Any:
        return SimpleNamespace(period_start=date(2026, 7, 1))

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(store, "rows", rows)
    monkeypatch.setattr(store, "trial_balance", trial_balance)
    monkeypatch.setattr(stores.PostgresEngagementStore, "get", engagement)
    return await store.gl_balance("E", code, date(2026, 11, 30))


def test_invoice_links_reposted_receivable_line(tmp_path: Path) -> None:
    """发票冲回再过账时关联最后过账的应收行; 分录号按数值排序 (9 < 10 < 11), 不按字符串.

    取自 Codex 审核 X01 第 2 轮的探针."""
    invoice = copy.deepcopy(INVOICES[0]) | {
        "Total": 1200,
        "LineItems": [{"AccountCode": "200", "LineAmount": 1200, "TaxAmount": 0}],
    }

    def posted(number: int, amount: int) -> dict[str, Any]:
        return {
            "JournalNumber": number,
            "JournalDate": "/Date(1785542400000+0000)/",
            "CreatedDateUTC": STAMP,
            "SourceID": "I1",
            "SourceType": "ACCREC",
            "JournalLines": [{"AccountCode": "610", "NetAmount": amount}, {"AccountCode": "200", "NetAmount": -amount}],
        }

    journals = [posted(9, 1100), posted(10, -1100), posted(11, 1200)]
    [sales_invoice] = build_tables(write_snapshot(tmp_path, invoices=[invoice], journals=journals))["sales_invoices"]
    assert sales_invoice.journal_entry_id == "XJ-11-1"


def test_invoice_links_latest_posting_even_with_earlier_date(tmp_path: Path) -> None:
    """重新过账时把业务日期改早 (9 号 8-05 原记, 10 号冲回, 11 号 8-01 重记), 仍关联 11 号.

    取自 Codex 审核 X01 第 3 轮的探针."""
    invoice = copy.deepcopy(INVOICES[0]) | {
        "Status": "AUTHORISED",
        "Total": 1200,
        "LineItems": [{"AccountCode": "200", "LineAmount": 1200, "TaxAmount": 0}],
    }

    def posted(number: int, amount: int, day: int) -> dict[str, Any]:
        stamp = datetime(2026, 8, day, tzinfo=UTC)
        return {
            "JournalNumber": number,
            "JournalDate": f"/Date({int(stamp.timestamp() * 1000)}+0000)/",
            "CreatedDateUTC": STAMP,
            "SourceID": "I1",
            "SourceType": "ACCREC",
            "JournalLines": [{"AccountCode": "610", "NetAmount": amount}, {"AccountCode": "200", "NetAmount": -amount}],
        }

    journals = [posted(9, 1100, 5), posted(10, -1100, 5), posted(11, 1200, 1)]
    [sales_invoice] = build_tables(write_snapshot(tmp_path, invoices=[invoice], journals=journals))["sales_invoices"]
    assert sales_invoice.journal_entry_id == "XJ-11-1"
