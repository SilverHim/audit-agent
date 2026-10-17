"""付费接口的模拟: 网页导出 → Journals, Bank Statements Plus 的响应, 及经 XeroClient 调用. 导出文件用 openpyxl 现造."""

import datetime as dt
from pathlib import Path
from typing import Any

import httpx
import openpyxl

from backend.adapters.xero import Tokens, TokenStore, XeroClient
from backend.adapters.xero_dataset import Snapshot
from backend.adapters.xero_mock import MockPaidTransport, infer_sources, journals_from_export, statements_response
from tests.test_xero_dataset import ACCOUNTS, INVOICES, PAYMENTS, write_snapshot

HEADER = (
    "Date",
    "Journal ID",
    "Account Code",
    "Account",
    "Narration",
    "Description",
    "Debit",
    "Credit",
    "Posted Date",
    "Posted By",
)
D = dt.datetime


def journal_lines() -> list[tuple[Any, ...]]:
    posted = D(2026, 10, 4, 12, 50)
    rows: list[tuple[Any, ...]] = []

    def add(number: int, day: D, narration: str, *lines: tuple[str, float, float]) -> None:
        rows.append((f"ID {number} {narration}",))
        for code, debit, credit in lines:
            rows.append((day, number, code, "Name", narration, None, debit, credit, posted, "Someone"))

    sale = (("610", 1100.0, 0), ("200", 0, 1000.0), ("820", 0, 100.0))
    add(1, D(2026, 8, 1), "Alpha Pty", *sale)
    add(2, D(2026, 8, 1), "Reversed: Alpha Pty - Reversal of ID 1", *((c, cr, db) for c, db, cr in sale))
    add(3, D(2026, 8, 1), "Alpha Pty", *sale)  # 发票改过后重新过账
    add(4, D(2026, 7, 16), "Conversion Balance", ("090", 1000.0, 0), ("840", 0, 1000.0))
    add(5, D(2026, 8, 31), "Being depreciation - Manual", ("437", 75.0, 0), ("090", 0, 75.0))
    add(6, D(2026, 8, 15), "Odette Garrison", ("477", 2500.0, 0), ("090", 0, 2500.0))
    return rows


def write_xlsx(path: Path, rows: list[tuple[Any, ...]], sheets: int = 1) -> None:
    workbook = openpyxl.Workbook()
    for _ in range(sheets - 1):  # 报表包导出时, 明细在最后一张表
        workbook.create_sheet()
    sheet = workbook.worksheets[-1]
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def exports(tmp_path: Path) -> tuple[Path, Snapshot]:
    folder = tmp_path / "raw"
    folder.mkdir()
    write_xlsx(
        folder / "journal_report_full.xlsx", [("Journal Report",), ("Demo",), ("period",), (), HEADER, *journal_lines()]
    )
    statement = [
        ("Bank Statement",),
        ("Demo",),
        ("period",),
        ("Business Bank",),
        (),
        ("Date", "Description", "Date imported into Xero", "Reference", "Reconciled", "Source", "Amount", "Balance"),
        (),
        (D(2024, 1, 1), None, None, None, None, None, 0, 100.0),
        (D(2026, 8, 2), "Alpha", D(2026, 10, 1), " ", "Yes", "Imported", 50.0, 150.0),
        (D(2026, 9, 1), "Fee", D(2026, 10, 1), "F1", "No", "User", -10.0, 140.0),
        (D(2027, 12, 31), None, None, None, None, None, 0, 140.0),
    ]
    write_xlsx(folder / "bank_statement_090.xlsx", statement, sheets=4)
    manual = [{"ManualJournalID": "M1", "Narration": "Being depreciation", "Status": "POSTED"}]
    return folder, write_snapshot(tmp_path / "snap", manual_journals=manual)


def test_journals_from_export_infer_sources(tmp_path: Path) -> None:
    folder, snapshot = exports(tmp_path)
    journals = {j["JournalNumber"]: j for j in journals_from_export(folder / "journal_report_full.xlsx", snapshot)}
    assert [x["NetAmount"] for x in journals[1]["JournalLines"]] == [1100.0, -1000.0, -100.0]
    assert journals[1]["JournalLines"][0]["AccountID"] == "ID-610"
    assert journals[1]["CreatedDateUTC"] == "/Date(1791078600000+0000)/"  # 悉尼 12:50, 当天起夏令时 (UTC+11)
    assert "Posted By" not in str(journals[1])  # 接口没有录入人
    assert {n: (j.get("SourceType"), j.get("SourceID")) for n, j in journals.items()} == {
        1: ("ACCREC", "I1"),
        2: ("ACCREC", "I1"),  # 冲回沿用原分录
        3: ("ACCREC", "I1"),  # 同一单据重新过账
        4: (None, None),  # 系统分录没有来源
        5: ("MANJOURNAL", "M1"),  # 网页说明末尾的 " - Manual"
        6: ("PAYSLIP", None),  # 用到工资科目
    }


def test_statements_response_balances() -> None:
    lines = [
        {"date": dt.date(2026, 8, 2), "payee": "A", "reference": "", "reconciled": True, "source": "", "amount": 50.0},
        {
            "date": dt.date(2026, 9, 1),
            "payee": "F",
            "reference": "F1",
            "reconciled": False,
            "source": "",
            "amount": -10.0,
        },
    ]
    data = statements_response(ACCOUNTS[0], lines, 100.0, dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    [statement] = data["statements"]
    assert (statement["startBalance"], statement["endBalance"]) == (150.0, 140.0)
    assert [(x["postedDate"], x["amount"], x["isReconciled"], x["type"]) for x in statement["statementLines"]] == [
        ("2026-09-01", -10.0, False, "DEBIT")
    ]


async def test_mock_transport_serves_paid_endpoints_to_client(tmp_path: Path) -> None:
    folder, snapshot = exports(tmp_path)
    store = TokenStore(tmp_path / "token.json")
    store.save(Tokens("at", "rt", 9e9, "", "T1", "Demo"))
    today = dt.date(2026, 10, 17)
    transport = MockPaidTransport(folder, snapshot.root, today)
    client = XeroClient("CID", store, transport=transport, clock=lambda: D(2026, 10, 17, tzinfo=dt.UTC).timestamp())
    journals = await client.journals()
    assert [j["JournalNumber"] for j in journals] == [1, 2, 3, 4, 5, 6]
    data = await client.bank_statements("ID-090", dt.date(2026, 7, 1), dt.date(2026, 11, 30))  # 截止日截到今天
    [statement] = data["statements"]
    assert (
        statement["endDate"] == "2026-10-17" and statement["startBalance"] == 100.0 and statement["endBalance"] == 140.0
    )
    assert len(statement["statementLines"]) == 2


async def test_mock_transport_rejects_like_real_api(tmp_path: Path) -> None:
    folder, snapshot = exports(tmp_path)
    transport = MockPaidTransport(folder, snapshot.root, dt.date(2026, 10, 17))
    url = "https://api.xero.com/finance.xro/1.0/BankStatementsPlus/statements"

    async def status(**params: str) -> int:
        response = await transport.handle_async_request(httpx.Request("GET", url, params=params))
        return response.status_code

    assert await status(BankAccountID="ID-090", FromDate="2025-07-01", ToDate="2026-07-01") == 400  # 超过 12 个月
    assert await status(BankAccountID="ID-090", FromDate="2026-07-01", ToDate="2026-10-18") == 400  # 截止日在未来
    assert await status(BankAccountID="NOPE", FromDate="2026-07-01", ToDate="2026-09-30") == 400
    assert await status(BankAccountID="ID-090", FromDate="2025-10-18", ToDate="2026-10-17") == 200
    page = await transport.handle_async_request(
        httpx.Request("GET", "https://api.xero.com/api.xro/2.0/Journals", params={"offset": "4"})
    )
    assert [j["JournalNumber"] for j in page.json()["Journals"]] == [5, 6]


def journal(number: int, day: dt.date, reference: str, lines: dict[str, float]) -> dict[str, Any]:
    stamp = dt.datetime.combine(day, dt.time(), dt.UTC)
    return {
        "JournalNumber": number,
        "JournalDate": f"/Date({int(stamp.timestamp() * 1000)}+0000)/",
        "Reference": reference,
        "JournalLines": [{"AccountCode": code, "NetAmount": amount} for code, amount in lines.items()],
    }


def test_receipt_not_matched_to_same_day_invoice(tmp_path: Path) -> None:
    """同日同额的销售发票与收款并存时, 收款分录 (借银行贷应收) 只在收款里找 (Codex 审核 X01 的探针)."""
    payment = PAYMENTS[0] | {"Date": "/Date(1785542400000+0000)/"}  # 2026-08-01, 与发票同日
    snapshot = write_snapshot(tmp_path, invoices=[INVOICES[0]], payments=[payment], bank_transactions=[])
    receipt = journal(1, dt.date(2026, 8, 1), "Payment: Alpha Pty", {"090": 1100.0, "610": -1100.0})
    infer_sources([receipt], snapshot)
    assert (receipt["SourceType"], receipt["SourceID"]) == ("ACCRECPAYMENT", "P1")


def test_unmatched_journals_keep_type_without_document(tmp_path: Path) -> None:
    """只有往来名称相符 (金额不同) 不匹配单据, 只记推出的类型; 报销付款按未付报销科目识别."""
    snapshot = write_snapshot(tmp_path, invoices=[INVOICES[0]], payments=[], bank_transactions=[])
    edited = journal(1, dt.date(2026, 9, 1), "Alpha Pty", {"610": 999.0, "200": -999.0})
    claim = journal(2, dt.date(2026, 10, 1), "Payment to Alpha Pty", {"801": 20.0, "090": -20.0})
    infer_sources([edited, claim], snapshot)
    assert (edited["SourceType"], edited.get("SourceID")) == ("ACCREC", None)
    assert (claim["SourceType"], claim.get("SourceID")) == ("EXPPAYMENT", None)
