"""需求 R275 规整方案与确定性套用: 手写方案套 Xero 网页导出与 Numbat CSV, 结果与快照, 按列对应一致. 不连库, 不调大模型.

journal_report.xlsx 的 Posted By 列是导出账号的姓名 (计划决定 12): 读出后先在内存里遮蔽该列再套用, 方案也不对应它,
测试与断言消息里不会出现该列的值.
"""

from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.adapters.xero_dataset import Snapshot, account_class, build_tables, trial_balance_cents
from backend.contracts.data import FieldSource, ParseFailure, PeriodSource, TableKind, TablePlan
from backend.contracts.llm_outputs import SheetRecognition
from backend.contracts.records import Account, JournalEntry, SalesInvoice, TrialBalanceRow
from backend.services import data_import as d
from backend.services import table_plan as tp

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "data" / "external" / "xero" / "raw"  # Xero 网页导出, 只读
# 方案样例: 账表识别 (DOC-SHEET) 的固定输出, 键为导出文件或 Numbat 表的文件名主干, 方案取其 plan (R276 复用, 只存一份)
PLANS = Path(__file__).resolve().parent / "fixtures" / "llm_variants" / "upload" / "DOC-SHEET"
Q1 = Snapshot(REPO / "data" / "external" / "xero" / "api" / "20261017-q1")
MOCK = Snapshot(REPO / "data" / "external" / "xero" / "api" / "20261017-mock")
PRIVATE_COLUMN = "Posted By"  # 导出账号的姓名, 读出即遮蔽


def plan_of(key: str) -> TablePlan:
    return SheetRecognition.model_validate_json((PLANS / f"{key}.json").read_text(encoding="utf-8")).plan


def masked(rows: d.RawRows) -> d.RawRows:
    """遮蔽 Posted By 列: 找到表头里的这一列, 之后各行该列置空 (没有这一列时原样返回)."""
    for index, (_, cells) in enumerate(rows):
        if PRIVATE_COLUMN in [d.cell_text(c) for c in cells]:
            position = [d.cell_text(c) for c in cells].index(PRIVATE_COLUMN)
            return rows[: index + 1] + [
                (n, [None if i == position else c for i, c in enumerate(cells)]) for n, cells in rows[index + 1 :]
            ]
    return rows


def raw(file_name: str, root: Path = RAW) -> tuple[str, d.RawRows]:
    """原样读出的工作表 (遮蔽 Posted By)."""
    name, rows = d.raw_rows(file_name, (root / file_name).read_bytes())
    return name, masked(rows)


def apply(plan: TablePlan, file_name: str) -> tuple[list[Any], list[ParseFailure]]:
    return tp.apply_plan(plan, *raw(file_name))


def edited(rows: d.RawRows, number: int, column: int, value: d.Cell) -> d.RawRows:
    """改一个单元格的副本 (只在内存里, 原始文件不动)."""
    return [(n, [value if (n, i) == (number, column) else c for i, c in enumerate(cells)]) for n, cells in rows]


@cache
def q1_tables() -> dict[str, list[Any]]:
    return build_tables(Q1)


# ── 验收 1: 试算表, 本期借贷两列, 上期一列 ──


def test_trial_balance_periods_from_columns() -> None:
    rows, failures = apply(plan_of("trial_balance_2026-06-30"), "trial_balance_2026-06-30.xlsx")

    assert failures == [] and all(isinstance(r, TrialBalanceRow) for r in rows)
    current = {r.account_code: r.balance_cents for r in rows if r.period == "current"}
    prior = {r.account_code: r.balance_cents for r in rows if r.period == "prior"}
    assert len(rows) == 12 and set(current) == set(prior)  # 6 个科目, 本期上期各 6 行; 标题行与 Total 行不出行
    assert current == trial_balance_cents(Q1.trial_balance(date(2026, 6, 30)))  # 与快照的 06-30 试算表一致
    assert current == {"200": -420000, "412": 360000, "453": 800000, "461": 51200, "090": -870320, "820": 79120}
    assert set(prior.values()) == {0}  # 30 June 2025 一列全为 0


def test_trial_balance_plan_carries_over_to_another_period() -> None:
    """同一方案换上期列名 (30 June 2026) 套到 11-30 的试算表: 本期与上期都与快照一致, 上期有正负号的余额."""
    plan = plan_of("trial_balance_2026-06-30")
    prior = PeriodSource(period="prior", balance=FieldSource(column="30 June 2026"))
    rows, failures = apply(
        plan.model_copy(update={"periods": [plan.periods[0], prior]}), "trial_balance_2026-11-30.xlsx"
    )

    assert failures == []
    current = {r.account_code: r.balance_cents for r in rows if r.period == "current"}
    earlier = {r.account_code: r.balance_cents for r in rows if r.period == "prior" and r.balance_cents}
    assert current == trial_balance_cents(MOCK.trial_balance(date(2026, 11, 30))) and len(current) == 32
    assert earlier == {k: v for k, v in trial_balance_cents(MOCK.trial_balance(date(2026, 6, 30))).items() if v}
    assert earlier["200"] == -420000 and earlier["412"] == 360000  # 贷方余额为负


# ── 验收 2: 总账分录, 分组标题向下填充 ──


def test_journal_report_groups_fill_down() -> None:
    rows, failures = apply(plan_of("journal_report"), "journal_report.xlsx")

    assert failures == [] and all(isinstance(r, JournalEntry) for r in rows)
    assert len(rows) == 1017 and len({r.entry_no for r in rows}) == 368
    assert sum(r.debit_cents for r in rows) == sum(r.credit_cents for r in rows) == 43969507  # 借贷平衡
    assert all(r.created_by is None for r in rows)  # Posted By 不对应
    by_entry: dict[str, list[JournalEntry]] = defaultdict(list)
    for r in rows:
        by_entry[r.entry_no].append(r)
    opening = by_entry["756"]
    assert [r.id for r in opening] == [f"756-{n}" for n in range(1, 7)]
    assert {r.description for r in opening} == {"Inventory Opening Balance"}  # 组标题 "ID 756 ..." 的摘要
    assert {r.description for r in by_entry["755"]} == {"DIISR - Small Business Services"}
    assert all(r.description for r in rows)
    # 与快照的分录逐笔一致: 各行 (科目, 借减贷), 入账日期, 录入时间 (Posted Date 为悉尼时间, 存 UTC)
    snapshot: dict[str, list[JournalEntry]] = defaultdict(list)
    for line in q1_tables()["journal_entries"]:
        snapshot[line.entry_no].append(line)
    assert {f"XJ-{no}" for no in by_entry} == set(snapshot)
    for no, lines in by_entry.items():
        theirs = snapshot[f"XJ-{no}"]
        assert Counter((r.account_code, r.debit_cents - r.credit_cents) for r in lines) == Counter(
            (r.account_code, r.debit_cents - r.credit_cents) for r in theirs
        ), no
        assert {(r.posting_date, r.created_at) for r in lines} == {(r.posting_date, r.created_at) for r in theirs}, no
    assert opening[0].created_at == datetime(2026, 10, 17, 2, 4, 10, 600000, tzinfo=UTC)


# ── 验收 3: 科目表, 类型取值映射 ──


def test_chart_of_accounts_maps_xero_types() -> None:
    rows, failures = apply(plan_of("chart_of_accounts"), "chart_of_accounts.csv")

    assert all(isinstance(r, Account) for r in rows)
    # 文件 59 个科目: 58 个映射到五类; 803 Wage Payables 的 Type 为空, 记为失败处 (第 44 行), 不猜也不丢
    assert len(rows) == 58 and failures == [ParseFailure(row=44, column="*Type", value="")]
    api = {a["Code"]: account_class(a["Type"]) for a in Q1.load("accounts") if a.get("Code")}
    assert {r.code: r.type for r in rows} == {code: api[code] for code in {r.code for r in rows}}  # 与 API 的类别一致
    assert Counter(r.type for r in rows) == {"expense": 29, "liability": 16, "asset": 8, "revenue": 3, "equity": 2}


def test_value_outside_the_map_is_a_failure() -> None:
    """映射表外的值算失败, 即使类型转换本身认得它 ("asset" 是合法的科目类别, 但不是 Xero 的类型名)."""
    name, rows = raw("chart_of_accounts.csv")
    changed = edited(rows, 2, 2, "asset")  # 090 的 Type
    accounts, failures = tp.apply_plan(plan_of("chart_of_accounts"), name, changed)

    assert failures[0] == ParseFailure(row=2, column="*Type", value="asset") and len(accounts) == 57
    assert "090" not in {a.code for a in accounts}


def test_blank_value_with_a_map_is_empty() -> None:
    """取值映射只管非空的值: 可选字段的空单元格取空, 不算失败."""
    rows = sheet_rows("No,Date,Account,Debit,Kind\nJ1,2025-07-01,1010,5,\nJ1,2025-07-01,4100,-5,Manual\n")
    plan = TablePlan(
        kind=TableKind.journal_entries,
        fields={
            "entry_no": FieldSource(column="No"),
            "posting_date": FieldSource(column="Date"),
            "account_code": FieldSource(column="Account"),
            "debit_cents": FieldSource(column="Debit"),
            "is_adjustment": FieldSource(column="Kind", values={"Manual": "true"}),
        },
        date_format="ymd",
    )
    result, failures = tp.apply_plan(plan, "x", rows)
    assert failures == [] and [r.is_adjustment for r in result] == [False, True]


# ── 验收 4: 销售发票, 合计行跳过, 明细合并 ──


def test_receivable_invoice_detail_matches_snapshot() -> None:
    rows, failures = apply(plan_of("receivable_invoice_detail"), "receivable_invoice_detail.xlsx")

    assert failures == [] and all(isinstance(r, SalesInvoice) for r in rows)
    snapshot = q1_tables()["sales_invoices"]
    assert len(rows) == len(snapshot) == 42  # 已生效的发票; 贷项通知单, 草稿, 待审, 作废的跳过
    assert {r.invoice_no: (r.amount_cents, r.invoice_date, r.posting_date, r.customer) for r in rows} == {
        s.invoice_no: (s.amount_cents, s.invoice_date, s.posting_date, s.customer) for s in snapshot
    }
    assert sum(r.amount_cents for r in rows) == 7329565
    assert not any(r.invoice_no.startswith("Total") for r in rows)
    assert [r.id for r in rows].count("ORC1006") == 1  # 三个明细行合成一张发票
    assert next(r for r in rows if r.invoice_no == "ORC1006").amount_cents == 148500


def test_dedupe_keeps_identical_lines_only() -> None:
    """明细行的发票合计不同 (方案外的情形) 时仍算重复键失败; 不开 dedupe 时相同的明细行也算失败."""
    name, rows = raw("receivable_invoice_detail.xlsx")
    plan = plan_of("receivable_invoice_detail")
    title = next(n for n, cells in rows if d.cell_text(cells[0]) == "ORC1006")  # ORC1006 有三个明细行
    second, total = title + 2, 10  # 第二个明细行, Invoice Total 所在列
    _, failures = tp.apply_plan(plan, name, edited(rows, second, total, 1.0))
    assert len(failures) == 1 and failures[0].row == second and failures[0].column == "id"
    _, failures = tp.apply_plan(plan.model_copy(update={"dedupe": False}), name, rows)
    assert [f.value for f in failures] == ["ORC1006", "ORC1006", "ORC1036", "ORC1039"]  # 生效发票中多行的三张


# ── 验收 5: Numbat 标准列, 恒等方案与按列对应一致 ──


@pytest.mark.parametrize(
    "kind", [k for k in TableKind if (REPO / "data" / "demo" / f"{k}.csv").is_file()]
)  # data/demo 的 7 张表
def test_numbat_identity_plans_equal_mapping(kind: TableKind) -> None:
    path = REPO / "data" / "demo" / f"{kind}.csv"
    data = path.read_bytes()
    sheet = d.read_sheet(path.name, data)
    preview = d.preview("IMP-1", path.name, [kind.value], sheet)
    plan = plan_of(kind.value)

    assert plan == tp.mapping_plan(kind, preview.suggested_mapping, preview.suggested_date_format)
    rows, failures = tp.apply_plan(plan, *d.raw_rows(path.name, data))
    expected, expected_failures = tp.convert(kind, sheet, preview.suggested_mapping, preview.suggested_date_format)
    assert failures == expected_failures == [] and rows == expected and rows


# ── 验收 6: 方案与表对不上, 解析失败的行号 ──


def test_plan_mismatches_are_reported() -> None:
    plan = plan_of("trial_balance_2026-06-30")
    with pytest.raises(d.MappingError) as e:
        apply(plan.model_copy(update={"header_row": 99}), "trial_balance_2026-06-30.xlsx")
    assert (e.value.code, e.value.message) == ("INVALID_VALUE", "表头行 99 超出工作表 (共 12 行)")
    with pytest.raises(d.MappingError) as e:
        apply(plan.model_copy(update={"header_row": 4}), "trial_balance_2026-06-30.xlsx")
    assert (e.value.code, e.value.message) == ("INVALID_VALUE", "表头行 4 是空行")
    with pytest.raises(d.MappingError) as e:
        apply(plan.model_copy(update={"header_row": 3}), "trial_balance_2026-06-30.xlsx")  # 标题行当表头: 列都对不上
    assert e.value.code == "INVALID_VALUE" and e.value.message.startswith("工作表没有列 30 June 2025, Account Code")
    journal = plan_of("journal_report")
    fields = {k: v for k, v in journal.fields.items() if k != "account_code"}
    with pytest.raises(d.MappingError) as e:
        apply(journal.model_copy(update={"fields": fields}), "journal_report.xlsx")
    assert (e.value.code, e.value.message) == ("MISSING_FIELD", "必须对应的字段没有对应: account_code")


def test_failures_carry_file_line_numbers() -> None:
    name, rows = raw("trial_balance_2026-06-30.xlsx")
    changed = edited(edited(rows, 7, 3, "abc"), 9, 5, "n/a")  # 412 的本期借方, 461 的上期余额
    result, failures = tp.apply_plan(plan_of("trial_balance_2026-06-30"), name, changed)

    assert failures == [
        ParseFailure(row=7, column="Debit - Year to date", value="abc"),
        ParseFailure(row=9, column="30 June 2025", value="n/a"),
    ]
    assert len(result) == 10  # 412 只出上期, 461 只出本期


@pytest.mark.parametrize(
    ("update", "code", "message"),
    [
        ({"kind": None, "fields": {}, "periods": []}, "INVALID_VALUE", "方案为不是账表, 不能套用"),
        ({"kind": TableKind.accounts}, "INVALID_VALUE", "accounts 没有字段 account_code"),
        (
            {"fields": {"account_code": FieldSource(column="Account Code"), "period": FieldSource(column="Account")}},
            "INVALID_VALUE",
            "按期分列时不能另对应 period",
        ),
        (
            {"fields": {"account_code": FieldSource(debit="Account", credit="Account Type")}},
            "INVALID_VALUE",
            "借贷两列与拆正负只用于金额字段: account_code",
        ),
        (
            {"fields": {"account_code": FieldSource(column="Account Code", values={"A-1": "x", "a1": "y"})}},
            "INVALID_VALUE",
            "取值映射的原文规范化后重复: account_code",
        ),
        ({"periods": []}, "MISSING_FIELD", "必须对应的字段没有对应: period, balance_cents"),
    ],
)
def test_plan_checks(update: dict[str, Any], code: str, message: str) -> None:
    plan = plan_of("trial_balance_2026-06-30").model_copy(update=update)
    with pytest.raises(d.MappingError) as e:
        apply(plan, "trial_balance_2026-06-30.xlsx")
    assert (e.value.code, e.value.message) == (code, message)


@pytest.mark.parametrize(
    "update",
    [
        {"skip": [{"column": "Nope", "pattern": "^Total$"}]},
        {"group_title": {"column": "Nope", "pattern": "^(?P<x>.*)$"}},
    ],
)
def test_rule_columns_must_exist(update: dict[str, Any]) -> None:
    """跳过规则与分组标题用到的列不在表头里时报错, 不在逐行时崩溃."""
    plan = TablePlan.model_validate({**plan_of("trial_balance_2026-06-30").model_dump(), **update})
    with pytest.raises(d.MappingError) as e:
        apply(plan, "trial_balance_2026-06-30.xlsx")
    assert (e.value.code, e.value.message) == ("INVALID_VALUE", "工作表没有列 Nope")


def test_unknown_fields_in_a_mapping_are_rejected_even_when_empty() -> None:
    """按列对应里不属于该表的字段, 值为空 (不对应) 也报, 且先于列不存在报出 (同原 check_mapping)."""
    sheet = d.read_sheet("x.csv", b"code,name,type\n1010,Cash,asset\n")
    mapping: dict[str, str | None] = {"code": "code", "name": "name", "type": "type", "colour": None}
    with pytest.raises(d.MappingError) as e:
        tp.convert(TableKind.accounts, sheet, mapping, "dmy")
    assert (e.value.code, e.value.message) == ("INVALID_VALUE", "accounts 没有字段 colour")
    with pytest.raises(d.MappingError) as e:
        tp.convert(TableKind.accounts, sheet, {**mapping, "name": "nope", "zzz": "code"}, "dmy")
    assert e.value.message == "accounts 没有字段 colour, zzz"


def test_same_failure_is_recorded_once() -> None:
    """试算表分期时一行的行模型校验失败只记一处, 不按期重复; 同行同列而值不同 (科目重复时各期的键) 仍各记一处."""
    rows = sheet_rows("Code,Dr,Cr,Prior\n,1,,2\n1010,1,,2\n1010,3,,4\n")
    plan = TablePlan(
        kind=TableKind.trial_balance,
        fields={"account_code": FieldSource(column="Code")},
        periods=[
            PeriodSource(period="current", balance=FieldSource(debit="Dr", credit="Cr")),
            PeriodSource(period="prior", balance=FieldSource(column="Prior")),
        ],
    )
    result, failures = tp.apply_plan(plan, "x", rows)
    assert failures == [
        ParseFailure(row=2, column="Code", value=""),
        ParseFailure(row=4, column="Code", value="current/1010"),
        ParseFailure(row=4, column="Code", value="prior/1010"),
    ]
    assert len(result) == 2


def test_same_value_in_different_columns_is_recorded_per_column() -> None:
    """同一行两列都空 (同值不同列) 各记一处."""
    sheet = d.read_sheet("x.csv", b"code,name,type\n,,asset\n")
    result, failures = tp.convert(TableKind.accounts, sheet, {"code": "code", "name": "name", "type": "type"}, "dmy")
    assert result == []
    assert failures == [ParseFailure(row=2, column="code", value=""), ParseFailure(row=2, column="name", value="")]


def test_one_bad_cell_mapped_to_two_fields_is_one_failure() -> None:
    """提交时同一列对应两个字段 (只有一列日期的发票) 而该格坏时只记一处; 原 convert 记两处相同的."""
    sheet = d.read_sheet("x.csv", b"no,customer,date,amount\nI-1,Acme,notadate,10\n")
    mapping: dict[str, str | None] = {
        "invoice_no": "no",
        "customer": "customer",
        "invoice_date": "date",
        "posting_date": "date",
        "amount_cents": "amount",
    }
    result, failures = tp.convert(TableKind.sales_invoices, sheet, mapping, "dmy")
    assert result == [] and failures == [ParseFailure(row=2, column="date", value="notadate")]


def test_failures_are_sorted_by_row() -> None:
    """列来源的字段排在标题字段之前, 同一数据行两者都坏: 标题所在行的失败排在前面."""
    rows = sheet_rows("Date,Account,Debit\nID 1 notadate\nx,1010,abc\n")
    plan = TablePlan(
        kind=TableKind.journal_entries,
        group_title={"column": "Date", "pattern": r"^ID (?P<no>\d+) (?P<day>\S+)$"},
        fields={
            "debit_cents": FieldSource(column="Debit"),
            "entry_no": FieldSource(group="no"),
            "posting_date": FieldSource(group="day"),
            "account_code": FieldSource(column="Account"),
        },
        date_format="ymd",
    )
    _, failures = tp.apply_plan(plan, "x", rows)
    assert failures == [
        ParseFailure(row=2, column="Date", value="notadate"),
        ParseFailure(row=3, column="Debit", value="abc"),
    ]


def test_failures_from_a_group_title_point_at_the_title_row() -> None:
    """取自分组标题的值转换失败时, 失败处为标题所在行与标题列, 其下各数据行只记一次."""
    rows = sheet_rows(
        "Date,Account,Debit,Credit\n"
        "ID 1 2025-07-01\n"
        "x,1010,1,\n"
        "x,4100,,1\n"
        "ID 2 notadate\n"  # 第 5 行
        "x,1010,2,\n"
        "x,4100,,2\n"
    )
    plan = TablePlan(
        kind=TableKind.journal_entries,
        group_title={"column": "Date", "pattern": r"^ID (?P<no>\d+) (?P<day>\S+)$"},
        fields={
            "entry_no": FieldSource(group="no"),
            "posting_date": FieldSource(group="day"),
            "account_code": FieldSource(column="Account"),
            "debit_cents": FieldSource(column="Debit"),
            "credit_cents": FieldSource(column="Credit"),
        },
        date_format="ymd",
    )
    result, failures = tp.apply_plan(plan, "x", rows)
    assert [(r.id, r.posting_date) for r in result] == [("1-1", date(2025, 7, 1)), ("1-2", date(2025, 7, 1))]
    assert failures == [ParseFailure(row=5, column="Date", value="notadate")]


def test_periods_only_for_trial_balance() -> None:
    plan = plan_of("chart_of_accounts")
    period = PeriodSource(period="current", balance=FieldSource(column="*Code"))
    with pytest.raises(d.MappingError, match="只有试算表能按期分列, 不能用于 accounts"):
        apply(plan.model_copy(update={"periods": [period]}), "chart_of_accounts.csv")


# ── 各种变形 (小样例) ──


def sheet_rows(text: str) -> d.RawRows:
    return d.raw_rows("x.csv", text.encode("utf-8"))[1]


def test_signed_amount_splits_into_debit_and_credit() -> None:
    rows = sheet_rows("No,Date,Account,Amount,Kind\nJ1,2025-07-01,1010,12.50,Manual\nJ1,2025-07-01,4100,-12.50,auto\n")
    plan = TablePlan(
        kind=TableKind.journal_entries,
        fields={
            "entry_no": FieldSource(column="No"),
            "posting_date": FieldSource(column="Date"),
            "account_code": FieldSource(column="Account"),
            "debit_cents": FieldSource(column="Amount", part="positive"),
            "credit_cents": FieldSource(column="Amount", part="negative"),
            "is_adjustment": FieldSource(column="Kind", values={"Manual": "true", "Auto": "false"}),
        },
        date_format="ymd",
    )
    result, failures = tp.apply_plan(plan, "x", rows)
    assert failures == []
    assert [(r.id, r.debit_cents, r.credit_cents, r.is_adjustment) for r in result] == [
        ("J1-1", 1250, 0, True),
        ("J1-2", 0, 1250, False),
    ]


def test_net_of_blank_debit_and_credit_is_zero() -> None:
    rows = sheet_rows("Code,Dr,Cr\n1010,,\n2000,,5\n")
    plan = TablePlan(
        kind=TableKind.trial_balance,
        fields={"account_code": FieldSource(column="Code")},
        periods=[PeriodSource(period="current", balance=FieldSource(debit="Dr", credit="Cr"))],
    )
    result, _ = tp.apply_plan(plan, "x", rows)
    assert [(r.account_code, r.balance_cents) for r in result] == [("1010", 0), ("2000", -500)]


def test_amount_unit_overrides_header_and_date_format_applies() -> None:
    rows = sheet_rows("Invoice,Customer,Date,Amount,balance_cents\nI-1,Acme,07/13/2025,1250,12.5\n")
    fields = {
        "invoice_no": FieldSource(column="Invoice"),
        "customer": FieldSource(column="Customer"),
        "invoice_date": FieldSource(column="Date"),
        "posting_date": FieldSource(column="Date"),
    }
    cents = TablePlan(
        kind=TableKind.sales_invoices,
        fields={**fields, "amount_cents": FieldSource(column="Amount")},
        amount_unit="cents",
        date_format="mdy",
    )
    [invoice], _ = tp.apply_plan(cents, "x", rows)
    assert (invoice.amount_cents, invoice.invoice_date) == (1250, date(2025, 7, 13))
    dollars = cents.model_copy(update={"fields": {**fields, "amount_cents": FieldSource(column="balance_cents")}})
    [invoice], _ = tp.apply_plan(dollars.model_copy(update={"amount_unit": "dollars"}), "x", rows)
    assert invoice.amount_cents == 1250  # 表头以 cents 结尾也按元读
    _, failures = tp.apply_plan(dollars.model_copy(update={"amount_unit": None}), "x", rows)
    assert failures == [ParseFailure(row=2, column="balance_cents", value="12.5")]  # 不写单位时按表头读成分, 须为整数


def test_group_titles_are_single_cell_rows() -> None:
    """只有标题列有值的行才是分组标题; 同样的文本带了别的值是数据行 (此处日期解析失败). 第一个标题之前的数据行取不到
    标题值, 失败处记在该数据行."""
    rows = sheet_rows(
        "Date,Account,Debit,Credit\n"
        "2025-07-01,1010,1,\n"  # 第 2 行: 还没有标题
        "ID 1 Rent\n"
        "2025-07-02,6100,5,\n"
        "2025-07-02,1010,,5\n"
        "ID 2 Sales,4100,,3\n"  # 第 6 行: 不是标题
        "Total,,,\n"
    )
    plan = TablePlan(
        kind=TableKind.journal_entries,
        skip=[{"column": "Date", "pattern": "^Total$"}],
        group_title={"column": "Date", "pattern": r"^ID (?P<no>\d+) (?P<text>.*)$"},
        fields={
            "entry_no": FieldSource(group="no"),
            "description": FieldSource(group="text"),
            "posting_date": FieldSource(column="Date"),
            "account_code": FieldSource(column="Account"),
            "debit_cents": FieldSource(column="Debit"),
            "credit_cents": FieldSource(column="Credit"),
        },
        date_format="ymd",
    )
    result, failures = tp.apply_plan(plan, "x", rows)
    assert [(r.id, r.description, r.debit_cents - r.credit_cents) for r in result] == [
        ("1-1", "Rent", 500),
        ("1-2", "Rent", -500),
    ]
    assert failures == [
        ParseFailure(row=2, column="Date", value=""),  # entry_no 为空 (标题列, 没有标题值)
        ParseFailure(row=6, column="Date", value="ID 2 Sales"),
    ]


def test_sheet_from_rows_takes_the_given_header() -> None:
    rows = sheet_rows("Report\nCompany\n\ncode,name,type\n1010,Cash,asset\n\n4100,Sales,revenue\n")
    sheet = d.sheet_from_rows("x", rows, 4)
    assert (sheet.header_row, sheet.columns, [n for n, _ in sheet.rows]) == (4, ["code", "name", "type"], [5, 7])
    assert d.read_sheet("x.csv", b"\n\ncode,name\n1,A\n").header_row == 3  # 缺省取第一个非空行


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "accounts", "fields": {"code": {"column": "a", "group": "b"}}},
        {"kind": "accounts", "fields": {"code": {"debit": "a"}}},
        {"kind": "accounts", "fields": {"code": {"debit": "a", "credit": "b", "part": "positive"}}},
        {"kind": "accounts", "fields": {"code": {"debit": "a", "credit": "b", "values": {"x": "y"}}}},
        {"kind": "accounts", "skip": [{"column": "a", "pattern": "("}]},
        {"kind": "accounts", "group_title": {"column": "a", "pattern": "^(.*)$"}},
        {"kind": "accounts", "fields": {"code": {"group": "missing"}}},
        {"kind": None, "fields": {"code": {"column": "a"}}},
        {"kind": "trial_balance", "periods": [{"period": "current", "balance": {"column": "a"}}] * 2},
        {"kind": "trial_balance", "periods": [{"period": "current", "balance": {"group": "g"}}]},
        {"kind": "accounts", "header_row": 0},
        {"kind": "accounts", "colour": "red"},
        {"kind": "accounts", "fields": {"code": {"column": "a", "part": "positive", "values": {"x": "1"}}}},
    ],
)
def test_malformed_plans_are_rejected(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TablePlan.model_validate(value)


def test_plan_samples_parse() -> None:
    """方案样例 (DOC-SHEET 的固定输出) 都合识别结果的模型: 5 份 Xero 导出 (含期间不符一例用的 11-30 试算表) 与 Numbat
    的 7 张表各一份."""
    assert sorted(p.stem for p in PLANS.glob("*.json")) == [
        "accounts",
        "bank_accounts",
        "bank_transactions",
        "chart_of_accounts",
        "delivery_notes",
        "journal_entries",
        "journal_report",
        "receivable_invoice_detail",
        "sales_invoices",
        "trial_balance",
        "trial_balance_2026-06-30",
        "trial_balance_2026-11-30",
    ]
    assert all(isinstance(plan_of(p.stem), TablePlan) for p in PLANS.glob("*.json"))
