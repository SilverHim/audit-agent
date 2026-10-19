"""需求 R96 工资册: 账表种类 employees 的导入字段与单元格解析, 假存储的写入与读回. 不连数据库.

真实库的写入与行级安全见 test_persistence_postgres.py, 迁移 0013 往返见 test_data_store_postgres.py; 概览, 导入字段
接口与迁移单头按 TableKind 或迁移链整体断言 (test_data_summary, test_data_imports_api, test_import_batches), 不在这里
重复."""

from datetime import date

import pytest

from backend.contracts.data import TableField, TableKind
from backend.contracts.records import Employee
from backend.services import data_import as d
from backend.services import table_plan as tp
from backend.services.data_quality import REQUIRED_TABLES, TABLE_TITLES
from backend.services.data_service import TABLE_NAMES
from tests.external import require_xero_supplements
from tests.fakes import ENGAGEMENT, FakeDataStore, FakeFinancialStore

EID = ENGAGEMENT.id
EXPECTED = [
    Employee(
        employee_id=employee_id,
        name=name,
        start_date=date(2026, 7, 20),
        employment_type="full_time",
        pay_period_days=14,
        gross_per_period_cents=gross,
    )
    for employee_id, name, gross in [
        ("E001", "Oliver Gray", 240000),
        ("E002", "Tracy Green", 180000),
        ("E003", "Odette Garrison", 250000),
    ]
]  # Xero 演示数据集补造目录工资册的 3 名员工 (按员工编号)
IDENTITY = {f.name: f.name for f in d.SCHEMAS[TableKind.employees].fields}


def test_every_table_kind_has_trail_and_request_titles() -> None:
    """防新加的账表种类漏了名称: 写入的留痕摘要 (TABLE_NAMES) 与补交请求的标题 (TABLE_TITLES) 按种类取, 缺了写入或
    提请求时 KeyError. 工资册不是就绪必需表."""
    assert set(TABLE_NAMES) == set(TABLE_TITLES) == set(TableKind)
    assert TableKind.employees not in REQUIRED_TABLES


def test_import_fields() -> None:
    assert d.SCHEMAS[TableKind.employees].fields == [
        TableField(name="employee_id", type="text", required=True),
        TableField(name="name", type="text", required=True),
        TableField(name="start_date", type="date", required=True),
        TableField(name="employment_type", type="text", required=False),
        TableField(name="pay_period_days", type="integer", required=True),
        TableField(name="gross_per_period_cents", type="amount", required=True),
    ]
    assert d.KEYS[TableKind.employees] == ("employee_id",)


@pytest.mark.parametrize(
    ("file_name", "data", "mapping", "expected"),
    [
        pytest.param("employees.csv", None, IDENTITY, EXPECTED, marks=pytest.mark.external),  # 补造目录的工资册
        (
            "Payroll register.csv",
            b"Employee No,Name,Start Date,Employment Type,Pay Period Days,Gross Pay\n"
            b'E002,Tracy Green,20/07/2026,full_time,14,"1,800.00"\n'
            b"E001,Oliver Gray,20/07/2026,,14,2400\n",
            {
                "employee_id": "Employee No",
                "name": "Name",
                "start_date": "Start Date",
                "employment_type": "Employment Type",
                "pay_period_days": "Pay Period Days",
                "gross_per_period_cents": "Gross Pay",
            },
            [EXPECTED[1], EXPECTED[0].model_copy(update={"employment_type": None})],
        ),  # 常见列名; 金额列名不带 cents 按元换算; 日月年; 雇佣类型可空
    ],
)
def test_payroll_sheet_is_suggested_and_converts(
    file_name: str, data: bytes | None, mapping: dict[str, str], expected: list[Employee]
) -> None:
    """data 为空时取 Xero 演示补造目录的工资册 (列名即字段名)."""
    if data is None:
        data = (require_xero_supplements("20261017-q1") / "payroll" / file_name).read_bytes()
    sheet = d.read_sheet(file_name, data)
    preview = d.preview("IMP-1", file_name, d.sheet_names(file_name, data), sheet)
    assert (preview.suggested_kind, preview.suggested_mapping) == (TableKind.employees, mapping)

    rows, failures = tp.convert(TableKind.employees, sheet, preview.suggested_mapping, preview.suggested_date_format)

    assert (rows, failures) == (expected, [])


def test_parse_integer() -> None:
    """整数字段 (发薪周期天数): 整数文本 (可带千分位与正号), 整数与小数部分为 0 的数值单元格; 空为 None."""
    assert [d.parse_integer(v) for v in ("14", " 7 ", "1,000", "+3", 14, 28.0, "", None)] == [
        14,
        7,
        1000,
        3,
        14,
        28,
        None,
        None,
    ]
    for bad in ("14.5", 14.5, "1e3", True):  # 带小数的, 科学计数与是否值不当整数
        with pytest.raises(d.CellError):
            d.parse_integer(bad)


def test_bad_cells_and_duplicate_ids_are_failures() -> None:
    """不是整数的天数, 不在 1 至 366 的天数 (模型约束; 超出库里 int4 的值不会写到库才报错) 与重复的员工编号各报一行;
    其余行照常转换. 1_4 只有经整数解析才算失败 (按文本交给行模型时会被当成 14)."""
    text = (
        "employee_id,name,start_date,employment_type,pay_period_days,gross_per_period_cents\n"
        "E001,Oliver Gray,2026-07-20,full_time,fortnight,240000\n"
        "E002,Tracy Green,2026-07-20,full_time,0,180000\n"
        "E003,Odette Garrison,2026-07-20,full_time,14,250000\n"
        "E003,Odette Garrison,2026-07-20,full_time,14,250000\n"
        "E004,Max Long,2026-07-20,full_time,3000000000,1\n"
        "E005,Una Score,2026-07-20,full_time,1_4,1\n"
    )
    sheet = d.read_sheet("employees.csv", text.encode("utf-8"))

    rows, failures = tp.convert(TableKind.employees, sheet, IDENTITY, "ymd")

    assert rows == [EXPECTED[2]]
    assert [f.row for f in failures] == [2, 3, 5, 6, 7]
    assert (failures[0].column, failures[0].value) == ("pay_period_days", "fortnight")


async def test_fake_stores_write_and_read_back_by_employee_id() -> None:
    """假存储同真实实现 (Agent 层测试与演示数据导入用): 没写过为空; FakeDataStore.write_tables 写工资册后
    FakeFinancialStore.employees 按员工编号读回, 再写整表替换."""
    financial = FakeFinancialStore()
    data = FakeDataStore()
    data.financial = financial
    assert await financial.employees(EID) == []

    await data.write_tables(EID, {TableKind.employees: EXPECTED[::-1]}, source="import", actor="U-1")
    assert await financial.employees(EID) == EXPECTED
    await data.write_tables(EID, {TableKind.employees: EXPECTED[:1]}, source="import", actor="U-1")
    assert await financial.employees(EID) == EXPECTED[:1]
