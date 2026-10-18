"""需求 R13 账表导入的解析: 读 CSV, XLSX, XLS, 建议表种类与列对应, 单元格转换, 整表转换. 不连数据库."""

import csv
import io
from datetime import UTC, date, datetime
from pathlib import Path

import openpyxl
import pytest

from backend.contracts.data import ApprovalLogRow, TableKind
from backend.contracts.records import Account, JournalEntry
from backend.persistence.stores import FINANCIAL_ROWS
from backend.services import data_import as d
from backend.services import table_plan as tp

DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import"


def sheet_of(text: str, name: str = "x.csv") -> d.Sheet:
    return d.read_sheet(name, text.encode("utf-8"))


@pytest.mark.parametrize("kind", [k for k in TableKind if k is not TableKind.approval_log])
def test_demo_csv_parses_like_csv_loader(kind: TableKind) -> None:
    """data/demo 的 CSV: 建议的表种类与对应都对, 转换结果与 CsvDatasetLoader 的解析一致."""
    path = DEMO / f"{kind}.csv"
    data = path.read_bytes()
    sheet = d.read_sheet(path.name, data)
    preview = d.preview("IMP-1", path.name, d.sheet_names(path.name, data), sheet)
    assert preview.suggested_kind is kind
    assert preview.sheets == [kind.value] and preview.row_count == len(sheet.rows)

    rows, failures = tp.convert(kind, sheet, preview.suggested_mapping, preview.suggested_date_format)

    assert failures == []
    with path.open(newline="", encoding="utf-8") as stream:
        expected = [
            FINANCIAL_ROWS[kind].model_validate({k: (v if v != "" else None) for k, v in r.items()})
            for r in csv.DictReader(stream)
        ]
    assert rows == expected


def test_amounts_in_dollars_and_cents() -> None:
    assert d.parse_amount("1,234.56", cents=False) == 123456
    assert d.parse_amount("(12.50)", cents=False) == -1250
    assert d.parse_amount("12.50-", cents=False) == -1250
    assert d.parse_amount("$5", cents=False) == 500
    assert d.parse_amount("AUD 7.1", cents=False) == 710
    assert d.parse_amount("12.345", cents=False) == 1235  # 四舍五入到分
    assert d.parse_amount(0.1 + 0.2, cents=False) == 30  # Excel 浮点数
    assert d.parse_amount(" ", cents=False) is None
    assert d.parse_amount("125840000", cents=True) == 125840000
    assert d.parse_amount(-1500, cents=True) == -1500
    for bad, cents in (("1.5", True), ("abc", False), ("1e400", False), (True, False)):
        with pytest.raises(d.CellError):
            d.parse_amount(bad, cents=cents)


def test_dates_by_format_iso_and_month_names() -> None:
    assert d.parse_date("01/07/2025", "dmy") == date(2025, 7, 1)
    assert d.parse_date("07/01/2025", "mdy") == date(2025, 7, 1)
    assert d.parse_date("2025-07-01", "dmy") == date(2025, 7, 1)  # 年在前总按 ISO
    assert d.parse_date("25/7/1", "ymd") == date(2025, 7, 1)
    assert d.parse_date("1 Jul 2025", "mdy") == date(2025, 7, 1)
    assert d.parse_date("01-July-25", "dmy") == date(2025, 7, 1)
    assert d.parse_date("01/07/2025 00:00", "dmy") == date(2025, 7, 1)
    assert d.parse_date(datetime(2025, 7, 1, 9), "dmy") == date(2025, 7, 1)
    assert d.parse_date("", "dmy") is None
    for bad in ("31/02/2025", "next week", "13/13/2025"):
        with pytest.raises(d.CellError):
            d.parse_date(bad, "dmy")


def test_datetimes_are_utc_with_sydney_default() -> None:
    assert d.parse_datetime("2025-07-15T06:01:00Z", "dmy") == datetime(2025, 7, 15, 6, 1, tzinfo=UTC)
    # 悉尼 7 月为 UTC+10, 1 月夏令时为 UTC+11
    assert d.parse_datetime("15/07/2025 4:01 PM", "dmy") == datetime(2025, 7, 15, 6, 1, tzinfo=UTC)
    assert d.parse_datetime("15/07/2025", "dmy") == datetime(2025, 7, 14, 14, 0, tzinfo=UTC)
    assert d.parse_datetime(datetime(2026, 1, 15, 10, 0), "dmy") == datetime(2026, 1, 14, 23, 0, tzinfo=UTC)
    with pytest.raises(d.CellError):
        d.parse_datetime("15/07/2025 25:61", "dmy")


def test_bools_and_text() -> None:
    assert [d.parse_bool(v) for v in ("Yes", "n", "1", "FALSE", "", True)] == [True, False, True, False, None, True]
    with pytest.raises(d.CellError):
        d.parse_bool("maybe")
    assert d.cell_text(1010.0) == "1010"  # Excel 把科目代码存成数值
    assert d.cell_text(datetime(2025, 7, 1)) == "2025-07-01"
    assert d.cell_text("  a ") == "a"


def test_xlsx_sheets_numbers_and_dates() -> None:
    book = openpyxl.Workbook()
    notes = book.active
    assert notes is not None
    notes.title = "Notes"
    notes.append(["just notes"])
    sheet = book.create_sheet("Ledger")
    sheet.append([])
    sheet.append(["Journal No", "Date", "Account", "Debit", "Credit"])
    sheet.append(["J1", datetime(2025, 7, 15), 1010, 12.5, None])
    sheet.append(["J1", datetime(2025, 7, 15), 4100.0, None, 12.5])
    stream = io.BytesIO()
    book.save(stream)
    data = stream.getvalue()

    assert d.sheet_names("gl.xlsx", data) == ["Notes", "Ledger"]
    ledger = d.read_sheet("gl.xlsx", data, "Ledger")
    assert ledger.columns == ["Journal No", "Date", "Account", "Debit", "Credit"]
    assert [n for n, _ in ledger.rows] == [3, 4]  # 实际行号, 前面的空行也算
    preview = d.preview("IMP-1", "gl.xlsx", ["Notes", "Ledger"], ledger)
    assert preview.suggested_kind is TableKind.journal_entries
    rows, failures = tp.convert(TableKind.journal_entries, ledger, preview.suggested_mapping, "dmy")
    assert failures == []
    assert [(r.id, r.account_code, r.debit_cents, r.credit_cents) for r in rows if isinstance(r, JournalEntry)] == [
        ("J1-1", "1010", 1250, 0),
        ("J1-2", "4100", 0, 1250),
    ]
    with pytest.raises(d.UnreadableFile):
        d.read_sheet("gl.xlsx", data, "Missing")


def test_xls_fixture() -> None:
    data = (FIXTURES / "accounts.xls").read_bytes()
    sheet = d.read_sheet("accounts.xls", data)
    preview = d.preview("IMP-1", "accounts.xls", d.sheet_names("accounts.xls", data), sheet)
    assert preview.suggested_kind is TableKind.accounts
    rows, failures = tp.convert(TableKind.accounts, sheet, preview.suggested_mapping, "dmy")
    assert failures == [] and rows[0] == Account(code="1010", name="Cash at bank", type="asset")


def test_unsupported_and_broken_files() -> None:
    with pytest.raises(d.UnreadableFile):
        d.sheet_names("ledger.pdf", b"%PDF")
    with pytest.raises(d.UnreadableFile):
        d.read_sheet("ledger.xlsx", b"not a zip")


def test_mapping_errors() -> None:
    sheet = sheet_of("code,name,type\n1010,Cash,asset\n")
    with pytest.raises(d.MappingError) as e:
        tp.convert(TableKind.accounts, sheet, {"code": "code", "name": "name", "type": "type", "colour": "x"}, "dmy")
    assert e.value.code == "INVALID_VALUE"
    with pytest.raises(d.MappingError) as e:
        tp.convert(TableKind.accounts, sheet, {"code": "code", "name": "nope", "type": "type"}, "dmy")
    assert e.value.code == "INVALID_VALUE"
    with pytest.raises(d.MappingError) as e:
        tp.convert(TableKind.accounts, sheet, {"code": "code", "name": "name", "type": None}, "dmy")
    assert e.value.code == "MISSING_FIELD"
    journal = sheet_of("entry,date,account\nJ1,2025-07-01,1010\n")
    mapping = {"entry_no": "entry", "posting_date": "date", "account_code": "account"}
    with pytest.raises(d.MappingError) as e:
        tp.convert(TableKind.journal_entries, journal, mapping, "dmy")
    assert e.value.code == "MISSING_FIELD" and "debit_cents" in e.value.message


def test_failures_report_row_column_and_value() -> None:
    text = "period,account,balance\ncurrent,1010,1.00\nlast year,1010,abc\nCY,1010,2\nnext,2000,3\n"
    sheet = sheet_of(text)
    mapping = {"period": "period", "account_code": "account", "balance_cents": "balance"}

    rows, failures = tp.convert(TableKind.trial_balance, sheet, mapping, "dmy")

    assert [(f.row, f.column, f.value) for f in failures] == [
        (3, "balance", "abc"),
        (4, "account", "current/1010"),  # 与第 2 行重复的唯一键
        (5, "period", "next"),
    ]
    assert len(rows) == 1


def test_account_type_synonyms() -> None:
    sheet = sheet_of("code,name,type\n1,A,Assets\n2,B,Income\n3,C,Expenses\n")
    rows, failures = tp.convert(TableKind.accounts, sheet, {"code": "code", "name": "name", "type": "type"}, "dmy")
    assert failures == [] and [r.type for r in rows if isinstance(r, Account)] == ["asset", "revenue", "expense"]


def test_generated_ids_and_approval_log_only_mapped_fields() -> None:
    bank = sheet_of(
        "account,date,amount,balance\nBA-01,2025-07-01,10,10\nBA-01,2025-07-02,5,15\nBA-02,2025-07-01,1,1\n"
    )
    mapping = {
        "bank_account_id": "account",
        "txn_date": "date",
        "amount_cents": "amount",
        "balance_after_cents": "balance",
    }
    rows, _ = tp.convert(TableKind.bank_transactions, bank, mapping, "ymd")
    assert [r.id for r in rows] == ["BA-01-00001", "BA-01-00002", "BA-02-00001"]

    log = sheet_of("entry,approver\nJ1,cfo\nJ2,\n")
    rows, failures = tp.convert(TableKind.approval_log, log, {"entry_no": "entry", "approved_by": "approver"}, "dmy")
    assert failures == []
    assert [r.model_dump(exclude_unset=True) for r in rows] == [
        {"entry_no": "J1", "approved_by": "cfo"},
        {"entry_no": "J2", "approved_by": None},
    ]
    assert all(isinstance(r, ApprovalLogRow) for r in rows)


def test_suggestions_for_unclear_files() -> None:
    sheet = sheet_of("foo,bar\n1,2\n")
    preview = d.preview("IMP-1", "export.csv", ["export"], sheet)
    assert (preview.suggested_kind, preview.suggested_mapping) == (None, {})
    assert preview.sample == {"foo": "1", "bar": "2"}
    assert d.suggest_date_format(["13/01/2025"]) == "dmy"
    assert d.suggest_date_format(["01/13/2025", "02/14/2025"]) == "mdy"
    assert d.suggest_date_format(["2025-01-13"]) == "ymd"
    assert d.suggest_date_format(["01/02/2025"]) == "dmy"  # 定不了取澳洲习惯


def test_csv_semicolon_bom_and_duplicate_headers() -> None:
    sheet = d.read_sheet("x.csv", "﻿Code;Name;Name\n1010;Cash;Bank\n".encode())
    assert sheet.columns == ["Code", "Name", "Name (2)"]
    assert sheet.rows == [(2, ["1010", "Cash", "Bank"])]
    latin = d.read_sheet("x.csv", "code,name\n1,Caf\xe9\n".encode("cp1252"))
    assert latin.rows[0][1][1] == "Café"


# ── 审核 FD-R13 第 1 轮后补充 (多数来自 Codex 的探针) ──


def test_time_after_short_date_and_explicit_offset() -> None:
    assert d.parse_datetime("1/7/25 4:01 PM", "dmy") == datetime(2025, 7, 1, 6, 1, tzinfo=UTC)
    assert d.parse_datetime("15/07/2025 16:01+00:00", "dmy") == datetime(2025, 7, 15, 16, 1, tzinfo=UTC)
    assert d.parse_datetime("15 Jul 2025 16:01 -0230", "dmy") == datetime(2025, 7, 15, 18, 31, tzinfo=UTC)
    assert d.parse_datetime("2025-07-15T12:00:00.5Z", "mdy") == datetime(2025, 7, 15, 12, 0, 0, 500000, tzinfo=UTC)
    assert d.parse_datetime("15/07/2025 12:00:00.1234567+00:00", "dmy") == datetime(
        2025, 7, 15, 12, 0, 0, 123456, tzinfo=UTC
    )  # 小数秒保留到微秒
    assert d.parse_datetime("2025-07-15T12:00:00.123456Z", "dmy") == d.parse_datetime(
        datetime(2025, 7, 15, 22, 0, 0, 123456), "dmy"
    )  # 文本与等值的 Excel 单元格一致 (悉尼 7 月为 UTC+10)
    assert d.parse_datetime("2025-07-15 00:00+14:00", "dmy") == datetime(2025, 7, 14, 10, 0, tzinfo=UTC)  # 边界
    assert d.parse_datetime("15/07/2025 12:00 AM", "dmy") == datetime(2025, 7, 14, 14, 0, tzinfo=UTC)  # 午夜


@pytest.mark.parametrize(
    "value",
    [
        "15/07/2025 13:00 PM",
        "15/07/2025 0:30 AM",
        "2025-07-15 16:01+99:00",
        "2025-07-15 16:01+14:01",
        "2025-07-15 16:01-1459",
        "2025-07-15 rubbish",
        "2025-07-15 25:00",
    ],
)
def test_invalid_datetimes_are_failures(value: str) -> None:
    with pytest.raises(d.CellError):
        d.parse_datetime(value, "dmy")


@pytest.mark.parametrize("value", ["01/07/20250", "1 Junk 2025", "01/07/202", "2025-07-01 extra", "1/7"])
def test_invalid_dates_are_failures(value: str) -> None:
    with pytest.raises(d.CellError):
        d.parse_date(value, "dmy")


def test_month_names_full_abbreviated_and_sept() -> None:
    assert d.parse_date("1 September 2025", "dmy") == date(2025, 9, 1)
    assert d.parse_date("1-Sept-25", "mdy") == date(2025, 9, 1)
    assert d.parse_date("1 sep. 2025", "dmy") == date(2025, 9, 1)


def test_sydney_dst_gap_is_a_failure_and_overlap_takes_first() -> None:
    with pytest.raises(d.CellError):
        d.parse_datetime("2025-10-05 02:30", "dmy")  # 夏令时拨快, 悉尼没有 2:30
    with pytest.raises(d.CellError):
        d.parse_datetime(datetime(2025, 10, 5, 2, 30), "dmy")  # Excel 单元格同样检查
    # 夏令时结束时 2:30 出现两次, 取第一次 (夏令时 +11)
    assert d.parse_datetime("2026-04-05 02:30", "dmy") == datetime(2026, 4, 4, 15, 30, tzinfo=UTC)


def test_column_names_unique_and_preview_matches_conversion() -> None:
    sheet = d.read_sheet("x.csv", b"code,name,name,name (2),type,\n1,A,B,C,asset,\n")
    assert sheet.columns == ["code", "name", "name (3)", "name (2)", "type"]
    assert sheet.headers == ["code", "name", "name", "name (2)", "type"]
    preview = d.preview("IMP-1", "x.csv", ["x"], sheet)
    mapping = {"code": "code", "name": "name (2)", "type": "type"}
    rows, failures = tp.convert(TableKind.accounts, sheet, mapping, "dmy")
    assert failures == [] and preview.sample["name (2)"] == "C" == rows[0].name


def test_duplicate_cents_column_keeps_units() -> None:
    """重名列的显示名加了后缀, 金额单位仍按原始表头 balance_cents 读成分."""
    sheet = d.read_sheet("tb.csv", b"period,account,balance_cents,balance_cents\ncurrent,1010,100,200\n")
    mapping = {"period": "period", "account_code": "account", "balance_cents": "balance_cents (2)"}
    rows, failures = tp.convert(TableKind.trial_balance, sheet, mapping, "dmy")
    assert failures == [] and rows[0].balance_cents == 200


def test_huge_amounts_are_failures() -> None:
    sheet = d.read_sheet("tb.csv", b"period,account,balance\ncurrent,1010,1e1000000\nprior,1010,1e30\n")
    mapping = {"period": "period", "account_code": "account", "balance_cents": "balance"}
    _, failures = tp.convert(TableKind.trial_balance, sheet, mapping, "dmy")
    assert [(f.row, f.column, f.value) for f in failures] == [(2, "balance", "1e1000000"), (3, "balance", "1e30")]
    with pytest.raises(d.CellError):
        d.parse_amount("9" * 400, cents=True)
    for bad in ("(1e1000000)", "1e1000000-", "(sNaN)", "-1e999999"):  # 负数写法与非有限值同样是解析失败
        with pytest.raises(d.CellError):
            d.parse_amount(bad, cents=False)
    assert d.parse_amount("(1e2)", cents=False) == -10000


def test_full_ledger_with_approval_columns_is_suggested_as_journal() -> None:
    columns = [
        "entry_no", "posting_date", "account_code", "debit_cents", "credit_cents",
        "created_by", "approved_by", "approved_at",
    ]  # fmt: skip
    assert d.suggest_kind("export.csv", columns) is TableKind.journal_entries
    assert d.suggest_kind("export.csv", ["entry_no", "created_by", "approved_by", "approved_at"]) is (
        TableKind.approval_log
    )
