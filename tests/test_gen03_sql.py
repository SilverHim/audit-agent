"""需求 R139 (slow): GEN-03 规则 C01, C02 的 SQL 连库验收. Xero q1 按补造清单写进测试库 (R112 的 build_xero_project,
module 级 fixture, 各用例共用), 用真实的 PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用
build_run 解析; 名称与 ACN 由用例给出 (同 A6 由关系图推出后合并进参数). 缺省关系图的两家等于离线预置 (XERO_RULES 的
GEN-03), 再以别的名称, 参数覆盖与临时写入的合成分录区分口径. 没有 TEST_DATABASE_URL 时跳过."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert

from backend.agents.base import AgentDeps
from backend.contracts.models import ProcedureRun
from backend.contracts.services import RuleResult
from backend.orchestration.nodes import build_run
from backend.persistence import models as m
from backend.persistence.db import bind_engagement
from tests.external import snapshot_dir, supplements_dir
from tests.xero_db import build_xero_project, database_url, procedure_of
from tests.xero_fakes import SNAPSHOT_NAME, XERO_RULES, xero_gen03_rows, xero_tables

pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="module")]  # 用例与 module 级项目同一个事件循环
VPA = ("Victoria Property Agency Pty Ltd", "900043030")  # 关系图节点的名称 (按登记摘录) 与 ACN
ADVISORY = ("Morgan Advisory Pty Ltd", "900043049")
SMART = ("SMART Agency Pty Ltd", "900043076")  # T6 同构用例的合成节点


@dataclass
class Gen03Project:
    """测试库中的 Xero 项目与 GEN-03 第 1 次执行的记录 (参数快照)."""

    deps: AgentDeps
    eid: str
    run: ProcedureRun


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def xero(tmp_path_factory: pytest.TempPathFactory) -> Gen03Project:
    project = await build_xero_project(
        database_url(), snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp_path_factory.mktemp("xero")
    )
    deps = project.deps
    template = deps.library.load("GEN-03")
    run = build_run(
        deps.library,
        template,
        procedure_of(deps, "GEN-03"),
        project.engagement,
        project.state,
        datetime.now(UTC),
        traits=project.traits,
        profile=project.profile,
    )
    return Gen03Project(deps, project.engagement.id, run)


async def gen03(project: Gen03Project, *parties: tuple[str, str | None], **overrides: object) -> RuleResult:
    """以参数快照加这些对象的名称与 ACN (再叠加 overrides) 跑 GEN-03 的规则."""
    names = {"party_names": [p[0] for p in parties], "party_acns": [p[1] for p in parties]}
    params = project.run.params_snapshot | names | overrides
    return await project.deps.rules.run(project.eid, project.deps.library.load("GEN-03"), project.run, params)


def entry_nos(result: RuleResult, key: str) -> set[str]:
    return {r["record"]["entry_no"] for r in result.rows["C01"] if r["party_key"] == key and "entry_no" in r["record"]}


# 合成行缺省的列: 分录行没有录入与审批, 不是调整分录; 银行流水的交易后余额记 0 (C01 不取它)
BLANK = {
    "journal_entries": {
        "created_at": None,
        "created_by": None,
        "approved_by": None,
        "approved_at": None,
        "is_adjustment": False,
    },
    "bank_transactions": {"balance_after_cents": 0, "reference": None},
}


@asynccontextmanager
async def with_lines(
    project: Gen03Project, lines: list[dict[str, Any]], table_name: str = "journal_entries"
) -> AsyncIterator[None]:
    """在测试库的项目中暂时写入一张账表 (分录或银行流水) 的合成行 (测试库是副本, 原文件不改), 退出时删去, 共用项目的
    其余用例不受影响."""
    engine = project.deps.data.engine  # type: ignore[attr-defined]
    table = m.FINANCIAL[table_name]
    blank = BLANK[table_name]
    async with engine.begin() as conn:
        await bind_engagement(conn, project.eid)
        await conn.execute(insert(table), [blank | line | {"engagement_id": project.eid} for line in lines])
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await bind_engagement(conn, project.eid)
            ids = [line["id"] for line in lines]
            await conn.execute(delete(table).where((table.c.engagement_id == project.eid) & table.c.id.in_(ids)))


@pytest.mark.external
async def test_xero_default_parties_equal_offline_presets(xero: Gen03Project) -> None:
    """验收 2, 5: 缺省关系图的 VPA 与 Morgan Advisory 上 C01, C02 等于离线预置 (VPA 的三笔房租凭证各 3 行与三笔付款流水,
    Morgan Advisory 没有往来, 窗口内没有权益科目的分录). 不剔除作废对 (格式为空) 时 VPA 多出原凭证 XJ-415 与其冲回
    XJ-417, 证明作废对是 SQL 剔除的."""
    result = await gen03(xero, VPA, ADVISORY)
    assert result == XERO_RULES["GEN-03"](xero.run)
    unvoided = await gen03(xero, VPA, reversal_reference_pattern=None)
    assert entry_nos(unvoided, "victoria property agency") - entry_nos(result, "victoria property agency") == {
        "XJ-415",
        "XJ-417",
    }


@pytest.mark.external
async def test_xero_takes_whole_vouchers_for_smart(xero: Gen03Project) -> None:
    """验收 6: 对 "Victoria Property Agency Pty Ltd" 与 "SMART Agency Pty Ltd", SMART 取到 XJ-548 与期后 XJ-641 的全部行
    (费用行 XJ-548-2 的说明是物品描述, 不含对方名称) 与期后的 BT-090-0097, VPA 照旧; 等于离线预置 (T6 同构用例用它)."""
    result = await gen03(xero, VPA, SMART)
    assert result.rows["C01"] == xero_gen03_rows(["smart agency", "victoria property agency"])
    [design] = [r["record"] for r in result.rows["C01"] if r["record"]["id"] == "XJ-548-2"]
    assert design["debit_cents"] == 181818 and "smart" not in design["description"].lower()


@pytest.mark.external
async def test_whole_words_after_normalisation(xero: Gen03Project) -> None:
    """验收 2 的 "整词含" (审核 R139-02): 临时写入合成的分录与银行流水, 名称规范化后整词相同才命中: 标点, 连字符, 多个
    空格与大小写不影响 (分录说明, 流水摘要), source_doc 带开头的前缀 "Payment to:" 去掉后命中; 名称后面接着字母的
    ("Agencyx") 不命中, 防改成子串含; ORG 节点 (ACN 为空) 照常按名称命中自己那一行."""

    def entry(no: str, description: str, source_doc: str = "SYNTHETIC") -> dict[str, Any]:
        return {
            "id": f"{no}-1",
            "entry_no": no,
            "posting_date": date(2026, 8, 3),
            "account_code": "469",
            "debit_cents": 100,
            "credit_cents": 0,
            "description": description,
            "source_doc": source_doc,
        }

    def bank(no: str, description: str) -> dict[str, Any]:
        return {
            "id": no,
            "bank_account_id": "BA-090",
            "txn_date": date(2026, 8, 3),
            "amount_cents": -100,
            "description": description,
        }

    entries = [
        entry("SYN-B1", "VICTORIA-PROPERTY  AGENCY. rent Aug"),  # 标点, 多空格, 大写: 命中
        entry("SYN-B2", "Victoria Property Agencyx Ltd"),  # 名称后接字母: 不命中
        entry("SYN-B3", "misc", "Payment to: Victoria Property Agency"),  # source_doc 去前缀后: 命中
        entry("SYN-B4", "Kiwi Holdings Ltd management fee"),  # ORG 节点 (ACN 为空): 命中
    ]
    lines = [bank("SYN-T1", "Victoria-Property Agency."), bank("SYN-T2", "VICTORIA PROPERTY AGENCYX")]
    async with with_lines(xero, entries), with_lines(xero, lines, "bank_transactions"):
        result = await gen03(xero, VPA, ("Kiwi Holdings Ltd", None))
    hits = {(r["party_key"], r["record"]["id"]) for r in result.rows["C01"] if r["record"]["id"].startswith("SYN-")}
    assert hits == {
        ("victoria property agency", "SYN-B1-1"),
        ("victoria property agency", "SYN-B3-1"),
        ("kiwi holdings", "SYN-B4-1"),
        ("victoria property agency", "SYN-T1"),
    }


@pytest.mark.external
async def test_xero_invoices_by_customer_name_or_abn(xero: Gen03Project) -> None:
    """验收 2 (3): 发票按客户名去后缀后相等 ("Hamilton Smith Pty Ltd" 对客户 Hamilton Smith Pty), 或 customer_abn 末
    9 位等于该方 ACN (名称无关的 "Unrelated Holdings Pty Ltd" 配上 Bayside Club 的 ABN 11000111000 的后 9 位), 开票日
    在本期与期后窗口内; 每行带该方的 party_key."""
    result = await gen03(xero, ("Hamilton Smith Pty Ltd", None), ("Unrelated Holdings Pty Ltd", "000111000"))
    found = {(r["party_key"], r["record"]["id"]) for r in result.rows["C01"] if r["source_type"] == "sales_invoice"}
    params = xero.run.params_snapshot
    start = date.fromisoformat(str(params["period_start"]))
    end = date.fromisoformat(str(params["period_end"])) + timedelta(days=int(params["subsequent_days"]))
    invoices = [i for i in xero_tables()["sales_invoices"] if start <= i.invoice_date <= end]
    expected = {("hamilton smith", i.id) for i in invoices if i.customer == "Hamilton Smith Pty"}
    expected |= {("unrelated holdings", i.id) for i in invoices if i.customer_abn == "11000111000"}
    assert found == expected
    assert len([k for k, _ in expected if k == "hamilton smith"]) == 4 and len(expected) == 6


@pytest.mark.external
async def test_equity_lines_in_window_without_voided_pairs(xero: Gen03Project) -> None:
    """验收 2 C02: 临时写入合成分录 (权益科目 960, 970): 窗口两端 (期初 07-01, 期后第 30 天 10-30) 的行取到, 窗口外
    (06-30, 10-31), 非权益科目 (090) 与作废对 (XJ-99001 及其冲回 XJ-99002) 的行不取; 按日期排序."""

    def line(no: str, day: date, account: str, description: str = "Synthetic equity entry") -> dict[str, Any]:
        return {
            "id": f"{no}-1",
            "entry_no": no,
            "posting_date": day,
            "account_code": account,
            "debit_cents": 0,
            "credit_cents": 100000,
            "description": description,
            "source_doc": "SYNTHETIC",
        }

    lines = [
        line("SYN-1", date(2026, 6, 30), "970"),
        line("SYN-2", date(2026, 10, 30), "960"),
        line("SYN-3", date(2026, 7, 1), "970"),
        line("SYN-4", date(2026, 10, 31), "960"),
        line("SYN-5", date(2026, 8, 3), "090"),
        line("XJ-99001", date(2026, 8, 3), "970"),
        line("XJ-99002", date(2026, 8, 3), "970", "Reversal of ID 99001"),
    ]
    async with with_lines(xero, lines):
        result = await gen03(xero, VPA)
    assert [(r["id"], r["account_category"]) for r in result.rows["C02"]] == [
        ("SYN-3-1", "share_capital"),
        ("SYN-2-1", "retained_earnings"),
    ]
    assert result.rows["C02"][0] == {  # 列同 C01 分录的 record
        "source_type": "journal_entry",
        **lines[2],
        "account_name": "Owner A Share Capital",
        "account_category": "share_capital",
    }
