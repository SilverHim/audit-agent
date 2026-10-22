"""Real PostgreSQL acceptance tests. Set TEST_DATABASE_URL to a migrated disposable database."""

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Index, delete, event, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.adapters.confirmation_mock import DatasetConfirmationChannel, MockConfirmationChannel
from backend.adapters.registry_mock import DatasetRegistrySource, MockRegistrySource
from backend.adapters.xero_supplements import SupplementMismatch
from backend.contracts.errors import EngagementLocked, VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditObjective,
    AuditPlan,
    ClientProfile,
    ComplianceCheck,
    DocumentType,
    Engagement,
    EngagementTerms,
    GovernanceCommunication,
    IndependenceDeclaration,
    LlmSettings,
    OwnershipGraph,
    PlanStatus,
    ProcedureRun,
    ProfileSource,
    Reliability,
    ReportStatus,
    RepresentationStatus,
    RunPopulation,
    TermsCheckStatus,
    TrailEntry,
    TrailKind,
    Versioned,
    WrittenRepresentationLetter,
)
from backend.contracts.ports import ConfirmationRequest
from backend.contracts.records import Account, JournalEntry, SupplierBill
from backend.persistence import models as m
from backend.persistence.db import transaction
from backend.persistence.stores import (
    CsvDatasetLoader,
    FileEvidenceStore,
    PostgresAuditOutputStore,
    PostgresEngagementStore,
    PostgresFinancialDataStore,
    PostgresLlmSettingsStore,
    PostgresUserStore,
    RoutingDatasetLoader,
    XeroDatasetLoader,
    now,
)
from backend.services.account_mapping import with_category
from backend.services.audit_trail import PostgresAuditTrail
from backend.services.procedure_library import ProcedureLibrary
from tests.external import snapshot_dir, supplements_dir
from tests.fakes import (
    NUMBAT_ACCOUNTS,
    NUMBAT_ROWS,
    audit_plan,
    compliance_check,
    engagement_terms,
    finished,
    governance_communication,
    independence_declaration,
    make_run,
    numbat_rev08_populations,
    ownership_graph,
    representation_letter,
)
from tests.test_data_store_postgres import migration as migration_module
from tests.test_fakes import demo_rows, trail_pages_hold
from tests.test_supplier_bills import Q1_BILLS, SELECT_BILLS

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def storage(tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="Test Auditor", password_hash="test", created_at=now()
            )
        )
    evidence = FileEvidenceStore(tmp_path, engine)
    loader = CsvDatasetLoader(ROOT / "data", engine, evidence)
    engagements = PostgresEngagementStore(engine)
    profile = loader.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await engagements.create(
        owner_id=user, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
    )
    yield engine, engagement, loader, evidence
    await engine.dispose()


XERO_Q1 = "xero/20261017-q1"


@dataclass
class XeroStorage:
    """真实库上的 Xero 装配 (xero_storage): 引擎, 新建的用户, 证据存储, 载入器 (快照与补造取仓库的), 项目存储;
    api 与 supplements 为快照与补造的上一级目录."""

    engine: AsyncEngine
    user: str
    evidence: FileEvidenceStore
    xero: XeroDatasetLoader
    engagements: PostgresEngagementStore
    api: Path
    supplements: Path

    async def engagement(self, dataset: str = XERO_Q1, period_end: date = date(2026, 9, 30)) -> Engagement:
        """新建项目 (期间自 2026-07-01 起); 画像取数据集的: xero/ 取载入器的, 其余取 data/ 下的 CSV 数据集."""
        loader = self.xero if dataset.startswith("xero/") else CsvDatasetLoader(ROOT / "data")
        return await self.engagements.create(
            owner_id=self.user,
            period_start=date(2026, 7, 1),
            period_end=period_end,
            dataset=dataset,
            profile=loader.client_profile(dataset),
        )


@pytest.fixture
async def xero_storage(tmp_path):
    """Xero 用例的开场: 先核对 xero/20261017-q1 的快照与补造 (tests.external: 跟踪了却找不到时失败, 哈希不符跳过),
    没有 TEST_DATABASE_URL 时跳过; 建一个用户, 证据存在 tmp_path. 见 XeroStorage."""
    api, supplements = snapshot_dir("20261017-q1").parent, supplements_dir("20261017-q1").parent
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="Xero", password_hash="x", created_at=now()
            )
        )
    evidence = FileEvidenceStore(tmp_path, engine)
    xero = XeroDatasetLoader(api, ROOT / "src/backend/knowledge/planning", engine, evidence, supplements=supplements)
    yield XeroStorage(engine, user, evidence, xero, PostgresEngagementStore(engine), api, supplements)
    await engine.dispose()


async def test_load_rules_and_project_isolation(storage):
    engine, e, loader, _evidence = storage
    await asyncio.gather(loader.load(e.id, "demo"), loader.load(e.id, "demo"))
    financial = PostgresFinancialDataStore(engine)
    assert await financial.data_version(e.id) == 1
    assert await financial.query(e.id, "SELECT current_setting('statement_timeout') AS timeout", {}) == [
        {"timeout": "10s"}
    ]
    assert await financial.query(e.id, "SELECT :eid AS project", {"eid": "another-project"}) == [{"project": e.id}]

    assert await financial.gl_balance(e.id, "1010", date(2026, 6, 30)) == 125840000
    assert await financial.gl_balance(e.id, "3200", date(2026, 6, 30)) == -376100000
    assert await financial.gl_balance(e.id, "4100", date(2026, 6, 30)) == -1200000000
    assert len(await financial.documents(e.id)) == 3
    async with engine.connect() as conn:
        assert not (await conn.execute(select(m.FINANCIAL["sales_invoices"]))).all()
        role = (
            await conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one()
        assert role == (False, False)
    assert not await financial.sales_invoices("other-project", ["SI-2026-0612"])
    library = ProcedureLibrary(ROOT / "src/backend/knowledge")
    import yaml

    expected = {
        ("REV-08", "R01"): ["SI-2026-0612"],
        ("REV-08", "R02"): [],
        ("REV-08", "R03"): [],
        ("CSH-05", "B01"): ["BA-01"],
        ("PLN-07", "A01"): ["1100"],
        ("GEN-05", "R01"): ["SJ-2026-0612-2"],  # 需求 R131: 悉尼周日 21:14 录入的收入, 自审自批
        ("GEN-05", "R04"): ["SJ-2026-0612-1"],
        ("GEN-05", "R08"): ["SJ-2026-0612-1"],
    }
    params = dict(
        period_end="2026-06-30",
        window_days=20,
        change_pct=0.2,
        min_change_revenue_pct=0.005,
        period_start="2025-07-01",
        prior_period_start=None,
        revenue_categories=["revenue"],
        cost_of_sales_categories=["cost_of_sales"],
        employee_expense_categories=["employee_expenses"],
        income_tax_categories=["income_tax"],
        subsequent_days=10,
        lookback_days=5,
        threshold_cents=9000000,
        reversal_reference_pattern=None,  # Numbat 没有数据集声明
        reversal_entry_no_template=None,
        # GEN-05 (需求 R131): 本循环只跑规则 SQL, 不跑执行前检查; 审批限额与错报沟通时间为空时 R03, R06 取不到行
        performance_materiality_cents=9000000,
        clearly_trivial_cents=600000,
        approval_limit_cents=None,
        entry_timestamps_reliable=True,
        entry_timestamps_note=None,
        audit_adjustment_entry_prefix="AJE-",
        timezone="Australia/Sydney",
        business_hours=[7, 20],
        rare_user_max=2,
        round_unit_cents=1000000,
        cash_categories=["cash"],
        receivables_categories=["receivables"],  # 需求 R132: R10, R12 的贷项凭证与名称比较
        counterparty_prefixes=["payment to", "payment from", "payment:"],
        entity_suffixes=["pty ltd", "pty limited", "pty", "ltd", "limited"],
        entry_nos=None,
        audit_adjustment_start=None,
        # GEN-03 (需求 R139): 名称与 ACN 由 A6 按关系图在执行时合并进参数, 这里不给对象, C01 取不到行
        party_names=[],
        party_acns=[],
        equity_categories=["share_capital", "retained_earnings", "other_equity"],
        # GEN-07 (需求 R177): 期后窗口的数据截止日 (Numbat 没有数据集声明, 不设上限), S01, S03 的整体重要性, S01 的天数
        data_as_of=None,
        materiality_cents=12000000,
        match_days=10,
        # FIN-09 (需求 R164): V01 的审计更正分录前缀 (Numbat 没有数据集声明, 取缺省)
        adjustment_prefix="AJE-",
    )
    for path in (
        (library.root / "procedures").glob("*.yaml")
        if hasattr(library, "root")
        else (ROOT / "src/backend/knowledge/procedures").glob("*.yaml")
    ):
        template = yaml.safe_load(path.read_text())
        for rule in template.get("rules", []):
            rows = await financial.query(e.id, rule["sql"], params)
            if (template["id"], rule["id"]) in expected:
                assert [r["source_id"] for r in rows] == expected[template["id"], rule["id"]]
            if rule["id"] == "M01":
                assert {r["basis"]: r["base_amount_cents"] for r in rows} == dict(
                    revenue=1200000000, total_assets=800000000, profit_before_tax=150000000
                )
            if rule["id"] == "D02":
                assert len(rows) == 8
            if (template["id"], rule["id"]) == ("PLN-07", "A03"):
                assert len(rows) == 12
    with pytest.raises(DBAPIError, match="read-only"):
        await financial.query(e.id, "DELETE FROM sales_invoices", {})
    with pytest.raises(DBAPIError):
        await financial.query(e.id, "SELECT 1; DELETE FROM sales_invoices", {})


async def test_journal_entries_and_accounts(storage):
    """分录行按凭证号或原始单据号取, 科目按代码取 (开发接口文档第 4 节), 与假存储同样的行; 按项目隔离."""
    engine, e, loader, _evidence = storage
    await loader.load(e.id, "demo")
    financial = PostgresFinancialDataStore(engine)

    sale = await financial.journal_entries(e.id, entry_nos=["SJ-2026-0612"])
    assert [(r.id, r.account_code, r.debit_cents, r.credit_cents) for r in sale] == [
        ("SJ-2026-0612-1", "1100", 14200000, 0),
        ("SJ-2026-0612-2", "4100", 0, 14200000),
    ]
    assert sale == [r for r in NUMBAT_ROWS if isinstance(r, JournalEntry) and r.entry_no == "SJ-2026-0612"]
    assert await financial.journal_entries(e.id, source_docs=["SI-2026-0612"]) == sale
    assert await financial.journal_entries(e.id, entry_nos=["SJ-2026-0612"], source_docs=["SI-2026-0612"]) == sale
    union = await financial.journal_entries(e.id, entry_nos=["SJ-2026-0618"], source_docs=["SI-2026-0612"])
    assert [r.id for r in union] == ["SJ-2026-0612-1", "SJ-2026-0612-2", "SJ-2026-0618-1", "SJ-2026-0618-2"]
    assert await financial.journal_entries(e.id, entry_nos=["SI-2026-0612"]) == []
    assert await financial.journal_entries(e.id, entry_nos=[]) == []  # 空列表的条件取不到行, 不取全表
    assert await financial.journal_entries(e.id, source_docs=[]) == []
    assert await financial.journal_entries(e.id, entry_nos=[], source_docs=[]) == []
    assert await financial.journal_entries(e.id, entry_nos="SJ-2026-0612") == []  # 单个字符串逐字符拆开, 同假存储
    assert (
        await financial.journal_entries("other-project", entry_nos=["SJ-2026-0612"], source_docs=["SI-2026-0612"]) == []
    )
    with pytest.raises(ValueError, match="不取全表"):
        await financial.journal_entries(e.id)

    picked = await financial.accounts(e.id, ["4100", "1100"])
    assert [(a.code, a.name, a.type, a.category) for a in picked] == [
        ("1100", "Trade receivables", "asset", "receivables"),
        ("4100", "Sales revenue", "revenue", "revenue"),
    ]
    assert picked == NUMBAT_ACCOUNTS
    everything = await financial.accounts(e.id)
    assert len(everything) == 23
    assert [a.code for a in everything] == sorted(a.code for a in everything)
    assert await financial.accounts(e.id, []) == []
    assert await financial.accounts("other-project") == []


async def test_journal_entries_and_accounts_sort_rows_inserted_in_reverse(storage):
    """新项目 (不载入 demo) 把 data/demo 的科目与几笔分录行倒序写入: 物理顺序与键序相反, accounts 仍按代码升序,
    journal_entries 仍按行 ID 升序 (载入 demo 后物理顺序本就是键序, 测不出排序)."""
    engine, e, _loader, _evidence = storage
    accounts = [with_category(Account.model_validate(r)) for r in demo_rows("accounts")]
    wanted = {"SJ-2026-0612", "SJ-2026-0618", "SJ-2026-0547", "CR-SI-2026-0547"}
    entries = [JournalEntry.model_validate(r) for r in demo_rows("journal_entries") if r["entry_no"] in wanted]
    async with transaction(engine, e.id) as conn:
        for a in sorted(accounts, key=lambda a: a.code, reverse=True):
            await conn.execute(insert(m.FINANCIAL["accounts"]).values(engagement_id=e.id, **a.model_dump()))
        for j in sorted(entries, key=lambda j: j.id, reverse=True):
            await conn.execute(insert(m.FINANCIAL["journal_entries"]).values(engagement_id=e.id, **j.model_dump()))
    financial = PostgresFinancialDataStore(engine)

    everything = await financial.accounts(e.id)
    assert len(everything) == 23 and [a.code for a in everything] == sorted(a.code for a in everything)
    union = await financial.journal_entries(
        e.id, entry_nos=["SJ-2026-0618"], source_docs=["SI-2026-0612", "SI-2026-0547"]
    )
    assert [r.id for r in union] == sorted(j.id for j in entries) and len(union) == 8


async def test_versions_rollback_trail_and_lock(storage):
    engine, e, loader, evidence = storage
    await loader.load(e.id, "demo")
    store = PostgresAuditOutputStore(engine)
    trail = PostgresAuditTrail(engine)
    engagements = PostgresEngagementStore(engine)
    obj = loader.planning_presets("demo")[0][0]
    await asyncio.gather(store.put(e.id, [obj], actor="A"), store.put(e.id, [obj], actor="B"))
    assert len(await store.history(e.id, AuditObjective, obj.id)) == 1
    changed = obj.model_copy(update={"account": "changed"})
    assert (await store.put(e.id, [changed], actor="B"))[0] == obj
    with pytest.raises(VersionConflict):
        await store.put(e.id, [obj.model_copy(update={"version": 3})], actor="B")
    doc = (await PostgresFinancialDataStore(engine).documents(e.id))[0]
    profile = await engagements.client_profile(e.client_id)
    with pytest.raises(RuntimeError):
        async with store.transaction(e.id):
            await store.put(e.id, [obj.model_copy(update={"version": 2})], actor="B")
            await evidence.set_status(e.id, doc.id, "extracted")
            await engagements.save_profile(
                profile.model_copy(update={"version": 2}), source=ProfileSource.update, updated_by="U-1"
            )
            await engagements.update(e.id, client_profile_version=2)
            await asyncio.wait_for(
                trail.record(
                    TrailEntry(
                        engagement_id=e.id,
                        kind=TrailKind.transition,
                        actor="B",
                        summary="survives rollback",
                        idempotency_key="rollback-test",
                    )
                ),
                3,
            )
            raise RuntimeError("rollback")
    assert (await store.get(e.id, AuditObjective, obj.id)).version == 1
    assert (await PostgresFinancialDataStore(engine).documents(e.id))[0].status == "received"
    assert (await engagements.client_profile(e.client_id)).version == 1
    assert await trail.find_success(e.id, "rollback-test")
    # 算不算写成只看 error 与 success, 不看 payload 的 status: 失败执行的结束留痕也算 (需求 R325)
    for key, payload, written in [
        ("run-failed", {"action": "finished", "status": "failed"}, True),
        ("call-error", {"error": "boom"}, False),
        ("call-unsuccessful", {"success": False}, False),
    ]:
        entry = TrailEntry(
            engagement_id=e.id, kind=TrailKind.run, actor="A1", summary=key, idempotency_key=key, payload=payload
        )
        await trail.record(entry)
        assert (await trail.find_success(e.id, key) is not None) is written, key
    with pytest.raises(DBAPIError):
        async with transaction(engine, e.id) as conn:
            await conn.execute(text("DELETE FROM audit_trail"))
    await engagements.update(e.id, locked=True)
    with pytest.raises(EngagementLocked):
        await store.put(e.id, [obj], actor="B")
    with pytest.raises(EngagementLocked):
        await engagements.update(e.id, locked=False)
    with pytest.raises(EngagementLocked):
        await evidence.set_status(e.id, doc.id, "extracted")


async def test_trail_last_of_filters_action_and_takes_latest(storage):
    """AuditTrail.last_of (需求 R282): 按 kind 与 payload.action 过滤, 取写入时间最近的一条; 只看本项目 (行级安全);
    没有为 None."""
    engine, e, _, _ = storage
    trail = PostgresAuditTrail(engine)
    base = datetime(2026, 12, 15, 1, 0, tzinfo=UTC)

    def entry(action: str, minute: int, kind: TrailKind = TrailKind.data, eid: str | None = None) -> TrailEntry:
        return TrailEntry(
            engagement_id=eid or e.id,
            kind=kind,
            actor="U-1",
            summary=action,
            payload={"action": action},
            created_at=base + timedelta(minutes=minute),
        )

    for item in [
        entry("xero_synced", 1),
        entry("xero_sync_failed", 3),
        entry("sync", 5),  # 不在 actions 里
        entry("xero_synced", 4, TrailKind.transition),  # 不是 data
        entry("xero_synced", 2),  # 后写入, 时间较早
    ]:
        await trail.record(item)
    actions = ("xero_synced", "xero_sync_failed", "xero_disconnect")
    found = await trail.last_of(e.id, TrailKind.data, actions)
    assert found is not None and (found.payload["action"], found.created_at) == (
        "xero_sync_failed",
        base + timedelta(minutes=3),
    )
    synced = await trail.last_of(e.id, TrailKind.data, ("xero_synced",))
    assert synced is not None and synced.created_at == base + timedelta(minutes=2)
    assert await trail.last_of(e.id, TrailKind.data, ("xero_disconnect",)) is None
    assert await trail.last_of(e.id, TrailKind.data, ()) is None
    assert await trail.last_of("ENG-NOT-MINE", TrailKind.data, actions) is None


async def test_trail_list_pages(storage):
    """AuditTrail.list 的 newest 与 before (同假实现的契约, 场景见 trail_pages_hold); 别的项目的留痕与游标经行级安全
    排除. 防真实实现与假实现的排序或游标不一致."""
    engine, e, loader, _ = storage
    other = await PostgresEngagementStore(engine).create(
        owner_id=e.owner_id,
        period_start=e.period_start,
        period_end=e.period_end,
        dataset="demo",
        profile=loader.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex}),
    )
    await trail_pages_hold(PostgresAuditTrail(engine), e.id, other.id)


# ── 需求 R298: 留痕表按项目读取的索引 ──

TIMELINE_KINDS = [TrailKind.transition, TrailKind.decision, TrailKind.deferral, TrailKind.data, TrailKind.conversation]
KEY = "R298:transition:p0_intel"  # 测试写入的一条带防重复键的留痕
TrailRead = Callable[[PostgresAuditTrail, str, str], Awaitable[object]]  # (留痕, 项目, 游标) → 一次读取
TRAIL_READS = [
    # 工作台时间线 (前端 lib/timeline.ts): 最近一页与以最早一条为游标往前翻, 带或不带它的那几类; 索引须给出顺序
    pytest.param(lambda t, eid, _: t.list(eid, None, limit=100, newest=True), m.TRAIL_BY_TIME, True, id="newest"),
    pytest.param(
        lambda t, eid, at: t.list(eid, None, limit=100, newest=True, before=at), m.TRAIL_BY_TIME, True, id="before"
    ),
    pytest.param(
        lambda t, eid, _: t.list(eid, TIMELINE_KINDS, limit=100, newest=True),
        m.TRAIL_BY_TIME,
        True,
        id="timeline_newest",
    ),
    pytest.param(
        lambda t, eid, at: t.list(eid, TIMELINE_KINDS, limit=100, newest=True, before=at),
        m.TRAIL_BY_TIME,
        True,
        id="timeline_before",
    ),
    # 只取一类: trail_pages.all_of_kind 的一页 (按写入顺序), last_of (最近一条); 索引须给出顺序
    pytest.param(
        lambda t, eid, _: t.list(eid, TrailKind.deferral, limit=500), m.TRAIL_BY_KIND, True, id="one_kind_page"
    ),
    pytest.param(
        lambda t, eid, _: t.last_of(eid, TrailKind.data, ("xero_synced",)), m.TRAIL_BY_KIND, True, id="last_of"
    ),
    # 按防重复键取: 一个键至多几条, 排序可以留着
    pytest.param(lambda t, eid, _: t.find_success(eid, KEY), m.TRAIL_BY_KEY, False, id="find_success"),
]


@pytest.mark.parametrize(("read", "index", "ordered"), TRAIL_READS)
async def test_trail_reads_use_the_indexes(storage, read: TrailRead, index: Index, ordered: bool) -> None:
    """验收 2: 截下 PostgresAuditTrail 这次读取发出的 SQL, 断言目标索引能直接服务这种读法 (不断言代价).

    EXPLAIN 在属主角色的回滚事务里做: 设 app.engagement_id (行级安全的项目条件同真实查询), 删掉同表另两个新索引只留
    目标索引, 关掉顺序扫描, 要顺序的读法 (ordered) 另关排序, 最后回滚. 断言: 计划里有目标索引, 它那一行的 Index Cond
    含 engagement_id; 要顺序的读法计划里没有 Sort (索引直接给出 (created_at, id) 的顺序, 不是取出再排).
    不删别的索引时选哪个随库里的统计而变: 规划器估选择率时代入 current_setting 的当前值, 用例的项目是新建的, 不在统计
    里, 估约 1 行, 几个索引代价几乎相同 (第 1 轮审核意见 01). 按键取的 find_success 一个键至多几条, 排序留着."""
    engine, e, _, _ = storage
    trail = PostgresAuditTrail(engine)
    ids = [
        await trail.record(
            TrailEntry(
                engagement_id=e.id,
                kind=kind,
                actor="U-1",
                summary=kind.value,
                payload={"action": "xero_synced"},
                idempotency_key=KEY if kind is TrailKind.transition else None,
            )
        )
        for kind in (TrailKind.data, TrailKind.transition, TrailKind.deferral)
    ]
    sent: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().startswith("SELECT") and "audit_trail" in statement:
            sent.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await read(trail, e.id, ids[1])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    [(statement, parameters)] = sent
    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 在属主角色的回滚事务里只留目标索引")
    owner = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with owner.connect() as conn:
            outer = await conn.begin()
            try:
                await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": e.id})
                for other in (m.TRAIL_BY_TIME, m.TRAIL_BY_KIND, m.TRAIL_BY_KEY):
                    if other is not index:
                        await conn.execute(text(f"DROP INDEX {other.name}"))
                await conn.execute(text("SET LOCAL enable_seqscan = off"))
                if ordered:
                    await conn.execute(text("SET LOCAL enable_sort = off"))
                plan = list((await conn.exec_driver_sql(f"EXPLAIN {statement}", parameters)).scalars())
            finally:
                await outer.rollback()
    finally:
        await owner.dispose()
    shown = "\n".join(plan)
    # 用到目标索引的扫描 (游标那一条的 created_at 有时也经它取, 计划里出现两次), 各自的 Index Cond 都含 engagement_id
    scans = [n for n, line in enumerate(plan) if index.name in line]
    assert scans, shown
    for scan in scans:
        assert "engagement_id" in next(line for line in plan[scan + 1 :] if "Index Cond" in line), shown
    assert not ordered or "Sort" not in shown, shown


# pg_indexes 的写法 (USING btree 之后): 列, 部分索引另有条件
TRAIL_INDEXES = {
    m.TRAIL_BY_TIME.name: "(engagement_id, created_at, id)",
    m.TRAIL_BY_KIND.name: "(engagement_id, kind, created_at, id)",
    m.TRAIL_BY_KEY.name: "(engagement_id, idempotency_key) WHERE (idempotency_key IS NOT NULL)",
}


async def test_migration_0015_round_trip(storage) -> None:
    """验收 3: 以属主角色在一个事务里走 0015 降级再升级 (两次), 最后回滚. 测试库的留痕表已有数据; 降级后三个索引
    都没了, 升级后定义同 TRAIL_INDEXES (列与部分索引的条件; 迁移与 0001 都直接用模型里的 Index 对象建), 再升级一次
    不报错. 新库 0001 按当前模型建表时已带这三个索引, 0015 跳过. 需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    engine, e, _, _ = storage
    await PostgresAuditTrail(engine).record(
        TrailEntry(engagement_id=e.id, kind=TrailKind.data, actor="U-1", summary="x", idempotency_key=KEY)
    )
    module = migration_module("0015_audit_trail_indexes")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    owner = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with owner.connect() as conn:
            outer = await conn.begin()
            try:

                async def indexes() -> dict[str, str]:
                    found = await conn.execute(
                        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'audit_trail'")
                    )
                    return {name: definition.split(" USING btree ")[1] for name, definition in found}

                assert (await indexes()).items() >= TRAIL_INDEXES.items()
                await conn.run_sync(run("downgrade"))
                assert not set(await indexes()) & set(TRAIL_INDEXES)
                await conn.run_sync(run("upgrade"))
                await conn.run_sync(run("upgrade"))  # 可重复执行
                assert (await indexes()).items() >= TRAIL_INDEXES.items()
            finally:
                await outer.rollback()
    finally:
        await owner.dispose()


async def test_working_language_on_postgres(storage) -> None:
    """需求 R339: 建项目缺省 en, 带 zh 时为 zh; set_working_language 返回改之前的值, 相同时不改 (updated_at 不动);
    项目锁定后抛 EngagementLocked, 不改."""
    engine, e, loader, _ = storage
    engagements = PostgresEngagementStore(engine)
    assert e.working_language == "en"
    assert await engagements.set_working_language(e.id, "zh") == "en"
    changed = await engagements.get(e.id)
    assert changed is not None and changed.working_language == "zh"
    assert await engagements.set_working_language(e.id, "zh") == "zh"
    assert await engagements.get(e.id) == changed
    await engagements.update(e.id, locked=True)
    with pytest.raises(EngagementLocked):
        await engagements.set_working_language(e.id, "en")
    assert (await engagements.get(e.id)).working_language == "zh"  # type: ignore[union-attr]
    profile = loader.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    zh = await engagements.create(
        owner_id=e.owner_id,
        period_start=e.period_start,
        period_end=e.period_end,
        dataset="demo",
        profile=profile,
        working_language="zh",
    )
    assert (await engagements.get(zh.id)).working_language == "zh"  # type: ignore[union-attr]


async def test_migration_0023_round_trip(storage) -> None:
    """需求 R339 验收 5: 以属主角色在一个事务里走 0023 降级再升级 (两次), 最后回滚. 测试库的项目表已有行 (含一个 zh
    的); 降级后没有这一列, 升级后各行为 en (旧库的项目都是英文), 缺省 en, 再升级一次不报错. 新库 0001 按当前模型
    建表时已带这一列, 0023 跳过. 需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    engine, e, _, _ = storage
    await PostgresEngagementStore(engine).set_working_language(e.id, "zh")
    module = migration_module("0023_engagement_working_language")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    column = text(
        "SELECT column_default, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'engagements' AND column_name = 'working_language'"
    )
    owner = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with owner.connect() as conn:
            outer = await conn.begin()
            try:
                assert (await conn.execute(column)).all() == [("'en'::text", "NO")]
                await conn.run_sync(run("downgrade"))
                assert (await conn.execute(column)).all() == []
                await conn.run_sync(run("upgrade"))
                await conn.run_sync(run("upgrade"))  # 可重复执行
                assert (await conn.execute(column)).all() == [("'en'::text", "NO")]
                languages = await conn.execute(text("SELECT DISTINCT working_language FROM engagements"))
                assert languages.scalars().all() == ["en"]
            finally:
                await outer.rollback()
    finally:
        await owner.dispose()


async def test_ownership_graph_versions_on_postgres(storage) -> None:
    """R136 验收 6: OWNERSHIP v1, v2 写进 ownership_graphs 表, 按版本读回仍是 OwnershipGraph (节点与关系不丢); 最新为
    v2, 历史两版; 别的项目读不到 (行级安全). 迁移的往返与授权见 test_data_store_postgres 的新表用例, 迁移链只有一个头
    由缺省组的单头用例守."""
    engine, e, _, _ = storage
    store = PostgresAuditOutputStore(engine)
    v1 = ownership_graph()
    v2 = v1.model_copy(update={"version": 2, "links": v1.links[:2]})
    await store.put(e.id, [v1], actor="A5")
    await store.put(e.id, [v2], actor="A5")

    assert m.OUTPUTS["OwnershipGraph"].name == "ownership_graphs"
    assert await store.get(e.id, OwnershipGraph, "OWNERSHIP", 1) == v1
    assert await store.get(e.id, OwnershipGraph, "OWNERSHIP") == v2
    assert await store.history(e.id, OwnershipGraph, "OWNERSHIP") == [v1, v2]
    assert await store.latest(e.id, OwnershipGraph) == [v2]
    assert await store.get("ENG-OTHER", OwnershipGraph, "OWNERSHIP") is None


async def test_run_population_on_postgres(storage) -> None:  # type: ignore[no-untyped-def]
    """需求 R324 审核 R324-03: 被测总体 (Numbat REV-08 的三条规则) 与执行记录终态一次写入 procedures 表, 按 ID 读回
    相等 (规则, 项目与识别字段不丢, 仍是 RunPopulation); 同表按 kind 分开, latest 各取各的; 别的项目读不到
    (行级安全)."""
    engine, e, _, _ = storage
    store = PostgresAuditOutputStore(engine)
    run = finished(make_run("REV-08"))
    population = RunPopulation(
        id="POP-RUN-REV-08-1", run_id=run.id, procedure_id="REV-08", rules=numbat_rev08_populations()
    )
    await store.put(e.id, [run, population], actor="A6")

    assert m.OUTPUTS["RunPopulation"].name == "procedures"
    assert await store.get(e.id, RunPopulation, "POP-RUN-REV-08-1") == population
    assert await store.latest(e.id, RunPopulation) == [population]
    assert await store.latest(e.id, ProcedureRun) == [run]
    assert await store.get("ENG-OTHER", RunPopulation, "POP-RUN-REV-08-1") is None


async def test_workpaper_versions_share_the_procedure_table(storage) -> None:
    """底稿与执行记录, 被测总体同表但互不混淆; 回滚, 版本留存与项目隔离均由真实存储保证."""
    from backend.contracts.models import (
        LocalizedText,
        ObjectRef,
        Workpaper,
        WorkpaperColumn,
        WorkpaperContent,
        WorkpaperHeader,
        WorkpaperSection,
        WorkpaperTable,
    )

    engine, e, _, _ = storage
    store = PostgresAuditOutputStore(engine)
    run = finished(make_run("REV-08"))
    population = RunPopulation(
        id="POP-RUN-REV-08-1", run_id=run.id, procedure_id="REV-08", rules=numbat_rev08_populations()
    )
    paper = Workpaper(
        id="WP-D-3",
        ref="D-3",
        area="D",
        procedure_id="REV-08",
        basis=[ObjectRef(kind=type(obj).__name__, id=obj.id, version=obj.version) for obj in (run, population)],
        content=WorkpaperContent(
            header=WorkpaperHeader(
                title=LocalizedText(zh="测试", en="Test"),
                client_name="Synthetic client",
                engagement_id=e.id,
                procedure_id="REV-08",
                preparer=LocalizedText(zh="智能体", en="Agent"),
            ),
            sections=[
                WorkpaperSection(
                    id="details",
                    title=LocalizedText(zh="明细", en="Details"),
                    children=[
                        WorkpaperSection(
                            id="subject",
                            title=LocalizedText(zh="对象", en="Subject"),
                            generated_language="en",
                            paragraphs=["Synthetic original text"],
                        )
                    ],
                    tables=[
                        WorkpaperTable(
                            id="items",
                            columns=[
                                WorkpaperColumn(
                                    key="amount", label=LocalizedText(zh="金额", en="Amount"), format="money"
                                )
                            ],
                            rows=[{"amount": 990000, "description": LocalizedText(zh="原文", en="Original")}],
                        )
                    ],
                )
            ],
        ),
    )
    async with store.transaction(e.id):
        await store.put(e.id, [run, population, paper], actor="A6")
    revised = paper.model_copy(update={"version": 2, "conclusion": "Reviewed against the supporting records."})
    with pytest.raises(RuntimeError, match="rollback"):
        async with store.transaction(e.id):
            await store.put(e.id, [revised], actor=e.owner_id)
            raise RuntimeError("rollback")
    assert await store.get(e.id, Workpaper, paper.id) == paper
    await store.put(e.id, [revised], actor=e.owner_id)
    assert await store.history(e.id, Workpaper, paper.id) == [paper, revised]
    assert await store.latest(e.id, ProcedureRun) == [run]
    assert await store.latest(e.id, RunPopulation) == [population]
    assert await store.latest(e.id, Workpaper) == [revised]
    assert await store.get("ENG-OTHER", Workpaper, paper.id) is None


@pytest.mark.parametrize(
    ("v1", "change"),
    [
        (representation_letter(), {"status": RepresentationStatus.unreliable, "inconsistencies": ["Debtor list"]}),
        (compliance_check(), {"run_id": "RUN-FIN-10-2", "must_disclaim": True}),
        (governance_communication(), {"response": "Noted by the directors."}),
        (independence_declaration(), {"status": ReportStatus.signed, "report_id": "REPORT-1", "signed_by": "U-1"}),
    ],
    ids=["representation_letter", "compliance_check", "governance_communication", "independence_declaration"],
)
async def test_completion_records_versions_on_postgres(storage, v1: Versioned, change: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    """R215 验收 7: 四类产出各写 v1, v2 进 completion_records 表; 按版本 get, 最新 get 与 latest 都与写入的相等 (读回
    仍是各自的类, 枚举字段仍是枚举); 同表四类按 kind 分开, 别的项目读不到 (行级安全). 迁移的往返与授权见
    test_data_store_postgres 的新表用例."""
    engine, e, _, _ = storage
    store = PostgresAuditOutputStore(engine)
    kind = type(v1)
    v2 = v1.model_copy(update={"version": 2, **change})
    await store.put(e.id, [v1], actor="A9")
    await store.put(e.id, [v2], actor="A9")

    assert m.OUTPUTS[kind.__name__].name == "completion_records"
    assert await store.get(e.id, kind, v1.id, 1) == v1
    back = await store.get(e.id, kind, v1.id)
    assert back == v2 and type(back) is kind
    assert all(getattr(back, field) is value for field, value in change.items() if isinstance(value, StrEnum))
    assert await store.latest(e.id, kind) == [v2]
    kinds = (WrittenRepresentationLetter, ComplianceCheck, GovernanceCommunication, IndependenceDeclaration)
    assert [await store.latest(e.id, k) for k in kinds if k is not kind] == [[], [], []]
    assert await store.get("ENG-OTHER", kind, v1.id) is None


async def test_engagement_terms_round_trip_on_postgres(storage) -> None:
    """R198 验收 6: TERMS v1 写进 gate_materials 表 (kind 列区分, 不需迁移), 读回相等, 仍是 EngagementTerms (事实子
    模型与核对结果的枚举不丢); 同表的评分卡不混进按类取的结果."""
    engine, e, _, _ = storage
    store = PostgresAuditOutputStore(engine)
    terms = engagement_terms()
    scorecard = AcceptanceScorecard(id="SCORECARD", registry_checks=[], exception_ids=[], client_profile_version=1)
    await store.put(e.id, [terms, scorecard], actor="A2")

    assert m.OUTPUTS["EngagementTerms"].name == m.OUTPUTS["AcceptanceScorecard"].name == "gate_materials"
    back = await store.get(e.id, EngagementTerms, "TERMS")
    assert type(back) is EngagementTerms and back == terms and back.checks[0].status is TermsCheckStatus.passed
    assert await store.latest(e.id, EngagementTerms) == [terms]


async def test_audit_plan_versions_on_postgres(storage) -> None:  # type: ignore[no-untyped-def]
    """R203 验收 6: PLAN-1 v1 (draft), v2 (approved) 写进 gate_materials 表 (与评分卡等同表, kind 区分, 不需迁移);
    按版本 get 读回与写入的相等, 仍是 AuditPlan, 状态仍是枚举, 时间表日期仍是 date, 叙述的段序不变 (JSONB 不保留
    字典键序, 叙述用列表); 最新为 v2; 同表的其他种类读不到它, 别的项目也读不到 (行级安全)."""
    engine, e, _, _ = storage
    store = PostgresAuditOutputStore(engine)
    v1, v2 = audit_plan(), audit_plan(version=2, status=PlanStatus.approved)
    await store.put(e.id, [v1], actor="A4")
    await store.put(e.id, [v2], actor="A1")

    assert m.OUTPUTS["AuditPlan"].name == "gate_materials"
    assert await store.get(e.id, AuditPlan, "PLAN-1", 1) == v1
    back = await store.get(e.id, AuditPlan, "PLAN-1", 2)
    assert back == v2 and type(back) is AuditPlan and back.status is PlanStatus.approved
    assert isinstance(back.timetable[0].due, date) and back.approved_at == v2.approved_at
    # 叙述的段序经 JSONB 存取不变 (审核意见 R203-02: 段序由列表定, 不靠字典键序)
    assert [x.section for x in back.narrative] == ["overall_strategy", "scope", "significant_risks", "materiality"]
    assert await store.get(e.id, AuditPlan, "PLAN-1") == v2
    assert await store.history(e.id, AuditPlan, "PLAN-1") == [v1, v2]
    assert await store.latest(e.id, AuditPlan) == [v2]
    assert await store.latest(e.id, AcceptanceScorecard) == []
    assert await store.get("ENG-OTHER", AuditPlan, "PLAN-1") is None


async def test_evidence_and_adapters(storage, tmp_path):
    engine, e, _loader, evidence = storage
    pdf = ROOT / "data/demo/contracts/CON-001.pdf"
    doc = await evidence.save(
        e.id, pdf.name, pdf.read_bytes(), type=DocumentType.contract, reliability=Reliability.external_copy
    )
    assert (
        await evidence.save(
            e.id, "copy.pdf", pdf.read_bytes(), type=DocumentType.contract, reliability=Reliability.external_copy
        )
    ).id == doc.id
    assert len(await evidence.pages(e.id, doc.id)) == 3
    with pytest.raises(ValueError):
        await evidence.save(
            e.id, "../escape.pdf", pdf.read_bytes(), type=DocumentType.contract, reliability=Reliability.external_copy
        )
    with pytest.raises(FileNotFoundError):
        await evidence.path("other-project", doc.id)
    registry = MockRegistrySource(ROOT / "data/demo")
    assert (await registry.abn(e.id, "68 900 042 015")).status == "Active"
    assert await registry.abn(e.id, "missing") is None
    routing = RoutingDatasetLoader(CsvDatasetLoader(ROOT / "data", engine, evidence), {})
    by_dataset = DatasetRegistrySource(PostgresEngagementStore(engine), routing.supplement_dir)  # 按项目数据集 (demo)
    assert (await by_dataset.asic_company(e.id, "900042015")).name == "Numbat Electronic Components Pty Ltd"
    with pytest.raises(LookupError):
        await by_dataset.abn("ENG-missing", "68900042015")
    req = ConfirmationRequest(
        bank_account_id="BA-01", bank_name="Harbourline", bsb="999-101", account_no="12345601", as_of=date(2026, 6, 30)
    )
    channel = MockConfirmationChannel(ROOT / "data/demo", tmp_path / "receipts")
    first = await channel.submit(e.id, req, "key")
    assert first == await MockConfirmationChannel(ROOT / "data/demo", tmp_path / "receipts").submit(e.id, req, "key")
    with pytest.raises(ValueError):
        await channel.submit(e.id, req.model_copy(update={"account_no": "23456702"}), "key")
    routed = DatasetConfirmationChannel(PostgresEngagementStore(engine), routing.supplement_dir, tmp_path / "receipts")
    assert await routed.submit(e.id, req, "key") == first  # 同一回执目录与幂等键
    assert await PostgresUserStore(engine).get(e.owner_id)


# 文件类型的 7 个新取值各存一份 (Xero 补造目录的 PDF, 各不相同以免按哈希去重); 补造目录没有 governance_communication
# 类文件, 借一份 PDF 只核对类型往返
NEW_TYPES = {
    DocumentType.registry_extract: "registry/extracts/900043030.pdf",
    DocumentType.engagement_letter: "documents/engagement/engagement_letter.pdf",
    DocumentType.representation_letter: "representation/representation_letter.pdf",
    DocumentType.correspondence: "documents/evidence/diisr_email.pdf",
    DocumentType.governance_communication: "contracts/CON-X02.pdf",
    DocumentType.agreement: "documents/evidence/director_loan_agreement.pdf",
    DocumentType.internal_record: "documents/evidence/fixed_asset_register.pdf",
}


@pytest.mark.external
async def test_new_document_types_round_trip(storage):
    """documents.type 是 text 列 (迁移 0001 无约束, 0004 也未加), 新取值不需迁移: 各存一份后按类型取回, 读回仍是枚举;
    没补登记的文件读回文件日期为空, 关联对象为 [] (补登记见 test_data_store_postgres)."""
    engine, e, _loader, evidence = storage
    supplements = supplements_dir("20261017-q1")
    upstream = {
        DocumentType.contract,
        DocumentType.invoice,
        DocumentType.confirmation,
        DocumentType.bank_statement,
        DocumentType.minutes,
        DocumentType.financial_statements,
        DocumentType.other,
    }
    assert set(NEW_TYPES) == set(DocumentType) - upstream
    saved = {
        kind: await evidence.save(
            e.id, Path(path).name, (supplements / path).read_bytes(), type=kind, reliability=Reliability.external_copy
        )
        for kind, path in NEW_TYPES.items()
    }
    financial = PostgresFinancialDataStore(engine)
    for kind, doc in saved.items():
        [back] = await financial.documents(e.id, kind)
        assert back == doc and back.type is kind
    assert len(await financial.documents(e.id)) == len(NEW_TYPES)
    [loan] = await financial.documents(e.id, DocumentType.agreement)
    assert (loan.file_name, loan.page_count, loan.document_date, loan.related_ids) == (
        "director_loan_agreement.pdf",
        2,
        None,
        [],
    )


@pytest.mark.parametrize("checkpoint_backend", ["memory", "postgres"])
async def test_full_workflow_real_storage(storage, tmp_path, checkpoint_backend, monkeypatch):
    from contextlib import AsyncExitStack

    from backend.contracts.models import (
        AuditException,
        AuditReportVersion,
        GateVerdict,
        Judgment,
        Misstatement,
        MisstatementStatus,
        OpinionType,
        Phase,
        ReviewTask,
        ReviewWithdrawal,
        TaskStatus,
    )
    from backend.core.config import Settings
    from backend.main import build_deps
    from backend.orchestration.checkpoint import open_checkpointer
    from backend.orchestration.decisions import GraphDecisionSubmit
    from backend.orchestration.graph import compile_graph
    from backend.orchestration.nodes import GraphNodes
    from backend.orchestration.runner import GraphRunner
    from tests.fakes import NUMBAT_FEATURES
    from tests.test_decisions import acc01_decision, pln_decision
    from tests.test_flow import FIN_APPROVE, REVIEWS, RPT_APPROVE, RPT_REJECT

    _engine, e, _, _ = storage
    settings = Settings(
        DATABASE_URL=os.environ["TEST_DATABASE_URL"],
        EVIDENCE_ROOT=tmp_path,
        LLM_MODE="mock",
        LLM_FIXTURES_ROOT=ROOT / "data/demo/llm",
        CHECKPOINT_BACKEND=checkpoint_backend,
        FEATURES=",".join(sorted(NUMBAT_FEATURES)),  # Numbat 回归: 只开基线功能
    )
    deps = build_deps(settings)
    stack = AsyncExitStack()
    saver = await stack.enter_async_context(open_checkpointer(settings))
    graph = compile_graph(GraphNodes(deps, 4), saver)
    runner = GraphRunner(graph, deps, {"mode": "mock", "model": "mock"})
    decisions = GraphDecisionSubmit(graph, deps, runner)

    async def settle():
        for _ in range(1000):
            if runner.run_error(e.id):
                pytest.fail(str(runner.run_error(e.id)))
            snapshot = await graph.aget_state(runner.config(e.id))
            # Wait for the runner's background task, not just an intermediate checkpoint.
            if not runner.is_running(e.id):
                return snapshot
            await asyncio.sleep(0.02)
        pytest.fail("Workflow did not reach a decision point")

    try:
        # 种子路径: 建项目后先载入数据集 (P1 不再自动载入, 载入即视同确认数据已齐, 需求 R10)
        await deps.datasets.load(e.id, e.dataset)
        await runner.start(e.id)
        await settle()
        for decision in [
            acc01_decision(GateVerdict.approve, id="D-ACC"),
            pln_decision(GateVerdict.approve),
            *REVIEWS,
            FIN_APPROVE,
            RPT_REJECT,
            RPT_APPROVE,
        ]:
            await settle()
            field = "reviewed_by" if hasattr(decision, "reviewed_by") else "decided_by"
            await decisions.submit(e.id, decision.model_copy(update={field: e.owner_id}))
            if checkpoint_backend == "postgres" and decision.id == "D-REV02-0612":
                await settle()

                # 撤回已提交到真实库, 写检查点失败后重建连接与 runner; 恢复须补回撤回, 不能把旧错报再次累计.
                async def lost_checkpoint(*args, **kwargs):
                    raise OSError("写检查点前退出")

                with monkeypatch.context() as patch:
                    patch.setattr(graph, "aupdate_state", lost_checkpoint)
                    with pytest.raises(OSError, match="写检查点前退出"):
                        await decisions.withdraw_review(e.id, "WD-1", decision.id, reason=None, withdrawn_by=e.owner_id)
                withdrawal = await deps.output_store.get(e.id, ReviewWithdrawal, "WD-1")
                assert (withdrawal.record_id, withdrawal.reason) == (decision.id, None)
                assert await deps.output_store.get("ENG-OTHER", ReviewWithdrawal, "WD-1") is None
                retired = await deps.output_store.get(e.id, Misstatement, f"M-{decision.id}")
                assert retired.status is MisstatementStatus.withdrawn
                assert [m.status for m in await deps.output_store.history(e.id, Misstatement, retired.id)] == [
                    MisstatementStatus.uncorrected,
                    MisstatementStatus.withdrawn,
                ]
                await runner.shutdown()
                await deps.financial_store.engine.dispose()
                await stack.aclose()
                deps = build_deps(settings)
                saver = await stack.enter_async_context(open_checkpointer(settings))
                graph = compile_graph(GraphNodes(deps, 4), saver)
                runner = GraphRunner(graph, deps, {"mode": "mock", "model": "mock"})
                decisions = GraphDecisionSubmit(graph, deps, runner)
                await runner.resume(e.id)
                snapshot = await settle()
                assert snapshot.values["review_withdrawals"] == [withdrawal]
                assert snapshot.values["misstatements"][0].status is MisstatementStatus.withdrawn
                task = await deps.output_store.get(e.id, ReviewTask, decision.task_id)
                assert task.status is TaskStatus.pending
                await decisions.submit(
                    e.id,
                    decision.model_copy(update={"id": "D-REDECIDE", "target_version": task.target_version}),
                )
        await settle()
        final = await deps.engagement_store.get(e.id)
        assert final.phase == Phase.Archived and final.locked
        # 检查点读回的顶层枚举字段 (checkpoint.TOP_LEVEL_ENUMS) 是枚举而不是字符串; Postgres 由 AuditPostgresSaver 转回
        values = (await graph.aget_state(runner.config(e.id))).values
        assert values["phase"] is Phase.Archived and values["opinion_route"] is OpinionType.qualified
        assert len(await deps.output_store.latest(e.id, AuditException)) == 4
        assert len(await deps.output_store.latest(e.id, Judgment)) == 6
        assert all(t.status == TaskStatus.decided for t in await deps.output_store.latest(e.id, ReviewTask))
        misstatements = await deps.output_store.latest(e.id, Misstatement)
        assert [m.amount_cents for m in misstatements if m.status is not MisstatementStatus.withdrawn] == [14200000]
        if checkpoint_backend == "postgres":
            assert [m.id for m in misstatements if m.status is MisstatementStatus.withdrawn] == ["M-D-REV02-0612"]
            assert values["uncorrected_schedule"].total_cents == 14200000
        assert len(await deps.output_store.latest(e.id, AuditReportVersion)) == 2
        assert len(await deps.financial_store.documents(e.id)) == 5
    finally:
        await runner.shutdown()
        await deps.financial_store.engine.dispose()
        await stack.aclose()


async def test_rule_checks_on_postgres(storage):
    """需求 R130: 执行前检查在真实 Postgres 上按约定的写法执行 (接口文档第 8.1 节): 只看参数的检查不碰表, 参数为
    None 与布尔值用 CAST 绑定; 看数据的检查查表; 理由取 reason 列 (字面量或参数). 命中的规则不执行, 没命中的照常出例外.
    防约定的 SQL 写法在 psycopg 下绑定不了 (None 推不出类型)."""
    import yaml

    from backend.contracts.knowledge import ProcedureTemplate
    from backend.contracts.models import ProcedureRun, RuleOutcome
    from backend.services.rule_engine import SqlRuleEngine

    engine, e, loader, _evidence = storage
    await loader.load(e.id, "demo")
    template = ProcedureTemplate.model_validate(
        yaml.safe_load(
            """
id: T
name: 执行前检查连库
phase: P2_Executing
cycle: Journals
owner: A6
engine: rule
autonomy_level: L3
reads: [journal_entries]
rules:
  - id: R01
    name: 录入时间不可靠时跳过
    severity: high
    checks:
      - status: skipped
        sql: SELECT CAST(:note AS text) AS reason WHERE NOT CAST(:reliable AS boolean)
    sql: SELECT 'journal_entry' AS source_type, MIN(id) AS source_id FROM journal_entries ORDER BY 1
  - id: R03
    name: 没有审批限额时不可执行
    severity: high
    checks:
      - status: not_executable
        sql: SELECT '客户画像没有审批限额' AS reason WHERE CAST(:limit_cents AS bigint) IS NULL
    sql: SELECT 'journal_entry' AS source_type, MIN(id) AS source_id FROM journal_entries ORDER BY 1
  - id: R04
    name: 审批人全空时不可执行
    severity: high
    checks:
      - status: not_executable
        sql: >-
          SELECT '审批人数据缺失' AS reason
          WHERE NOT EXISTS (SELECT 1 FROM journal_entries WHERE approved_by IS NOT NULL)
    sql: SELECT 'journal_entry' AS source_type, MIN(id) AS source_id FROM journal_entries ORDER BY 1
"""
        )
    )
    run = ProcedureRun(
        id="RUN-T-1",
        procedure_id="T",
        run_no=1,
        trigger="initial",
        params_snapshot={},
        status="running",
        started_at=now(),
    )
    params = {"note": "录入时间不可靠", "reliable": False, "limit_cents": None}
    found = await SqlRuleEngine(PostgresFinancialDataStore(engine)).run(e.id, template, run, params)
    assert found.outcomes == [
        RuleOutcome(rule_id="R01", status="skipped", reason="录入时间不可靠"),
        RuleOutcome(rule_id="R03", status="not_executable", reason="客户画像没有审批限额"),
    ]
    assert [x.rule_id for x in found.exceptions] == ["R04"]  # Numbat 的分录有审批人, R04 照常执行
    again = await SqlRuleEngine(PostgresFinancialDataStore(engine)).run(
        e.id, template, run, params | {"reliable": True, "limit_cents": 500000}
    )
    assert (again.outcomes, [x.rule_id for x in again.exceptions]) == ([], ["R01", "R03", "R04"])


async def test_rule_edge_cases(storage):
    import yaml
    from sqlalchemy import delete, update

    from backend.contracts.knowledge import ProcedureTemplate
    from backend.contracts.models import ProcedureRun
    from backend.services.rule_engine import SqlRuleEngine

    engine, e, loader, _evidence = storage
    await loader.load(e.id, "demo")
    financial = PostgresFinancialDataStore(engine)
    templates = {
        name: ProcedureTemplate.model_validate(
            yaml.safe_load((ROOT / f"src/backend/knowledge/procedures/{name}.yaml").read_text())
        )
        for name in ["REV-08", "CSH-05", "PLN-07"]
    }

    def sql(name, rule):
        return next(r.sql for r in templates[name].rules if r.id == rule)

    params = dict(period_end="2026-06-30", window_days=20)
    async with transaction(engine, e.id) as conn:
        await conn.execute(
            delete(m.FINANCIAL["delivery_notes"]).where(m.FINANCIAL["delivery_notes"].c.invoice_id == "SI-2026-0618")
        )
        for index, amount in enumerate([4000000, 5000000]):
            await conn.execute(
                insert(m.FINANCIAL["bank_transactions"]).values(
                    engagement_id=e.id,
                    id=f"BT-PARTIAL-{index}",
                    bank_account_id="BA-01",
                    txn_date=date(2026, 7, 3),
                    amount_cents=amount,
                    balance_after_cents=0,
                    description="partial receipt",
                    reference="SI-2026-0618",
                )
            )
    assert [r["source_id"] for r in await financial.query(e.id, sql("REV-08", "R02"), params)] == ["SI-2026-0618"]
    result = await financial.query(e.id, sql("REV-08", "R03"), params)
    assert len(result) == 1 and result[0]["amount_cents"] == 500000
    # Exercise exception IDs, evidence mapping and JSON conversion against actual SQL output.
    run = ProcedureRun(
        id="RUN-REV-08-2",
        procedure_id="REV-08",
        run_no=2,
        trigger="rerun",
        params_snapshot=params,
        status="running",
        started_at=now(),
    )
    findings = await SqlRuleEngine(financial).run(e.id, templates["REV-08"], run, params)
    assert [x.id for x in findings.exceptions] == ["X-REV-08-2-R01-1", "X-REV-08-2-R02-1", "X-REV-08-2-R03-1"]
    assert all(
        x.evidence[0].type == "invoice" and x.evidence[0].reliability == "internal_original"
        for x in findings.exceptions
    )
    async with transaction(engine, e.id) as conn:
        bank = m.FINANCIAL["bank_transactions"]
        await conn.execute(delete(bank).where(bank.c.bank_account_id == "BA-02"))
        tb = m.FINANCIAL["trial_balance"]
        await conn.execute(
            update(tb).where(tb.c.period == "prior", tb.c.account_code == "1100").values(balance_cents=0)
        )
    cash = await financial.query(e.id, sql("CSH-05", "B01"), params)
    assert next(r for r in cash if r["source_id"] == "BA-02")["amount_cents"] is None
    pln07 = dict(
        change_pct=0.2,
        min_change_revenue_pct=0.005,
        revenue_categories=["revenue"],
        period_start="2025-07-01",
        period_end="2026-06-30",
        prior_period_start=None,
    )
    analytics = await financial.query(e.id, sql("PLN-07", "A01"), pln07)
    assert analytics[0]["source_id"] == "1100" and analytics[0]["change_pct"] is None


XERO_SNAPSHOT = "xero/20261017-mock"  # 本地 xero pull --paid mock 的快照, 不入库; 没有时跳过


async def test_csh02_d03_d04_drop_voided_pairs(storage):
    """CSH-02 D03 与 D04 剔除作废对 (需求 R105 验收 4): 不插行时 D03, D04 的行与 tests.fakes 由 CSV 派生的
    NUMBAT_D03_ROWS (6 行), NUMBAT_D04_ROWS (74 行) 整行相同, 给不给作废对的格式都一样 (Numbat 没有冲回凭证); D02
    与派生的 NUMBAT_D02_ROWS (8 行) 整行相同 (R107: 派生函数与 Xero 共用). 插入
    VX-1 (06-29, 1010 借 700000 / 4200 贷 700000) 与镜像的冲回凭证 VX-2 (只有 4200 一行的说明为 "Reversal of ID 1",
    1010 一行的说明不匹配: 冲回凭证按凭证号整张剔除, 不按行), 以及原凭证不存在的冲回凭证 VX-3 后: 给格式
    "Reversal of ID (\\d+)" 与模板 "VX-{1}" 时两条规则都不含这些行, 其余行与插行前整行相同; 不给 (为空) 时都含 (D03
    只有银行科目 1010 的两行). 两条规则的 WITH 段写得不一致即失败."""
    import yaml

    from backend.contracts.knowledge import ProcedureTemplate
    from tests.fakes import NUMBAT_D02_ROWS, NUMBAT_D03_ROWS, NUMBAT_D04_ROWS

    engine, e, loader, _evidence = storage
    await loader.load(e.id, "demo")
    financial = PostgresFinancialDataStore(engine)
    template = ProcedureTemplate.model_validate(
        yaml.safe_load((ROOT / "src/backend/knowledge/procedures/CSH-02.yaml").read_text())
    )
    sql = {r.id: r.sql for r in template.rules}
    plain = dict(
        period_end="2026-06-30", lookback_days=5, reversal_reference_pattern=None, reversal_entry_no_template=None
    )
    voids = {**plain, "reversal_reference_pattern": r"Reversal of ID (\d+)", "reversal_entry_no_template": "VX-{1}"}

    async def ids(rule: str, params: dict[str, object]) -> list[str]:
        return [r["id"] for r in await financial.query(e.id, sql[rule], params)]

    assert await financial.query(e.id, sql["D02"], dict(period_end="2026-06-30", subsequent_days=10)) == NUMBAT_D02_ROWS
    expected = {"D03": NUMBAT_D03_ROWS, "D04": NUMBAT_D04_ROWS}
    for rule in ("D03", "D04"):
        for params in (plain, voids):
            assert await financial.query(e.id, sql[rule], params) == expected[rule], rule

    journal = m.FINANCIAL["journal_entries"]
    lines = [
        ("VX-1-1", "VX-1", "1010", 700000, 0, "Accrued interest"),
        ("VX-1-2", "VX-1", "4200", 0, 700000, "Accrued interest"),
        ("VX-2-1", "VX-2", "4200", 700000, 0, "Reversal of ID 1"),
        # 同一张冲回凭证中说明不匹配的行 (在银行科目上, D03, D04 都覆盖): 随凭证一起剔除
        ("VX-2-2", "VX-2", "1010", 0, 700000, "Accrued interest reversal"),
        # 说明匹配格式, 而模板拼出的原凭证 (VX-9) 不存在: 只剔除这张冲回凭证本身
        ("VX-3-1", "VX-3", "4200", 100, 0, "Reversal of ID 9"),
    ]
    async with transaction(engine, e.id) as conn:
        for line_id, entry_no, account, debit, credit, description in lines:
            await conn.execute(
                insert(journal).values(
                    engagement_id=e.id,
                    id=line_id,
                    entry_no=entry_no,
                    posting_date=date(2026, 6, 29),
                    created_at=None,
                    created_by=None,
                    approved_by=None,
                    account_code=account,
                    debit_cents=debit,
                    credit_cents=credit,
                    description=description,
                    source_doc=None,
                    is_adjustment=False,
                )
            )
    inserted = {line[0] for line in lines}
    assert set(await ids("D03", plain)) & inserted == {"VX-1-1", "VX-2-2"}
    assert set(await ids("D04", plain)) & inserted == inserted
    for rule in ("D03", "D04"):
        assert await financial.query(e.id, sql[rule], voids) == expected[rule], rule


async def test_rev08_r03_matches_invoice_number_digits_as_a_whole_word(storage):
    """REV-08 R03: 发票号为字母前缀加纯数字 (ORC1026) 时, 参考号里出现该数字串且前后都不是数字的流水也算它的收款
    ("1026", "Invoice 1026"); "1028", "10261", "21026" 不算; 参考号含发票号的照原条件算. 带连字符的发票号
    (Numbat 的 SI-2026-0612) 只按包含发票号, 参考号 "0612" 不算. Numbat 临时项目内插入行, 期末 2026-06-30, 窗口 20."""
    import yaml

    from backend.contracts.knowledge import ProcedureTemplate

    engine, e, loader, _evidence = storage
    await loader.load(e.id, "demo")
    financial = PostgresFinancialDataStore(engine)
    template = ProcedureTemplate.model_validate(
        yaml.safe_load((ROOT / "src/backend/knowledge/procedures/REV-08.yaml").read_text())
    )
    r03 = next(r.sql for r in template.rules if r.id == "R03")
    params = dict(period_end="2026-06-30", window_days=20)
    invoices, bank = m.FINANCIAL["sales_invoices"], m.FINANCIAL["bank_transactions"]

    async def receipts(*rows: tuple[str, str, int]) -> None:
        async with transaction(engine, e.id) as conn:
            for txn_id, reference, amount in rows:
                await conn.execute(
                    insert(bank).values(
                        engagement_id=e.id,
                        id=txn_id,
                        bank_account_id="BA-01",
                        txn_date=date(2026, 7, 2),
                        amount_cents=amount,
                        balance_after_cents=0,
                        description="receipt",
                        reference=reference,
                    )
                )

    async def hits() -> dict[str, tuple[int, int, str]]:
        rows = await financial.query(e.id, r03, params)
        return {r["source_id"]: (r["amount_cents"], r["received_cents"], r["bank_transaction_ids"]) for r in rows}

    assert await hits() == {}
    async with transaction(engine, e.id) as conn:
        await conn.execute(
            insert(invoices).values(
                engagement_id=e.id,
                id="SI-ORC1026",
                invoice_no="ORC1026",
                customer="Test Customer Pty Ltd",
                customer_abn=None,
                contract_id=None,
                invoice_date=date(2026, 6, 25),
                posting_date=date(2026, 6, 25),
                amount_cents=715000,
                journal_entry_id=None,
            )
        )
    await receipts(("BT-1026", "1026", 385000))
    assert await hits() == {"SI-ORC1026": (330000, 385000, "BT-1026")}

    await receipts(("BT-X-1", "1028", 100000), ("BT-X-2", "10261", 100000), ("BT-X-3", "21026", 100000))
    await receipts(("BT-0612", "0612", 100000))  # SI-2026-0612 带连字符, 只按包含发票号
    assert await hits() == {"SI-ORC1026": (330000, 385000, "BT-1026")}

    await receipts(("BT-INV-1026", "Invoice 1026", 100000), ("BT-ORC1026", "ORC1026", 100000))
    assert await hits() == {"SI-ORC1026": (130000, 585000, "BT-1026,BT-INV-1026,BT-ORC1026")}


async def test_pln07_a01_compares_balance_sheet_with_opening(storage):
    """PLN-07 A01: 资产负债类本期末对期初 (上期损益结转进留存收益), 损益类只在两期等长时对上期; 门槛为本期收入的
    0.5% 且不小于基数绝对值的 20%. 参数由程序库按项目与缺省数据集声明解析. Numbat 只命中 1100; 在临时项目里改行
    (或删行) 看结转, 等长 (含闰年), 门槛, 只在一期出现的科目与变动为 0 几处的区分力."""
    from sqlalchemy import delete, update

    from backend.contracts.records import DatasetTraits
    from backend.contracts.state import initial_state

    engine, e, loader, _evidence = storage
    await loader.load(e.id, "demo")
    financial = PostgresFinancialDataStore(engine)
    library = ProcedureLibrary(ROOT / "src/backend/knowledge")
    template = library.load("PLN-07")
    a01 = next(r.sql for r in template.rules if r.id == "A01")
    params = library.resolve_params(template, e, initial_state(e, [], 1), None, traits=DatasetTraits(), profile=None)
    assert (params["period_start"], params["period_end"], params["prior_period_start"]) == (
        date(2025, 7, 1),
        date(2026, 6, 30),
        None,
    )
    tb = m.FINANCIAL["trial_balance"]

    async def hits(**override: object) -> dict[str, dict[str, object]]:
        return {r["source_id"]: r for r in await financial.query(e.id, a01, {**params, **override})}

    async def balance(period: str, code: str, cents: int | None) -> None:
        """改一行试算表余额; None 删掉这一行 (该科目这一期没有)."""
        async with transaction(engine, e.id) as conn:
            where = (tb.c.engagement_id == e.id, tb.c.period == period, tb.c.account_code == code)
            if cents is None:
                await conn.execute(delete(tb).where(*where))
            else:
                await conn.execute(update(tb).where(*where).values(balance_cents=cents))

    found = await hits()
    assert list(found) == ["1100"]  # 8100 变动 6780000 过收入门槛 6000000, 但不足基数的 20% (7644000)
    row = found["1100"]
    assert (row["amount_cents"], row["current_cents"], row["prior_cents"], row["opening_cents"], row["basis"]) == (
        52500000,
        202500000,
        150000000,
        150000000,
        "balance_sheet",
    )
    assert float(row["change_pct"]) == 0.35 and row["account_name"] == "Trade receivables"

    # 结转: 本期留存收益恰为上期 (-326920000) 加上期损益 (-89180000) 时没有变动, 不命中. 比例门槛与变动率按期初
    # -416100000, 不按上期表原值: 对期初变动 +70000000 不足期初的 20% (却超过上期原值的 20%), 不命中; +100000000 命中
    await balance("current", "3200", -416100000)
    assert "3200" not in await hits()
    await balance("current", "3200", -346100000)
    assert "3200" not in await hits()
    await balance("current", "3200", -316100000)
    row = (await hits())["3200"]
    assert (row["amount_cents"], row["prior_cents"], row["opening_cents"], row["basis"]) == (
        100000000,
        -326920000,
        -416100000,
        "balance_sheet",
    )
    assert round(float(row["change_pct"]), 4) == 0.2403  # 100000000 / 416100000
    await balance("current", "3200", -376100000)

    # 等长: 本期工资改为 200000000 (变动 95000000); 上期起始日为空或 2024-07-01 时命中, 上期只有 3 个月时不比
    await balance("current", "6100", 200000000)
    for prior_start, expected in ((None, True), (date(2024, 7, 1), True), (date(2025, 4, 1), False)):
        result = await hits(prior_period_start=prior_start)
        assert ("6100" in result) is expected, prior_start
        if expected:
            assert (result["6100"]["basis"], result["6100"]["opening_cents"]) == ("profit_or_loss", 105000000)
    # 闰年: 本期 2024-07-01 至 2025-06-30 (365 天), 上期从 2023-07-01 起 (366 天, 含 2024-02-29), 整月数相等, 要比;
    # 按天数判定会把它当作不等长
    leap = {"period_start": date(2024, 7, 1), "period_end": date(2025, 6, 30), "prior_period_start": date(2023, 7, 1)}
    assert "6100" in await hits(**leap)
    await balance("current", "6100", 118000000)

    # 门槛: 期初为 0 的预付款, 变动 5999999 不到本期收入的 0.5% (6000000), 6000000 命中, 变动率为空
    await balance("prior", "1300", 0)
    await balance("current", "1300", 5999999)
    assert "1300" not in await hits()
    await balance("current", "1300", 6000000)
    row = (await hits())["1300"]
    assert (row["amount_cents"], row["opening_cents"], row["change_pct"]) == (6000000, 0, None)

    # 只在一期出现的科目也比: 上期没有 1300 这一行, 本期 6000000 照样命中 (上期为空, 期初 0, 变动率为空); 本期没有
    # 1500 这一行, 变动为上期余额取负
    await balance("prior", "1300", None)
    row = (await hits())["1300"]
    assert (row["amount_cents"], row["prior_cents"], row["opening_cents"], row["change_pct"]) == (
        6000000,
        None,
        0,
        None,
    )
    await balance("current", "1500", None)
    row = (await hits())["1500"]
    assert (row["current_cents"], row["amount_cents"], row["opening_cents"]) == (None, -235000000, 235000000)

    # 变动为 0 的不出: 收入门槛取 0 时, 两期都为 0 的科目 (1300 上期没有, 本期改为 0) 也不出行
    await balance("current", "1300", 0)
    assert "1300" not in await hits(min_change_revenue_pct=0)


async def test_pln07_a03_monthly_figures_from_journal(storage):
    """PLN-07 A03 (需求 R103 验收 2): data/demo 的总账在审计期间内按月汇总, 得 12 行, 同 NUMBAT_A03_ROWS (各列合计
    同本期试算表: 收入取 revenue 分类, 费用取类型 expense, 员工费用取 employee_expenses). 入账日期不在审计期间内的
    分录不计: 期间缩到 2025-08-01 至 2026-05-31 时首尾两个月不出, 其余月份不变; 期间从月中起止时首尾两个月只计期间
    内的. 收入与员工费用的分类取参数. 审计
    期间的每个月都出一行: 期间多出没有分录的 2025-06, 或删掉 2026-02 的全部分录时, 那个月三项为 0 (第 1 轮审核
    R103-01)."""
    from sqlalchemy import delete

    from backend.contracts.records import DatasetTraits
    from backend.contracts.state import initial_state
    from tests.fakes import NUMBAT_A03_ROWS

    engine, e, loader, _evidence = storage
    await loader.load(e.id, "demo")
    financial = PostgresFinancialDataStore(engine)
    library = ProcedureLibrary(ROOT / "src/backend/knowledge")
    template = library.load("PLN-07")
    a03 = next(r.sql for r in template.rules if r.id == "A03")
    params = library.resolve_params(template, e, initial_state(e, [], 1), None, traits=DatasetTraits(), profile=None)
    assert params["employee_expense_categories"] == ["employee_expenses"]

    rows = [dict(r) for r in await financial.query(e.id, a03, params)]
    assert rows == NUMBAT_A03_ROWS
    shorter = {**params, "period_start": date(2025, 8, 1), "period_end": date(2026, 5, 31)}
    assert [dict(r) for r in await financial.query(e.id, a03, shorter)] == NUMBAT_A03_ROWS[1:-1]
    # 期间首尾不在月初月末时, 首尾两个月只计期间内的分录 (7 月 16 日之前, 6 月 15 日之后的不计)
    inside = {**params, "period_start": date(2025, 7, 16), "period_end": date(2026, 6, 15)}
    partial = [dict(r) for r in await financial.query(e.id, a03, inside)]
    assert [r["month"] for r in partial] == [r["month"] for r in NUMBAT_A03_ROWS]
    assert partial[1:-1] == NUMBAT_A03_ROWS[1:-1]
    assert partial[0]["revenue_cents"] < NUMBAT_A03_ROWS[0]["revenue_cents"]
    assert partial[-1]["expense_cents"] < NUMBAT_A03_ROWS[-1]["expense_cents"]
    # 员工费用按参数取分类: 不给分类时这一列都为 0, 其余列不变
    none = [dict(r) for r in await financial.query(e.id, a03, {**params, "employee_expense_categories": []})]
    assert none == [{**row, "employee_expense_cents": 0} for row in NUMBAT_A03_ROWS]
    # 收入按参数取分类: 加上 other_income 时每月并入 4200 储蓄利息 (合成数据集设计第 4.4 节), 其余两列不变
    interest = [115000, 116000, 116000, 117000, 116000, 117000, 117000, 116000, 117000, 116000, 118000, 119000]
    wider = {**params, "revenue_categories": ["revenue", "other_income"]}
    assert [dict(r) for r in await financial.query(e.id, a03, wider)] == [
        {**row, "revenue_cents": row["revenue_cents"] + cents}
        for row, cents in zip(NUMBAT_A03_ROWS, interest, strict=True)
    ]

    # 没有分录的月份也出一行, 三项为 0
    empty = {"revenue_cents": 0, "expense_cents": 0, "employee_expense_cents": 0}
    longer = [dict(r) for r in await financial.query(e.id, a03, {**params, "period_start": date(2025, 6, 1)})]
    assert longer == [{"month": date(2025, 6, 1), **empty}, *NUMBAT_A03_ROWS]
    journal = m.FINANCIAL["journal_entries"]
    async with transaction(engine, e.id) as conn:
        february = (journal.c.posting_date >= date(2026, 2, 1)) & (journal.c.posting_date <= date(2026, 2, 28))
        await conn.execute(delete(journal).where(journal.c.engagement_id == e.id, february))
    rows = [dict(r) for r in await financial.query(e.id, a03, params)]
    assert rows == [{**row, **empty} if row["month"] == date(2026, 2, 1) else row for row in NUMBAT_A03_ROWS]


@pytest.mark.external
async def test_xero_snapshot_loads_and_ties_to_trial_balance(tmp_path):
    """Xero 快照经 XeroDatasetLoader 载入真实库: 各表有数, 科目带标准分类,
    生产的 gl_balance 对每个科目等于本期试算表."""
    snapshot = snapshot_dir(XERO_SNAPSHOT.removeprefix("xero/"))  # 先核对: 跟踪了却找不到时失败
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="Xero", password_hash="x", created_at=now()
            )
        )
    loader = RoutingDatasetLoader(
        CsvDatasetLoader(ROOT / "data", engine, FileEvidenceStore(tmp_path, engine)),
        {
            "xero/": XeroDatasetLoader(
                snapshot.parent, ROOT / "src/backend/knowledge/planning", engine, FileEvidenceStore(tmp_path, engine)
            )
        },
    )
    profile = loader.client_profile(XERO_SNAPSHOT).model_copy(update={"id": "CLIENT-" + uuid4().hex})
    e = await PostgresEngagementStore(engine).create(
        owner_id=user,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 11, 30),
        dataset=XERO_SNAPSHOT,
        profile=profile,
    )
    await loader.load(e.id, XERO_SNAPSHOT)
    financial = PostgresFinancialDataStore(engine)
    counts = {t: (await financial.query(e.id, f"SELECT count(*) AS n FROM {t}", {}))[0]["n"] for t in m.FINANCIAL}
    assert counts["accounts"] > 0 and counts["journal_entries"] > 0 and counts["delivery_notes"] == 0
    unmapped = await financial.query(e.id, "SELECT code FROM accounts WHERE category IS NULL", {})
    assert len(unmapped) <= 1  # Demo 只有中转科目认不出
    current = {r.account_code: r.balance_cents for r in await financial.trial_balance(e.id, "current")}
    diff = {
        code: (balance, gl)
        for code, balance in current.items()
        if balance != (gl := await financial.gl_balance(e.id, code, date(2026, 11, 30)))
    }
    assert diff == {}
    await engine.dispose()


async def test_llm_settings_upsert_per_slot(storage):
    """模型设置表 (迁移 0003): 按 slot 覆盖写入, 系统级不绑定项目; 应用角色可读写."""
    engine, *_ = storage
    store = PostgresLlmSettingsStore(engine)
    first = LlmSettings(
        slot="review",
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
        model="claude-fable-5-1",
        api_key_encrypted="gAAAA-test",
        key_last4="1234",
        updated_by="U-1",
        updated_at=now(),
    )
    assert await store.save(first) == first
    assert await store.get("review") == first
    second = first.model_copy(update={"model": "claude-opus-5-5", "api_key_encrypted": None, "key_last4": None})
    await store.save(second)
    assert await store.get("review") == second
    async with engine.begin() as conn:
        assert (await conn.execute(select(m.llm_settings.c.slot).where(m.llm_settings.c.slot == "review"))).all() == [
            ("review",)
        ]


async def test_user_store_create_keeps_existing_account():
    """注册: 登录名唯一, 同名并发注册只成功一个, 不改原账号; 测试结束删掉自己建的行."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    users = PostgresUserStore(engine)
    name = "register-" + uuid4().hex
    other = "case-" + uuid4().hex
    legacy = "Legacy-" + uuid4().hex + "@Example.com"
    try:
        results = await asyncio.gather(users.create(name, "First", "hash-1"), users.create(name, "Second", "hash-2"))
        created = [user for user in results if user is not None]
        assert len(created) == 1
        found = await users.by_email(name)
        assert found is not None
        user, password_hash = found
        assert user == created[0]
        assert password_hash == {"First": "hash-1", "Second": "hash-2"}[user.full_name]
        assert await users.get(user.id) == user
        assert await users.create(name, "Third", "hash-3") is None
        # 只差大小写的并发注册同样只成功一个, 查找也不区分大小写
        variants = await asyncio.gather(
            users.create(other, "Lower", "h-1"), users.create(other.upper(), "Upper", "h-2")
        )
        assert sum(user is not None for user in variants) == 1
        assert await users.by_email(other.upper()) is not None
        # 注册功能之前存入的含大写账号: 按小写能登录, 按小写也不能再注册
        async with engine.begin() as conn:
            await conn.execute(
                insert(m.users).values(
                    id="USR-" + uuid4().hex,
                    email=legacy,
                    full_name="Legacy",
                    password_hash="h-legacy",
                    created_at=now(),
                )
            )
        found = await users.by_email(legacy.lower())
        assert found is not None and found[1] == "h-legacy"
        assert await users.create(legacy.lower(), "Copy", "h-copy") is None
    finally:
        async with engine.begin() as conn:  # 只删本测试建的账号
            await conn.execute(delete(m.users).where(m.users.c.email.in_([name, other, other.upper(), legacy])))
        await engine.dispose()


@pytest.mark.external
async def test_xero_mock_and_q1_profiles_coexist(xero_storage):
    """R70: 同一库先后以 xero/20261017-mock 与 xero/20261017-q1 建项目都成功, 快照画像 CLIENT-XERO-3C1332BB 与
    补造画像 CLIENT-XERO-3C1332BB-Q1 并存; 再建一次 (画像已在库中, 内容相同) 也照常. 不写死版本号 (R127): 库里
    若存着补造画像改内容之前的版本, 建项目时会出新版本, 断言最新一版的内容等于补造画像, 项目采用最新一版."""
    snapshot_dir("20261017-mock")  # 另核对 mock 快照 (q1 的由 xero_storage 核对)
    x = xero_storage
    loader, store = x.xero, x.engagements
    periods = {"xero/20261017-mock": date(2026, 11, 30), XERO_Q1: date(2026, 9, 30)}
    created = []
    for _ in range(2):
        for dataset, end in periods.items():
            created.append(await x.engagement(dataset, period_end=end))
    mock = await store.client_profile("CLIENT-XERO-3C1332BB")
    q1 = await store.client_profile("CLIENT-XERO-3C1332BB-Q1")
    assert mock is not None and mock.legal_name == "Demo Company (AU)"
    assert q1 is not None and q1.legal_name == "Demo Company (AU) Pty Ltd" and q1.abn == "25900043021"
    supplied = loader.client_profile("xero/20261017-q1")
    assert q1.model_dump(exclude={"version"}) == supplied.model_dump(exclude={"version"})
    assert q1.approval_limit_cents == 500000  # 扩展字段 (R127) 写进了库
    adopted = {e.client_id: e.client_profile_version for e in created[2:]}  # 第二轮: 两份画像都已是最新一版
    assert adopted == {mock.id: mock.version, q1.id: q1.version}


async def test_create_adopts_latest_profile_or_writes_a_new_version():
    """R127: 建项目时数据集的画像与该客户的各版比较内容 (不看版本号): 有一版相同就沿用最新一版 (含审计师改过的),
    都不同才写新一版 (版本 +1); 旧版本不改. R288 (审核 R288-02): 两处写入 (首版与数据集改了的新版) 都记来源 dataset
    与建项目的人, 审计师那一版记 update 与审计师."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user, client = "USR-" + uuid4().hex, "CLIENT-R127-" + uuid4().hex[:8]
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="R127", password_hash="x", created_at=now()
            )
        )
    store = PostgresEngagementStore(engine)
    base = ClientProfile(
        id=client,
        legal_name="R127 Pty Ltd",
        abn="12345678901",
        entity_type="small_proprietary",
        industry_anzsic="7000",
        listed=False,
        directors=["A"],
    )

    async def create(profile: ClientProfile) -> int:
        engagement = await store.create(
            owner_id=user, period_start=date(2026, 7, 1), period_end=date(2026, 9, 30), dataset="demo", profile=profile
        )
        return engagement.client_profile_version

    async def latest() -> ClientProfile:
        profile = await store.client_profile(client)
        assert profile is not None
        return profile

    assert await create(base) == 1
    assert await create(base) == 1  # 内容相同, 沿用
    edited = base.model_copy(update={"version": 2, "directors": ["A", "B"]})
    await store.save_profile(
        edited, source=ProfileSource.update, updated_by="U-1"
    )  # 审计师在项目的 ACC-01 改了画像 (第 2 版)
    assert await create(base) == 2  # 数据集没变 (同第 1 版): 沿用最新一版, 不把数据集原样盖回去
    assert (await latest()).version == 2 and (await latest()).directors == ["A", "B"]
    changed = base.model_copy(update={"approval_limit_cents": 500000})
    assert await create(changed) == 3  # 数据集本身改了 (与各版都不同), 写第 3 版
    assert await create(changed) == 3
    assert await create(base) == 3  # 与第 1 版相同, 沿用最新一版, 不再多写
    assert (await latest()).version == 3
    first, second = await store.client_profile(client, 1), await store.client_profile(client, 2)
    assert first is not None and first.approval_limit_cents is None and first.directors == ["A"]  # 旧版本不改
    assert second == edited
    history = await store.profile_history(client)
    assert [(r.profile.version, r.source, r.updated_by) for r in history] == [
        (1, ProfileSource.dataset, user),
        (2, ProfileSource.update, "U-1"),
        (3, ProfileSource.dataset, user),
    ]
    await engine.dispose()


@pytest.mark.external
async def test_xero_q1_seed_load_is_snapshot_only(tmp_path, xero_storage):
    """R269 验收 11: 真实库载入 xero/20261017-q1 只载快照: 服务完成记录与工资册为 0 行, 发票的合同关联为空, 开户行取
    快照的科目名, 不登记文件 (补造部分由演示数据导入写入, 见 test_demo_import_db); 每个科目 gl_balance 等于本期试算表.
    补造与快照不匹配时不载入."""
    x = xero_storage
    engine, api, supplements, loader = x.engine, x.api, x.supplements, x.xero
    e = await x.engagement()
    await loader.load(e.id, "xero/20261017-q1")
    financial = PostgresFinancialDataStore(engine)
    counts = {t: (await financial.query(e.id, f"SELECT count(*) AS n FROM {t}", {}))[0]["n"] for t in m.FINANCIAL}
    assert counts == {
        "accounts": 62,
        "trial_balance": 33,
        "journal_entries": 1017,
        "sales_invoices": 42,
        "supplier_bills": 6,  # 期末仍未付的应付账单 (需求 R178)
        "delivery_notes": 0,
        "bank_accounts": 2,
        "bank_transactions": 146,
        "employees": 0,  # 工资册不随数据集载入 (需求 R96)
    }
    bills = await financial.query(e.id, SELECT_BILLS, {})
    assert [SupplierBill.model_validate(b) for b in bills] == Q1_BILLS  # 数据集载入同样写入 (需求 R178 验收 6)
    assert await financial.record(e.id, "supplier_bill", Q1_BILLS[1].id) == Q1_BILLS[1].model_dump()
    current = {r.account_code: r.balance_cents for r in await financial.trial_balance(e.id, "current")}
    assert {c: v for c, v in current.items() if v != await financial.gl_balance(e.id, c, date(2026, 9, 30))} == {}
    assert await financial.documents(e.id) == [] and await financial.contracts(e.id) == []
    linked = await financial.query(e.id, "SELECT count(*) AS n FROM sales_invoices WHERE contract_id IS NOT NULL", {})
    assert linked[0]["n"] == 0
    snapshot = loader.merged("xero/20261017-q1", None)  # 快照映射出的八张表, 不并补造表
    banks = await financial.query(e.id, "SELECT id, bank_name FROM bank_accounts ORDER BY id", {})
    assert [(b["id"], b["bank_name"]) for b in banks] == sorted((b.id, b.bank_name) for b in snapshot["bank_accounts"])
    # 补造与快照不匹配时不载入: 各表, 载入标记与证据文件都没有写入
    demo = tmp_path / "xero_demo"
    shutil.copytree(supplements / "20261017-q1", demo / "20261017-q1")
    links = demo / "20261017-q1" / "contract_links.csv"
    links.write_bytes(links.read_bytes() + b" ")
    planning = ROOT / "src/backend/knowledge/planning"
    broken = XeroDatasetLoader(api, planning, engine, FileEvidenceStore(tmp_path / "ev", engine), supplements=demo)
    other = await x.engagement()
    with pytest.raises(SupplementMismatch, match=r"contract_links\.csv"):
        await broken.load(other.id, "xero/20261017-q1")
    for t in m.FINANCIAL:
        assert (await financial.query(other.id, f"SELECT count(*) AS n FROM {t}", {}))[0]["n"] == 0
    assert await financial.documents(other.id) == []
    assert not (tmp_path / "ev").exists() or not any((tmp_path / "ev").rglob("*.pdf"))


@pytest.mark.external
async def test_load_adjustments_two_rounds(tmp_path, xero_storage):
    """R73: 真实库载入 xero/20261017-q1 后按两轮载入更正分录: 行数, data_version, 幂等, gl_balance, 试算表不变;
    尚未载入, 数据集不同, 同 ID 内容不同, 项目锁定都拒绝且不写. R271: 每轮有新行时总账整表换一版 (版本行的来源,
    原文件名, 行数与写入人), 两轮之后体检的借贷平衡, 分录与试算表, 行数与来源都通过, 计入更正分录时分录与试算表不符."""
    x = xero_storage
    engine, user, evidence, xero, store = x.engine, x.user, x.evidence, x.xero, x.engagements
    api, supplements, engagement = x.api, x.supplements, x.engagement
    financial = PostgresFinancialDataStore(engine)

    async def state(eid):
        journal = (await financial.query(eid, "SELECT count(*) AS n FROM journal_entries", {}))[0]["n"]
        version = (await financial.query(eid, "SELECT data_version FROM dataset_loads", {}))[0]["data_version"]
        return journal, version

    e = await engagement()
    with pytest.raises(ValueError, match="尚未载入数据集"):
        await xero.load_adjustments(e.id, "xero/20261017-q1", ["AJE-X01"])
    await xero.load(e.id, "xero/20261017-q1")
    trial = await financial.trial_balance(e.id, "current")
    assert await state(e.id) == (1017, 1)
    first = await xero.load_adjustments(e.id, "xero/20261017-q1", ["AJE-X01", "AJE-X05"])
    assert [(r.id, r.account_code, r.debit_cents, r.credit_cents) for r in first] == [
        ("AJE-X01-1", "711", 82500, 0),
        ("AJE-X01-2", "710", 0, 75000),
        ("AJE-X01-3", "820", 0, 7500),
        ("AJE-X05-1", "620", 4853, 0),
        ("AJE-X05-2", "270", 0, 4853),
    ]
    assert await state(e.id) == (1022, 2)
    assert await xero.load_adjustments(e.id, "xero/20261017-q1", ["AJE-X01", "AJE-X05"]) == first  # 幂等
    assert await state(e.id) == (1022, 2)
    second = await xero.load_adjustments(e.id, "xero/20261017-q1", ["AJE-X02", "AJE-X03", "AJE-X04"], actor=user)
    assert len(second) == 6 and await state(e.id) == (1028, 3)
    # 需求 R274: 每轮更正分录载入在 data_changes 记总账分录 (过期判断按它), 幂等的那次不记
    from backend.persistence.data_store import PostgresDataStore

    recorded = await PostgresDataStore(tmp_path, engine).changes(e.id)
    assert [(c.data_version, c.input.value) for c in recorded if c.data_version > 1] == [
        (2, "journal_entries"),
        (3, "journal_entries"),
    ]
    assert len(await xero.load_adjustments(e.id, "xero/20261017-q1")) == 11  # 全部: 已有的跳过
    assert await state(e.id) == (1028, 3)
    assert await xero.load_adjustments(e.id, "xero/20261017-q1", []) == []  # 空列表不载入
    assert await state(e.id) == (1028, 3)
    as_of = date(2026, 9, 30)
    assert [await financial.gl_balance(e.id, code, as_of) for code in ("620", "270", "710", "820")] == [
        4853,
        -4853,
        0,
        -126697,
    ]
    assert await financial.trial_balance(e.id, "current") == trial  # 试算表不动
    # 需求 R271: 两轮各一版总账 (幂等与空列表的调用不出版本), 体检不计审计更正分录
    from backend.contracts.data import DataCheck, TableKind
    from backend.services.data_quality import run_checks

    data = PostgresDataStore(tmp_path, engine)
    journals = [v for v in await data.versions(e.id) if v.kind is TableKind.journal_entries]
    assert [(v.id, v.data_version, v.records, v.source, v.file_name, v.created_by) for v in journals] == [
        ("XERO-journal_entries-v2", 2, 1022, "xero", "adjustments/journal_entries.csv", "A1"),
        ("XERO-journal_entries-v3", 3, 1028, "xero", "adjustments/journal_entries.csv", user),
    ]

    async def checks(prefix: str | None) -> dict[str, DataCheck]:
        inputs = await data.check_inputs(e.id, date(2026, 7, 1), as_of, adjustment_prefix=prefix)
        result = run_checks(inputs, journal_version=journals[-1], now=now())
        return {c.id.value: c for c in result.checks}

    excluded = await checks("AJE-")
    assert [excluded[k].status for k in ("debits_equal_credits", "journals_agree_tb", "count_agrees_source")] == [
        "passed",
        "passed",
        "passed",
    ]
    assert excluded["count_agrees_source"].params == {"source": "Xero", "expected": 1028, "received": 1028}
    counted = (await checks(None))["journals_agree_tb"]  # 去掉排除规则: 更正分录计入发生额
    assert counted.status == "failed"
    assert {"account_code": "620", "difference_cents": 4853} in counted.params["differences"]
    assert {"account_code": "710", "difference_cents": -75000} in counted.params["differences"]
    # 前缀按字面量匹配 (autoescape): A_E- 里的 _ 不当通配符, 不排除 AJE- 的行 (审核 R271-03)
    period = (date(2026, 7, 1), as_of)
    literal = await data.check_inputs(e.id, *period, adjustment_prefix="A_E-")
    assert literal.movements == (await data.check_inputs(e.id, *period)).movements

    # 同 ID 内容不同: 更正分录文件换成摘要改过的副本 (只替换文件位置, 走同一套写入)
    source = supplements / "20261017-q1" / "adjustments" / "journal_entries.csv"
    edited_csv = tmp_path / "edited.csv"
    edited_csv.write_text(
        source.read_text(encoding="utf-8").replace("Audit adjustment AJE-X01:", "Audit adjustment AJE-X01 (edited):"),
        encoding="utf-8",
    )

    class Edited(XeroDatasetLoader):
        def adjustments_path(self, dataset: str) -> Path:
            return edited_csv

    edited = Edited(api, ROOT / "src/backend/knowledge/planning", engine, evidence)
    with pytest.raises(ValueError, match="AJE-X01-1"):
        await edited.load_adjustments(e.id, "xero/20261017-q1", ["AJE-X01"])
    assert await state(e.id) == (1028, 3)

    # 数据集不同: 项目载入的是 demo
    demo = await engagement("demo")
    await CsvDatasetLoader(ROOT / "data", engine, evidence).load(demo.id, "demo")
    before = await state(demo.id)
    with pytest.raises(ValueError, match="different dataset"):
        await xero.load_adjustments(demo.id, "xero/20261017-q1", ["AJE-X01"])
    assert await state(demo.id) == before

    # 新项目一次载入全部: 11 行, 版本只加一
    fresh = await engagement()
    await xero.load(fresh.id, "xero/20261017-q1")
    assert len(await xero.load_adjustments(fresh.id, "xero/20261017-q1")) == 11
    assert await state(fresh.id) == (1028, 2)
    assert [
        (v.data_version, v.records) for v in await data.versions(fresh.id) if v.kind is TableKind.journal_entries
    ] == [(2, 1028)]
    assert [await financial.gl_balance(fresh.id, code, as_of) for code in ("620", "270", "710", "820")] == [
        4853,
        -4853,
        0,
        -126697,
    ]

    # 审批日志列了更正凭证号 (审核 R271-01): 写总账时补上审批人, 同参数再调仍按同一版审批日志比较, 不写
    from backend.contracts.data import ApprovalLogRow

    approved = await engagement()
    await xero.load(approved.id, "xero/20261017-q1")
    log = [ApprovalLogRow(entry_no="AJE-X01", approved_by="Reviewer")]
    await data.write_tables(approved.id, {TableKind.approval_log: log}, source="import", actor=user)
    loaded = await xero.load_adjustments(approved.id, "xero/20261017-q1", ["AJE-X01"])
    assert [r.approved_by for r in loaded] == ["Reviewer"] * 3 and await state(approved.id) == (1020, 3)
    assert await xero.load_adjustments(approved.id, "xero/20261017-q1", ["AJE-X01"]) == loaded
    assert await state(approved.id) == (1020, 3)

    # 项目锁定
    locked = await engagement()
    await xero.load(locked.id, "xero/20261017-q1")
    await store.update(locked.id, locked=True)
    with pytest.raises(EngagementLocked):
        await xero.load_adjustments(locked.id, "xero/20261017-q1", ["AJE-X01"])
    assert await state(locked.id) == (1017, 1)


# ── 试算表加已验证更正分录: 数据库函数 adjusted_trial_balance (需求 R174) ──

ADJUSTED = "SELECT account_code, balance_cents FROM adjusted_trial_balance(:target, :entry_nos)"
ADJUSTMENT_ROUNDS = (["AJE-X01", "AJE-X05"], ["AJE-X02", "AJE-X03", "AJE-X04"])  # 按 MD-8 两轮载入的凭证
# PLN-01 M01 的余额来源: 本期试算表; 同口径换成更正后余额时只换这一段
TB_BALANCES = (
    "FROM trial_balance t\n JOIN accounts a ON a.engagement_id=t.engagement_id AND a.code=t.account_code\n"
    " WHERE t.engagement_id=:eid AND t.period='current')"
)
ADJUSTED_BALANCES = (
    "FROM adjusted_trial_balance(:eid, :entry_nos) t\n"
    " JOIN accounts a ON a.engagement_id=:eid AND a.code=t.account_code)"
)


@pytest.mark.external
async def test_adjusted_trial_balance_on_xero(xero_storage):
    """R174 验收 5: 真实库载入 xero/20261017-q1, 按 MD-8 (load_adjustments) 两轮载入更正分录. 数据库函数
    adjusted_trial_balance 与 services/adjusted_tb.adjusted_balances (分录行按凭证号取自库) 逐科目相等 (函数的行序
    不定, 按科目比); PLN-01 M01 同口径 (模板的 SQL 只把余额来源换成该函数) 得三个基准: 列表为空同 PLN-01, 第 1 轮
    (AJE-X01, X05), 第 2 轮 (另加 X02 至 X04); 已载入而不在列表中的凭证不算; NULL 同空列表. 系统试算表不随更正改版.
    SECURITY INVOKER: 绑定别的项目时读不到本项目的行 (行级安全照常生效); 绑定本项目而 p_eid 为别的项目时也不取本项目
    的行."""
    from backend.services.adjusted_tb import adjusted_balances

    x = xero_storage
    financial = PostgresFinancialDataStore(x.engine)
    pln01 = ProcedureLibrary(ROOT / "src/backend/knowledge").load("PLN-01")
    [original] = [r.sql for r in pln01.rules if r.id == "M01"]
    assert original.count(TB_BALANCES) == 1
    m01 = original.replace(TB_BALANCES, ADJUSTED_BALANCES)
    categories = {k: pln01.params[k] for k in ("revenue_categories", "income_tax_categories")}
    e = await x.engagement()
    await x.xero.load(e.id, XERO_Q1)
    trial = await financial.trial_balance(e.id, "current")

    async def bases(sql: str, entry_nos: list[str]) -> tuple[int, int, int]:
        rows = {
            r["basis"]: r["base_amount_cents"]
            for r in await financial.query(e.id, sql, categories | {"entry_nos": entry_nos})
        }
        return rows["revenue"], rows["total_assets"], rows["profit_before_tax"]

    async def adjusted(target: str, entry_nos: list[str] | None, bound: str | None = None) -> dict[str, int]:
        rows = await financial.query(bound or e.id, ADJUSTED, {"target": target, "entry_nos": entry_nos})
        return {r["account_code"]: r["balance_cents"] for r in rows}

    async def agrees(entry_nos: list[str]) -> None:
        lines = await financial.journal_entries(e.id, entry_nos=entry_nos)
        assert await adjusted(e.id, entry_nos) == adjusted_balances(trial, lines, entry_nos)

    first, both = ADJUSTMENT_ROUNDS[0], sorted(ADJUSTMENT_ROUNDS[0] + ADJUSTMENT_ROUNDS[1])
    unadjusted = (3243100, 1080529, -1322696)
    assert await bases(original, []) == await bases(m01, []) == unadjusted  # 列表为空: 同 PLN-01
    await agrees([])
    assert await adjusted(e.id, None) == await adjusted(e.id, [])  # NULL 同空列表
    await x.xero.load_adjustments(e.id, XERO_Q1, first)
    await agrees(first)
    assert await bases(m01, first) == (3243100, 1092882, -1317843)
    await x.xero.load_adjustments(e.id, XERO_Q1, ADJUSTMENT_ROUNDS[1])
    await agrees(both)
    await agrees(first)  # 已载入的 X02 至 X04 不在列表中: 不算
    assert await bases(m01, both) == (3243100, 1092882, -2213699)
    assert await bases(m01, first) == (3243100, 1092882, -1317843)
    assert await bases(m01, []) == await bases(original, []) == unadjusted
    assert await financial.trial_balance(e.id, "current") == trial  # 系统试算表不随更正改版
    other = await x.engagement()
    assert await adjusted(e.id, both, bound=other.id) == {}  # 行级安全: 读不到本项目的行
    assert await adjusted(other.id, both) == {}  # 只取 p_eid 的行


async def test_migration_0022_round_trip() -> None:
    """R174 验收 4: 以属主角色在一个事务里走 0022 降级再升级两次, 最后回滚 (不动测试库): 降级后函数不在 (同停在 0021
    的库), 升级后可调且不是 SECURITY DEFINER, 再升级一次照常 (CREATE OR REPLACE). 需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    module = migration_module("0022_adjusted_trial_balance")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    definer = text("SELECT prosecdef FROM pg_proc WHERE proname = 'adjusted_trial_balance'")
    call = text("SELECT count(*) FROM adjusted_trial_balance('ENG-none', ARRAY['AJE-X01'])")
    owner = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with owner.connect() as conn:
            outer = await conn.begin()
            try:
                assert (await conn.execute(definer)).scalars().all() == [False]
                await conn.run_sync(run("downgrade"))
                assert (await conn.execute(definer)).scalars().all() == []
                for _ in range(2):
                    await conn.run_sync(run("upgrade"))
                    assert (await conn.execute(definer)).scalars().all() == [False]
                    assert (await conn.execute(call)).scalar_one() == 0
            finally:
                await outer.rollback()
    finally:
        await owner.dispose()


def synthetic_abn() -> str:
    """随机 9 位编号补上 ABR 算法的校验位 (测试库是共用的, 每次用不同的 ABN 免得撞上别的用例)."""
    from backend.services.abn import abn_problem

    while True:
        body = f"{uuid4().int % 10**9:09d}"
        hits = [f"{p}{body}" for p in range(10, 100) if abn_problem(f"{p}{body}") is None]
        if hits:
            return hits[0]


async def test_new_clients_and_engagements_without_dataset(tmp_path):
    """R288: create_client 写第 1 版 (来源 manual, 写入人); 同一 ABN (库里最新一版或还没进库的数据集画像) 抛
    AbnTaken; 已有的编号抛 VersionConflict 不覆盖; clients 取最新一版与第 1 版的来源; 没有数据集的项目可建可读,
    数据管理的 dataset_loads 行 dataset 为空; ACC-01 更新记来源 update."""
    from backend.contracts.errors import AbnTaken
    from backend.contracts.models import ProfileSource
    from backend.persistence.data_store import PostgresDataStore

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="R288", password_hash="x", created_at=now()
            )
        )
    store = PostgresEngagementStore(engine)
    client = "CLIENT-M-" + uuid4().hex[:12]
    abn, other_abn = synthetic_abn(), synthetic_abn()
    profile = ClientProfile(
        id=client,
        legal_name="Wombat Analytics Pty Ltd",
        abn=abn,
        entity_type="small_proprietary",
        industry_anzsic="6920",
        listed=False,
        directors=["Jane Citizen"],
    )
    saved = await store.create_client(profile, created_by=user, dataset_profiles=[])
    assert saved == profile
    [record] = await store.profile_history(client)
    assert (record.profile, record.source, record.updated_by) == (profile, ProfileSource.manual, user)
    [entry] = [e for e in await store.clients() if e.profile.id == client]
    assert (entry.profile, entry.first_source) == (profile, ProfileSource.manual)

    twin = profile.model_copy(update={"id": "CLIENT-M-" + uuid4().hex[:12], "legal_name": "Twin Pty Ltd"})
    with pytest.raises(AbnTaken) as taken:
        await store.create_client(twin, created_by=user, dataset_profiles=[])
    assert (taken.value.client_id, taken.value.legal_name) == (client, "Wombat Analytics Pty Ltd")
    from_dataset = profile.model_copy(update={"id": "CLIENT-DS-" + uuid4().hex[:8], "abn": other_abn})
    with pytest.raises(AbnTaken):
        await store.create_client(
            twin.model_copy(update={"abn": other_abn}), created_by=user, dataset_profiles=[from_dataset]
        )
    with pytest.raises(VersionConflict):
        await store.create_client(profile.model_copy(update={"abn": other_abn}), created_by=user, dataset_profiles=[])
    assert [r.profile for r in await store.profile_history(client)] == [profile]  # 撞号不覆盖

    engagement = await store.create(
        owner_id=user, period_start=date(2026, 7, 1), period_end=date(2027, 6, 30), dataset=None, profile=saved
    )
    assert (engagement.dataset, engagement.client_profile_version) == (None, 1)
    assert await store.get(engagement.id) == engagement
    readiness = await PostgresDataStore(tmp_path, engine).confirm_ready(engagement.id, user, reason="R288")
    assert readiness.ready_by == user
    async with transaction(engine, engagement.id) as conn:
        loaded = (await conn.execute(select(m.dataset_loads.c.dataset))).scalar_one()
    assert loaded is None
    await store.save_profile(
        profile.model_copy(update={"version": 2, "directors": ["Jane Citizen", "Sam Example"]}),
        source=ProfileSource.update,
        updated_by=user,
    )
    assert [(r.profile.version, r.source) for r in await store.profile_history(client)] == [
        (1, ProfileSource.manual),
        (2, ProfileSource.update),
    ]
    [entry] = [e for e in await store.clients() if e.profile.id == client]
    assert (entry.profile.version, entry.first_source) == (2, ProfileSource.manual)

    # R289: ACC-01 改了 ABN 时 save_profile 带 dataset_profiles 另查重, 在项目事务里 (同 update_client_profile), 与
    # create_client 同一把锁, 同一范围, 不含本客户自己: 撞上别的客户或数据集画像抛 AbnTaken, 不写
    neighbour_abn = synthetic_abn()
    neighbour = profile.model_copy(update={"id": "CLIENT-M-" + uuid4().hex[:12], "abn": neighbour_abn})
    await store.create_client(neighbour, created_by=user, dataset_profiles=[])
    v3 = profile.model_copy(update={"version": 3})
    for abn, others in ((neighbour_abn, []), (other_abn, [from_dataset])):
        with pytest.raises(AbnTaken):
            async with transaction(engine, engagement.id):
                await store.save_profile(
                    v3.model_copy(update={"abn": abn}),
                    source=ProfileSource.update,
                    updated_by=user,
                    dataset_profiles=others,
                )
    assert [r.profile.version for r in await store.profile_history(client)] == [1, 2]
    async with transaction(engine, engagement.id):  # ABN 是本客户自己的 (库里最新一版), 不算撞上
        await store.save_profile(v3, source=ProfileSource.update, updated_by=user, dataset_profiles=[from_dataset])
    assert [r.profile.version for r in await store.profile_history(client)] == [1, 2, 3]
    await engine.dispose()


async def test_concurrent_new_clients_with_one_abn_only_one_wins():
    """R288 (审核 R288-03): 并发新建同一 ABN 的几个客户, 查重与写入在同一把咨询锁内, 每轮恰好一个成功, 其余抛
    AbnTaken; 库里该 ABN 只有一个客户. ABN 为合成的 (随机 9 位编号, 校验位按算法补)."""
    from backend.contracts.errors import AbnTaken

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"), pool_size=8)
    store = PostgresEngagementStore(engine)
    try:
        for _ in range(3):
            abn = synthetic_abn()
            profiles = [
                ClientProfile(
                    id="CLIENT-M-" + uuid4().hex[:12],
                    legal_name=f"Concurrent Wombat Pty Ltd {n}",
                    abn=abn,
                    entity_type="small_proprietary",
                    industry_anzsic="6920",
                    listed=False,
                    directors=["Jane Citizen"],
                )
                for n in range(4)
            ]
            results = await asyncio.gather(
                *(store.create_client(p, created_by="U-1", dataset_profiles=[]) for p in profiles),
                return_exceptions=True,
            )
            won = [r for r in results if isinstance(r, ClientProfile)]
            assert len(won) == 1 and all(isinstance(r, AbnTaken) for r in results if r not in won), results
            assert [e.profile.id for e in await store.clients() if e.profile.abn == abn] == [won[0].id]
    finally:
        await engine.dispose()


async def test_payroll_register_written_as_a_new_data_version(storage, tmp_path):
    """需求 R96: 数据集载入不写工资册; 经 PostgresDataStore.write_tables (source import) 写入后 employees 按员工编号读回
    3 行 (一行雇佣类型为空), 账表查看 (table_rows) 同序, 数据版本 +1, data_imports 一版, 记 employees 变动; 再写整表
    替换; 另一项目读不到 (行级安全)."""
    from backend.contracts.data import DataChange, DataInput, TableKind
    from backend.persistence.data_store import PostgresDataStore
    from tests.test_employees import EXPECTED

    staff = [EXPECTED[0], EXPECTED[1].model_copy(update={"employment_type": None}), EXPECTED[2]]  # E002 雇佣类型为空

    engine, e, loader, _evidence = storage
    other = await PostgresEngagementStore(engine).create(
        owner_id=e.owner_id,
        period_start=date(2025, 7, 1),
        period_end=date(2026, 6, 30),
        dataset="demo",
        profile=loader.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex}),
    )
    await loader.load(e.id, "demo")
    financial = PostgresFinancialDataStore(engine)
    data = PostgresDataStore(tmp_path, engine)
    assert await financial.employees(e.id) == []  # 数据集载入不写它
    assert (await data.stats(e.id))[TableKind.employees].records == 0

    version = await data.write_tables(
        e.id, {TableKind.employees: list(reversed(staff))}, source="import", actor="U-1", file_name="employees.csv"
    )

    rows = await financial.employees(e.id)
    assert [(r.employee_id, r.name, r.gross_per_period_cents) for r in rows] == [
        ("E001", "Oliver Gray", 240000),
        ("E002", "Tracy Green", 180000),
        ("E003", "Odette Garrison", 250000),
    ]
    assert rows == staff  # 入职日, 雇佣类型 (空的读回 None) 与发薪周期也读回
    total, page = await data.table_rows(e.id, TableKind.employees, None, 0, 50)
    assert (total, [(r["employee_id"], r["employment_type"]) for r in page]) == (
        3,
        [("E001", "full_time"), ("E002", None), ("E003", "full_time")],
    )
    assert version == 2 and (await data.readiness(e.id)).data_version == 2
    assert [(v.kind, v.source, v.records, v.file_name, v.data_version) for v in await data.versions(e.id)] == [
        (TableKind.employees, "import", 3, "employees.csv", 2)
    ]
    assert [c for c in await data.changes(e.id) if c.data_version == 2] == [
        DataChange(data_version=2, input=DataInput.employees)
    ]
    assert (await data.stats(e.id))[TableKind.employees].records == 3

    await data.write_tables(e.id, {TableKind.employees: staff[:1]}, source="import", actor="U-1")  # 整表替换
    assert [r.employee_id for r in await financial.employees(e.id)] == ["E001"]
    assert await financial.employees(other.id) == []
    assert await financial.query(other.id, "SELECT count(*) AS n FROM employees", {}) == [{"n": 0}]
    assert await financial.query(e.id, "SELECT count(*) AS n FROM employees", {}) == [{"n": 1}]


async def test_collab_dispute_variant_real_storage(tmp_path):
    """需求 R155 验收 5 (R56 的真实存储变体, 本组开关打开): Numbat 争议变体 (复核模型换成 llm_variants/dispute) 在真实
    Postgres 存储与检查点上跑到归档. 停在等复核时 CSH-02 在检查点与 Store 中读回都是枚举 in_dispute; 收尾同
    test_collab_flow 的争议变体 (呈递两条任务后提交, D-ARB 为 a6_upheld 带咨询). 呈递留痕 2 行从库读回 (GEN-11,
    负责人, 任务), D-ARB 与 D-REV02-0612 读回的 dwell_seconds 95, 40 与 presentation_id 对得上那两行, D-ARB 的
    consulted 与咨询记录读回不变; 结果同 R55."""

    from backend.adapters.llm import MockLLMClient, SwitchableLLMClient
    from backend.contracts.models import (
        DisputeResolution,
        GateVerdict,
        Procedure,
        ProcedureStatus,
        ReviewRecord,
        TrailKind,
    )
    from backend.orchestration.runner import GraphRunner
    from tests.fakes import wait_idle
    from tests.test_collab_flow import COLLAB, CONSULTATION, PRESENTATION, REV02_TASK, SHOWN, TASK, presenting
    from tests.test_decisions import acc01_decision, pln_decision
    from tests.test_dispute import VARIANTS, resolve
    from tests.test_e2e_mock import assert_results, real_storage
    from tests.test_flow import FIN_APPROVE, REVIEWS, RPT_APPROVE, RPT_REJECT

    async with real_storage(tmp_path, FEATURES=",".join(sorted(COLLAB))) as run:
        s, deps, eid, owner = run.s, run.deps, run.eid, run.owner
        cast(SwitchableLLMClient, deps.review_llm).reload(MockLLMClient(VARIANTS / "dispute"), "mock")
        arbitration = resolve(DisputeResolution.a6_upheld, consulted=True, consultation_record=CONSULTATION)
        reviews = [arbitration if r.task_id == TASK else r for r in REVIEWS]
        present = presenting(SHOWN)
        await s.runner.start(eid)
        await wait_idle(s.runner, eid)
        for decision in [acc01_decision(GateVerdict.approve, id="D-ACC"), pln_decision(GateVerdict.approve)]:
            await s.submit.submit(eid, decision.model_copy(update={"decided_by": owner}))
            await wait_idle(s.runner, eid)
        values = (await s.runner.app.aget_state(GraphRunner.config(eid))).values
        assert next(p for p in values["procedure_pack"] if p.id == "CSH-02").status is ProcedureStatus.in_dispute
        assert (await deps.output_store.get(eid, Procedure, "CSH-02")).status is ProcedureStatus.in_dispute
        for decision in [*reviews, FIN_APPROVE, RPT_REJECT, RPT_APPROVE]:
            actor = "reviewed_by" if isinstance(decision, ReviewRecord) else "decided_by"
            decision = decision.model_copy(update={actor: owner})
            if isinstance(decision, ReviewRecord):
                await present(s.runner, eid, decision)
            await s.submit.submit(eid, decision)
            await wait_idle(s.runner, eid)
            assert s.runner.run_error(eid) is None, (decision.id, s.runner.run_error(eid))

        await assert_results(s, eid, approver=owner)
        entries = await deps.trail.list(eid, limit=10_000)
        shown = [e for e in entries if e.kind is TrailKind.presentation]
        # 库里按呈递时间排: CSH-02 那次在决定前 95 秒, 早于 REV-02 的 40 秒
        assert [(e.procedure_id, e.actor, e.payload["task_id"]) for e in shown] == [
            (PRESENTATION, owner, TASK),
            (PRESENTATION, owner, REV02_TASK),
        ]
        ids = {e.payload["task_id"]: e.id for e in shown}
        arbitrated = await deps.output_store.get(eid, ReviewRecord, "D-ARB")
        cutoff = await deps.output_store.get(eid, ReviewRecord, "D-REV02-0612")
        assert (arbitrated.dwell_seconds, arbitrated.presentation_id) == (95, ids[TASK])
        assert (arbitrated.consulted, arbitrated.consultation_record) == (True, CONSULTATION)
        assert (cutoff.dwell_seconds, cutoff.presentation_id) == (40, ids[REV02_TASK])
        moved = [
            (e.payload["from"], e.payload["to"])
            for e in entries
            if e.kind is TrailKind.transition and e.payload["id"] == "CSH-02"
        ]
        assert moved == [("planned", "in_dispute"), ("in_dispute", "completed")]


async def test_currency_on_postgres(storage) -> None:
    """币种读写, 同值幂等, 人工确认优先于同步, 锁定保护与新建时的来源在真实库核验."""
    engine, e, loader, _ = storage
    engagements = PostgresEngagementStore(engine)
    assert (e.currency, e.currency_source) == ("AUD", "default")
    assert await engagements.set_currency(e.id, "NZD", "xero") == e
    synced = await engagements.get(e.id)
    assert synced is not None and (synced.currency, synced.currency_source) == ("NZD", "xero")
    assert await engagements.set_currency(e.id, "NZD", "xero") == synced
    assert await engagements.get(e.id) == synced
    await engagements.set_currency(e.id, "USD", "auditor")
    manual = await engagements.get(e.id)
    assert await engagements.set_currency(e.id, "NZD", "xero") == manual
    assert await engagements.get(e.id) == manual
    await engagements.update(e.id, locked=True)
    with pytest.raises(EngagementLocked):
        await engagements.set_currency(e.id, "NZD", "auditor")
    profile = loader.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    created = await engagements.create(
        owner_id=e.owner_id,
        period_start=e.period_start,
        period_end=e.period_end,
        dataset="demo",
        profile=profile,
        currency="NZD",
    )
    assert (created.currency, created.currency_source) == ("NZD", "xero")
    assert await engagements.get(created.id) == created


async def test_migration_0024_round_trip(storage) -> None:
    """旧项目升级后以 AUD/default 回填, 重复升级幂等; 降级移除两列, 最后回滚不影响其他连库用例."""
    engine, e, _, _ = storage
    await PostgresEngagementStore(engine).set_currency(e.id, "NZD", "auditor")
    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = migration_module("0024_engagement_currency")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    columns = text(
        "SELECT column_name, column_default, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'engagements' AND column_name IN ('currency', 'currency_source') ORDER BY column_name"
    )
    owner = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with owner.connect() as conn:
            outer = await conn.begin()
            try:
                assert (await conn.execute(columns)).all() == [
                    ("currency", "'AUD'::text", "NO"),
                    ("currency_source", "'default'::text", "NO"),
                ]
                await conn.run_sync(run("downgrade"))
                assert (await conn.execute(columns)).all() == []
                await conn.run_sync(run("upgrade"))
                await conn.run_sync(run("upgrade"))
                assert (
                    await conn.execute(text("SELECT DISTINCT currency, currency_source FROM engagements"))
                ).all() == [("AUD", "default")]
            finally:
                await outer.rollback()
    finally:
        await owner.dispose()


async def test_currency_and_trail_share_transaction(storage) -> None:
    """币种确认与对应留痕同提交/回滚; 默认独立留痕的其他调用不受影响."""
    engine, e, _, _ = storage
    engagements = PostgresEngagementStore(engine)
    outputs = PostgresAuditOutputStore(engine)
    trail = PostgresAuditTrail(engine)
    entry = TrailEntry(
        engagement_id=e.id,
        kind=TrailKind.data,
        actor=e.owner_id,
        summary="Synthetic currency confirmation",
        payload={"action": "currency", "from": "AUD", "to": "NZD", "source": "auditor"},
    )
    with pytest.raises(RuntimeError, match="rollback"):
        async with outputs.transaction(e.id):
            await engagements.set_currency(e.id, "NZD", "auditor")
            rolled_back = await trail.record(entry, independent=False)
            raise RuntimeError("rollback")
    assert await engagements.get(e.id) == e
    assert await trail.get(e.id, rolled_back) is None
    async with outputs.transaction(e.id):
        await engagements.set_currency(e.id, "NZD", "auditor")
        committed = await trail.record(entry, independent=False)
    assert (await engagements.get(e.id)).currency == "NZD"
    assert (await trail.get(e.id, committed)).payload == entry.payload
