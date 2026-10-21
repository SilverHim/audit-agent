"""需求 R179 (slow): GEN-06 E01, E02 的 SQL 连库验收. 测试库中的 Xero q1 项目 (按补造清单写入, 数据集声明随之载入)
与 Numbat 项目 (经数据集载入) 取会话共用的 (conftest 的 xero_db, numbat_db, 与 GEN-07 的 SQL 用例共用); 用真实的
PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用 build_run 解析 (xero_db.first_run). 缺省参数下
Xero 的行等于离线派生 (XERO_RULES 的 GEN-06, 即 xero_gen06_rows), 再以参数覆盖断言到刚好能区分口径 (区间两端, 作废对,
数据截止日). 没有 TEST_DATABASE_URL 时跳过."""

from datetime import date

import pytest

from backend.contracts.models import ProcedureRun
from backend.contracts.services import RuleResult
from tests.xero_db import DbProject, XeroProject, first_run, rules_with
from tests.xero_fakes import XERO_RULES

pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="session")]  # 与会话共用的项目同一个事件循环


@pytest.fixture(scope="module")
def xero_run(xero_db: XeroProject) -> ProcedureRun:
    return first_run(xero_db, "GEN-06")


@pytest.fixture(scope="module")
def numbat_run(numbat_db: DbProject) -> ProcedureRun:
    return first_run(numbat_db, "GEN-06")


def entries(result: RuleResult) -> list[str]:
    """E01 的凭证号, 去重后按出现顺序."""
    return list(dict.fromkeys(str(r["entry_no"]) for r in result.rows["E01"]))


@pytest.mark.external
async def test_xero_gen06_rows_on_postgres(xero_db: XeroProject, xero_run: ProcedureRun) -> None:
    """验收 1, 5: Xero q1 上 E01, E02 的行与离线派生 (xero_gen06_rows) 逐行相同 (列, 值, 类型与顺序), 不出例外; 区间为
    07-01 至 10-17."""
    params = xero_run.params_snapshot
    assert (xero_run.id, params["period_start"], params["data_as_of"]) == (
        "RUN-GEN-06-1",
        date(2026, 7, 1),
        date(2026, 10, 17),
    )
    result = await rules_with(xero_db, "GEN-06", xero_run)
    assert (result.exceptions, result.outcomes) == ([], [])
    assert result.rows == XERO_RULES["GEN-06"](xero_run).rows
    days = [r["posting_date"] for rule in ("E01", "E02") for r in result.rows[rule]]
    assert min(days) >= date(2026, 7, 1) and max(days) == date(2026, 10, 17)  # 快照本期第一笔分录在 07-16


@pytest.mark.external
async def test_xero_gen06_counter_examples(xero_db: XeroProject, xero_run: ProcedureRun) -> None:
    """验收 1, 6: 能区分口径的反例.
    - 作废对: XJ-548 在 E01, 同日的作废对 XJ-547/549 与期后的 XJ-728/744 不在; 不剔除作废对 (没有冲回格式) 时都会进来.
    - 区间: 上期的发票 (ORC0999, 2025-08-16) 不在 E02; 期间起日改到 2025-07-01 时进来.
    - 数据截止日: data_as_of 改为 10-14 时 10-16 的 XJ-720 与 10-17 的发票都在区间外; 为空时取期末加 31 天, 区间不变
      (快照数据只到 10-17)."""
    result = await rules_with(xero_db, "GEN-06", xero_run)
    found = entries(result)
    assert "XJ-548" in found and {"XJ-547", "XJ-549", "XJ-728", "XJ-744"}.isdisjoint(found)
    unvoided = entries(
        await rules_with(xero_db, "GEN-06", xero_run, reversal_reference_pattern=None, reversal_entry_no_template=None)
    )
    assert {"XJ-547", "XJ-549", "XJ-728", "XJ-744"} <= set(unvoided)

    numbers = [r["invoice_no"] for r in result.rows["E02"]]
    assert "ORC0999" not in numbers
    earlier = await rules_with(xero_db, "GEN-06", xero_run, period_start=date(2025, 7, 1))
    assert "ORC0999" in [r["invoice_no"] for r in earlier.rows["E02"]]

    early = await rules_with(xero_db, "GEN-06", xero_run, data_as_of=date(2026, 10, 14))
    assert "XJ-720" in found and "XJ-720" not in entries(early)
    assert max(r["posting_date"] for r in early.rows["E02"]) <= date(2026, 10, 14)
    assert (await rules_with(xero_db, "GEN-06", xero_run, data_as_of=None)).rows == result.rows


async def test_numbat_gen06_sql_runs(numbat_db: DbProject, numbat_run: ProcedureRun) -> None:
    """Numbat (没有数据集声明: data_as_of 不设上限, 没有冲回格式) 上 GEN-06 的 SQL 能执行不报错; 区间为本期起至期末
    后 31 天."""
    params = numbat_run.params_snapshot
    assert (params["data_as_of"], params["reversal_reference_pattern"]) == (None, None)
    result = await rules_with(numbat_db, "GEN-06", numbat_run)
    assert result.outcomes == [] and result.rows["E01"]
    assert {r["posting_date"] >= date(2025, 7, 1) for r in [*result.rows["E01"], *result.rows["E02"]]} == {True}
    assert max(r["posting_date"] for r in result.rows["E01"]) <= date(2026, 7, 31)
