"""需求 R177 (slow): GEN-07 期后监控 S01-S03 的 SQL 连库验收. Xero q1 按补造清单写进测试库 (R112 的 build_xero_project,
数据集声明随之载入), Numbat 经数据集载入, 两个项目各建一次 (module 级 fixture), 各用例共用; 用真实的
PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用 build_run 解析. 缺省参数下 Xero 的结果等于离线
预置 (XERO_RULES 的 GEN-07); 另以反例与参数覆盖断言到刚好能区分口径 (同额行, 作废对, 常规过账, 净额为 0, 窗口截止日).
没有 TEST_DATABASE_URL 时跳过."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from backend.agents.base import AgentDeps
from backend.contracts.models import ClientProfile, Engagement, ProcedureRun
from backend.contracts.records import DatasetTraits
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import build_run
from tests.external import snapshot_dir, supplements_dir
from tests.test_xero_rules_db import build_xero_project, database_url, new_owner, procedure_of
from tests.xero_fakes import SNAPSHOT_NAME, XERO_RULES

pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="module")]  # 用例与 module 级项目同一个事件循环
NUMBAT_MATERIALITY = {
    "materiality_cents": 12000000,
    "performance_materiality_cents": 9000000,
    "clearly_trivial_cents": 600000,
}


@dataclass
class Gen07Project:
    """测试库中的一个项目与 GEN-07 第 1 次执行的记录 (参数快照)."""

    deps: AgentDeps
    eid: str
    run: ProcedureRun


def gen07_run(
    deps: AgentDeps, engagement: Engagement, state: EngagementState, traits: DatasetTraits, profile: ClientProfile
) -> ProcedureRun:
    """GEN-07 第 1 次执行的记录: 参数由程序库按项目, 状态的重要性与数据集声明解析 (同 GraphNodes)."""
    template = deps.library.load("GEN-07")
    procedure = procedure_of(deps, "GEN-07")
    return build_run(
        deps.library, template, procedure, engagement, state, datetime.now(UTC), traits=traits, profile=profile
    )


async def gen07(project: Gen07Project, **overrides: object) -> RuleResult:
    """以参数快照 (叠加 overrides) 跑 GEN-07 的规则."""
    deps = project.deps
    return await deps.rules.run(
        project.eid, deps.library.load("GEN-07"), project.run, project.run.params_snapshot | overrides
    )


def hits(result: RuleResult, rule_id: str) -> list[str]:
    """某条规则的例外所指的行 (source_ids 首项), 按例外编号."""
    return [x.source_ids[0] for x in result.exceptions if x.rule_id == rule_id]


async def journal_lines(project: Gen07Project, entry_nos: list[str]) -> dict[str, tuple[str, str, int]]:
    """测试库中这些凭证的分录行: 行 ID → (入账日, 科目, 借减贷)."""
    rows = await project.deps.financial_store.query(
        project.eid,
        "SELECT id, posting_date, account_code, debit_cents-credit_cents AS net_cents FROM journal_entries "
        "WHERE engagement_id=:eid AND entry_no=ANY(CAST(:entry_nos AS text[])) ORDER BY id",
        {"entry_nos": entry_nos},
    )
    return {r["id"]: (r["posting_date"].isoformat(), r["account_code"], r["net_cents"]) for r in rows}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def xero(tmp_path_factory: pytest.TempPathFactory) -> Gen07Project:
    project = await build_xero_project(
        database_url(), snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp_path_factory.mktemp("xero")
    )
    run = gen07_run(project.deps, project.engagement, project.state, project.traits, project.profile)
    return Gen07Project(project.deps, project.engagement.id, run)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def numbat(tmp_path_factory: pytest.TempPathFactory) -> Gen07Project:
    """Numbat (没有数据集声明, 取缺省; 重要性按 R55: 整体 12000000, 明显微小 600000) 经数据集载入."""
    from backend.core.config import Settings
    from backend.main import build_deps

    evidence = tmp_path_factory.mktemp("numbat") / "evidence"
    deps = build_deps(Settings(DATABASE_URL=database_url(), EVIDENCE_ROOT=evidence, LLM_MODE="mock"))
    owner = await new_owner(deps.data.engine)  # type: ignore[attr-defined]
    profile = deps.datasets.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=owner, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
    )
    await deps.datasets.load(engagement.id, "demo")
    state = initial_state(engagement, [], profile.version) | NUMBAT_MATERIALITY
    return Gen07Project(deps, engagement.id, gen07_run(deps, engagement, state, DatasetTraits(), profile))  # type: ignore[arg-type]


@pytest.mark.external
async def test_xero_gen07_on_postgres(xero: Gen07Project) -> None:
    """验收 2, 5: Xero q1 上恰好 9 条例外, 与离线预置逐字段相同 (ID, 金额, detail, 证据指针). 窗口为 10-01 至 10-17
    (期末加 31 天截到数据截止日), 各行带 window_end."""
    assert (xero.run.id, xero.run.params_snapshot["data_as_of"]) == ("RUN-GEN-07-1", date(2026, 10, 17))
    result = await gen07(xero)
    assert (result.rows, result.outcomes) == ({}, [])
    assert result.exceptions == XERO_RULES["GEN-07"](xero.run).exceptions
    assert [(x.id, x.source_ids[0], x.amount_cents) for x in result.exceptions] == [
        ("X-GEN-07-1-S01-1", "BT-090-0092", 618750),
        ("X-GEN-07-1-S01-2", "BT-090-0097", -450000),
        ("X-GEN-07-1-S01-3", "BT-090-0099", 200000),
        ("X-GEN-07-1-S01-4", "BT-090-0107", 100000),
        ("X-GEN-07-1-S01-5", "BT-090-0130", 150000),
        ("X-GEN-07-1-S01-6", "BT-090-0131", 45000),
        ("X-GEN-07-1-S02-1", "XJ-720-2", 115500),
        ("X-GEN-07-1-S03-1", "XJ-626-2", 630000),
        ("X-GEN-07-1-S03-2", "XJ-676-2", 196999),
    ]
    s01 = [(x.detail["txn_date"], x.detail["description"], x.detail["reference"]) for x in result.exceptions[:6]]
    assert s01 == [
        ("2026-10-10", "Ridgeway University", "ORC01025"),
        ("2026-10-11", "SMART Agency", "70135 70209"),
        ("2026-10-11", "Jakaranda Maple Systems", "DEPOSIT ADV"),
        ("2026-10-12", "e-Bank dep", None),
        ("2026-10-15", "e-Bank dep", None),
        ("2026-10-15", "Majestic Contracting", "Sublet RM2031"),
    ]
    journal = [
        (x.detail["posting_date"], x.detail["account_code"], x.detail["source_doc"]) for x in result.exceptions[6:]
    ]
    assert journal == [
        ("2026-10-16", "610", "1026C"),
        ("2026-10-04", "710", "MKT-08123"),
        ("2026-10-11", "720", "PC Complete"),
    ]
    assert {x.detail["window_end"] for x in result.exceptions} == {"2026-10-17"}


@pytest.mark.external
async def test_xero_gen07_counter_examples(xero: Gen07Project) -> None:
    """验收 6: 能区分对错的反例.
    - S01: BT-090-0085 (214500) 与 BT-090-0124 (385000) 在 ±10 天内有同额的总账银行科目行 XJ-690-2, XJ-695-2,
      BT-091-0004 (-450000) 有 XJ-732-2, 都不命中; BT-090-0097 (-450000) 在 ±10 天内只有作废对 XJ-728/XJ-744 的
      XJ-744-2 同额, 剔除作废对后仍命中; 不剔除作废对 (没有冲回格式) 时它会漏掉.
    - S02: XJ-756 (存货期初, 各科目净额为 0), 常规赊销 (XJ-612: 借应收, 贷收入) 与赊购 (XJ-613: 贷应付) 都在窗口内,
      都不进 S02; 去掉 "各科目净额都为 0" 的剔除时 XJ-756 会进来.
    - data_as_of 改为 10-14 时 S01 只剩前 4 条 (10-15 的 BT-090-0130, 0131 落在窗口外), S02 为 0 条 (XJ-720 在 10-16),
      S03 仍为 2 条."""
    result = await gen07(xero)
    s01 = hits(result, "S01")
    assert {"BT-090-0085", "BT-090-0124", "BT-091-0004"}.isdisjoint(s01) and "BT-090-0097" in s01
    lines = await journal_lines(xero, ["XJ-690", "XJ-695", "XJ-732", "XJ-728", "XJ-744"])
    assert {k: lines[k] for k in ("XJ-690-2", "XJ-695-2", "XJ-732-2", "XJ-744-2", "XJ-728-2")} == {
        "XJ-690-2": ("2026-10-14", "090", 214500),
        "XJ-695-2": ("2026-10-14", "090", 385000),
        "XJ-732-2": ("2026-10-17", "091", -450000),
        "XJ-744-2": ("2026-10-17", "090", -450000),  # 作废对: XJ-744 是 XJ-728 的冲回
        "XJ-728-2": ("2026-10-17", "091", -450000),
    }
    unvoided = await gen07(xero, reversal_reference_pattern=None, reversal_entry_no_template=None)
    assert hits(unvoided, "S01") == [x for x in s01 if x != "BT-090-0097"]

    assert hits(result, "S02") == ["XJ-720-2"]
    routine = await journal_lines(xero, ["XJ-756", "XJ-612", "XJ-613"])
    assert {line[0] for line in routine.values()} <= {"2026-10-01", "2026-10-17"} and len(routine) == 12
    opening = [(acct, net) for line_id, (_, acct, net) in routine.items() if line_id.startswith("XJ-756-")]
    assert {acct for acct, _ in opening} == {"630"} and sum(net for _, net in opening) == 0  # 只有存货一个科目, 净额 0
    [s02] = [r for r in xero.deps.library.load("GEN-07").rules if r.id == "S02"]
    netting = "GROUP BY entry_no HAVING bool_or(net_cents<>0)"
    assert s02.sql.count(netting) == 1
    rows = await xero.deps.financial_store.query(
        xero.eid, s02.sql.replace(netting, "GROUP BY entry_no"), xero.run.params_snapshot
    )
    assert [r["source_id"] for r in rows] == ["XJ-720-2", "XJ-756-4"]  # 不剔除净额为 0 的凭证时 XJ-756 会进来
    assert {(acct, net) for line_id, (_, acct, net) in routine.items() if line_id.startswith("XJ-612-")} >= {
        ("610", 385000),
        ("200", -350000),
    }
    assert ("800", -118125) in {
        (acct, net) for line_id, (_, acct, net) in routine.items() if line_id.startswith("XJ-613-")
    }

    early = await gen07(xero, data_as_of=date(2026, 10, 14))
    assert hits(early, "S01") == ["BT-090-0092", "BT-090-0097", "BT-090-0099", "BT-090-0107"]
    assert (hits(early, "S02"), hits(early, "S03")) == ([], ["XJ-626-2", "XJ-676-2"])
    assert {x.detail["window_end"] for x in early.exceptions} == {"2026-10-14"}


async def test_numbat_gen07_sql_runs(numbat: Gen07Project) -> None:
    """验收 7: Numbat (没有数据集声明: data_as_of 不设上限, 没有冲回格式) 上 GEN-07 的 SQL 能执行不报错 (不断言条数);
    窗口为期末后 31 天."""
    params = numbat.run.params_snapshot
    assert (params["data_as_of"], params["reversal_reference_pattern"], params["reversal_entry_no_template"]) == (
        None,
        None,
        None,
    )
    result = await gen07(numbat)
    assert result.outcomes == [] and {x.detail["window_end"] for x in result.exceptions} <= {"2026-07-31"}
