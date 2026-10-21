"""需求 R177 (slow): GEN-07 期后监控 S01-S03 的 SQL 连库验收. 测试库中的 Xero q1 项目 (按补造清单写入, 数据集声明随之
载入) 与 Numbat 项目 (经数据集载入) 取会话共用的 (conftest 的 xero_db, numbat_db, 与 GEN-06 的 SQL 用例共用); 用真实的
PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用 build_run 解析 (xero_db.first_run). 缺省参数下
Xero 的结果等于离线预置 (XERO_RULES 的 GEN-07); 另以反例与参数覆盖断言到刚好能区分口径 (同额行, 作废对, 常规过账, 净额
为 0, 窗口截止日). 没有 TEST_DATABASE_URL 时跳过."""

from datetime import date

import pytest

from backend.contracts.models import ProcedureRun
from backend.contracts.services import RuleResult
from tests.xero_db import DbProject, XeroProject, first_run, rules_with
from tests.xero_fakes import XERO_RULES

pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="session")]  # 与会话共用的项目同一个事件循环


@pytest.fixture(scope="module")
def xero_run(xero_db: XeroProject) -> ProcedureRun:
    return first_run(xero_db, "GEN-07")


@pytest.fixture(scope="module")
def numbat_run(numbat_db: DbProject) -> ProcedureRun:
    return first_run(numbat_db, "GEN-07")


def hits(result: RuleResult, rule_id: str) -> list[str]:
    """某条规则的例外所指的行 (source_ids 首项), 按例外编号."""
    return [x.source_ids[0] for x in result.exceptions if x.rule_id == rule_id]


async def journal_lines(project: DbProject, entry_nos: list[str]) -> dict[str, tuple[str, str, int]]:
    """测试库中这些凭证的分录行: 行 ID → (入账日, 科目, 借减贷)."""
    rows = await project.deps.financial_store.query(
        project.eid,
        "SELECT id, posting_date, account_code, debit_cents-credit_cents AS net_cents FROM journal_entries "
        "WHERE engagement_id=:eid AND entry_no=ANY(CAST(:entry_nos AS text[])) ORDER BY id",
        {"entry_nos": entry_nos},
    )
    return {r["id"]: (r["posting_date"].isoformat(), r["account_code"], r["net_cents"]) for r in rows}


@pytest.mark.external
async def test_xero_gen07_on_postgres(xero_db: XeroProject, xero_run: ProcedureRun) -> None:
    """验收 2, 5: Xero q1 上恰好 9 条例外, 与离线预置 (xero_fakes.xero_gen07_exceptions, 各条的编号, 行与金额由离线用例
    逐条核对) 逐字段相同 (ID, 金额, detail, 证据指针). 窗口为 10-01 至 10-17 (期末加 31 天截到数据截止日), 各行带
    window_end."""
    assert (xero_run.id, xero_run.params_snapshot["data_as_of"]) == ("RUN-GEN-07-1", date(2026, 10, 17))
    result = await rules_with(xero_db, "GEN-07", xero_run)
    assert (result.rows, result.outcomes) == ({}, [])
    assert result.exceptions == XERO_RULES["GEN-07"](xero_run).exceptions
    assert {x.detail["window_end"] for x in result.exceptions} == {"2026-10-17"}


@pytest.mark.external
async def test_xero_gen07_counter_examples(xero_db: XeroProject, xero_run: ProcedureRun) -> None:
    """验收 6: 能区分对错的反例.
    - S01: BT-090-0085 (214500) 与 BT-090-0124 (385000) 在 ±10 天内有同额的总账银行科目行 XJ-690-2, XJ-695-2,
      BT-091-0004 (-450000) 有 XJ-732-2, 都不命中; BT-090-0097 (-450000) 在 ±10 天内只有作废对 XJ-728/XJ-744 的
      XJ-744-2 同额, 剔除作废对后仍命中; 不剔除作废对 (没有冲回格式) 时它会漏掉.
    - S02: XJ-756 (存货期初, 各科目净额为 0), 常规赊销 (XJ-612: 借应收, 贷收入) 与赊购 (XJ-613: 贷应付) 都在窗口内,
      都不进 S02; 去掉 "各科目净额都为 0" 的剔除时 XJ-756 会进来.
    - data_as_of 改为 10-14 时 S01 只剩前 4 条 (10-15 的 BT-090-0130, 0131 落在窗口外), S02 为 0 条 (XJ-720 在 10-16),
      S03 仍为 2 条."""
    result = await rules_with(xero_db, "GEN-07", xero_run)
    s01 = hits(result, "S01")
    assert {"BT-090-0085", "BT-090-0124", "BT-091-0004"}.isdisjoint(s01) and "BT-090-0097" in s01
    lines = await journal_lines(xero_db, ["XJ-690", "XJ-695", "XJ-732", "XJ-728", "XJ-744"])
    assert {k: lines[k] for k in ("XJ-690-2", "XJ-695-2", "XJ-732-2", "XJ-744-2", "XJ-728-2")} == {
        "XJ-690-2": ("2026-10-14", "090", 214500),
        "XJ-695-2": ("2026-10-14", "090", 385000),
        "XJ-732-2": ("2026-10-17", "091", -450000),
        "XJ-744-2": ("2026-10-17", "090", -450000),  # 作废对: XJ-744 是 XJ-728 的冲回
        "XJ-728-2": ("2026-10-17", "091", -450000),
    }
    unvoided = await rules_with(
        xero_db, "GEN-07", xero_run, reversal_reference_pattern=None, reversal_entry_no_template=None
    )
    assert hits(unvoided, "S01") == [x for x in s01 if x != "BT-090-0097"]

    assert hits(result, "S02") == ["XJ-720-2"]
    routine = await journal_lines(xero_db, ["XJ-756", "XJ-612", "XJ-613"])
    assert {line[0] for line in routine.values()} <= {"2026-10-01", "2026-10-17"} and len(routine) == 12
    opening = [(acct, net) for line_id, (_, acct, net) in routine.items() if line_id.startswith("XJ-756-")]
    assert {acct for acct, _ in opening} == {"630"} and sum(net for _, net in opening) == 0  # 只有存货一个科目, 净额 0
    [s02] = [r for r in xero_db.deps.library.load("GEN-07").rules if r.id == "S02"]
    netting = "GROUP BY entry_no HAVING bool_or(net_cents<>0)"
    assert s02.sql.count(netting) == 1
    rows = await xero_db.deps.financial_store.query(
        xero_db.eid, s02.sql.replace(netting, "GROUP BY entry_no"), xero_run.params_snapshot
    )
    assert [r["source_id"] for r in rows] == ["XJ-720-2", "XJ-756-4"]  # 不剔除净额为 0 的凭证时 XJ-756 会进来
    assert {(acct, net) for line_id, (_, acct, net) in routine.items() if line_id.startswith("XJ-612-")} >= {
        ("610", 385000),
        ("200", -350000),
    }
    assert ("800", -118125) in {
        (acct, net) for line_id, (_, acct, net) in routine.items() if line_id.startswith("XJ-613-")
    }

    early = await rules_with(xero_db, "GEN-07", xero_run, data_as_of=date(2026, 10, 14))
    assert hits(early, "S01") == ["BT-090-0092", "BT-090-0097", "BT-090-0099", "BT-090-0107"]
    assert (hits(early, "S02"), hits(early, "S03")) == ([], ["XJ-626-2", "XJ-676-2"])
    assert {x.detail["window_end"] for x in early.exceptions} == {"2026-10-14"}


async def test_numbat_gen07_sql_runs(numbat_db: DbProject, numbat_run: ProcedureRun) -> None:
    """验收 7: Numbat (没有数据集声明: data_as_of 不设上限, 没有冲回格式) 上 GEN-07 的 SQL 能执行不报错 (不断言条数);
    窗口为期末后 31 天."""
    params = numbat_run.params_snapshot
    assert (params["data_as_of"], params["reversal_reference_pattern"], params["reversal_entry_no_template"]) == (
        None,
        None,
        None,
    )
    result = await rules_with(numbat_db, "GEN-07", numbat_run)
    assert result.outcomes == [] and {x.detail["window_end"] for x in result.exceptions} <= {"2026-07-31"}
