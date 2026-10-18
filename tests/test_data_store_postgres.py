"""数据管理存储 (PostgresDataStore) 的真实 PostgreSQL 测试. 设 TEST_DATABASE_URL 为已迁移到最新的独立测试库."""

import csv
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.contracts.data import ApprovalLogRow, ImportCommit, ImportSession, TableKind
from backend.contracts.errors import EngagementLocked
from backend.contracts.models import DocumentType, Reliability
from backend.contracts.records import Account, JournalEntry
from backend.orchestration.runner import GraphRunner
from backend.persistence import models as m
from backend.persistence.data_store import PostgresDataStore
from backend.persistence.db import transaction
from backend.persistence.stores import (
    FINANCIAL_ROWS,
    CsvDatasetLoader,
    FileEvidenceStore,
    PostgresEngagementStore,
    PostgresFinancialDataStore,
    now,
)
from backend.services.data_service import DataService

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"


@pytest.fixture
async def storage(tmp_path: Path):  # type: ignore[no-untyped-def]
    """测试库上的一个新用户与它的两个项目 (都以 demo 为客户来源, 尚未载入数据)."""
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
    created = [
        await engagements.create(
            owner_id=user, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
        )
        for _ in range(2)
    ]
    yield engine, created, loader, engagements
    await engine.dispose()


def csv_rows(name: str) -> list[dict[str, str]]:
    with (DEMO / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


async def test_dataset_load_counts_as_ready_and_stats_match_csv(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    engine, (first, _), loader, _ = storage
    store = PostgresDataStore(tmp_path, engine)
    before = await store.readiness(first.id)
    assert (before.data_version, before.ready_at, before.ready_by) == (0, None, None)
    assert all(s.records == 0 for s in (await store.stats(first.id)).values())

    await loader.load(first.id, "demo")

    readiness = await store.readiness(first.id)
    assert (readiness.data_version, readiness.ready_by) == (1, "dataset")
    assert readiness.ready_at == readiness.loaded_at is not None
    stats = await store.stats(first.id)
    tb = csv_rows("trial_balance")
    assert (stats[TableKind.trial_balance].current, stats[TableKind.trial_balance].prior) == (
        sum(r["period"] == "current" for r in tb),
        sum(r["period"] == "prior" for r in tb),
    )
    journals = csv_rows("journal_entries")
    assert stats[TableKind.journal_entries].records == len(journals)
    assert stats[TableKind.journal_entries].period_from == date.fromisoformat(min(r["posting_date"] for r in journals))
    assert stats[TableKind.journal_entries].period_to == date.fromisoformat(max(r["posting_date"] for r in journals))
    for kind in ("accounts", "sales_invoices", "bank_accounts", "bank_transactions", "delivery_notes"):
        assert stats[TableKind(kind)].records == len(csv_rows(kind)), kind
    assert stats[TableKind.approval_log].records == 0
    # documents 表的新列 (媒体类型, 来源渠道) 有缺省值, 读取不受影响
    financial = PostgresFinancialDataStore(engine)
    assert len(await financial.documents(first.id)) == 3
    assert await financial.data_version(first.id) == 1


async def test_confirm_ready_keeps_first_confirmation_and_respects_lock(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    engine, (_, second), _, engagements = storage
    store = PostgresDataStore(tmp_path, engine)

    first = await store.confirm_ready(second.id, "U-1")
    again = await store.confirm_ready(second.id, "U-2")

    assert (first.data_version, first.ready_by) == (0, "U-1")
    assert again == first  # 已确认的不改
    await engagements.update(second.id, locked=True)
    with pytest.raises(EngagementLocked):
        await store.confirm_ready(second.id, "U-3")


async def test_versions_checks_and_open_requests(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """数据概览读的三项 (需求 R11): 已提交的表版本 (不含解析中的会话), 体检结果按体检项顺序, 待补交请求数."""
    engine, (first, second), _, _ = storage
    store = PostgresDataStore(tmp_path, engine)
    assert (await store.versions(first.id), await store.checks(first.id), await store.open_requests(first.id)) == (
        [],
        [],
        0,
    )
    at = now()
    async with transaction(engine, first.id) as conn:
        common = {"engagement_id": first.id, "created_by": "U-1", "created_at": at}
        imports = [
            {
                **common,
                "id": "IMP-2",
                "kind": "accounts",
                "source": "import",
                "file_name": "coa.csv",
                "records": 23,
                "status": "committed",
                "data_version": 2,
                "committed_at": at + timedelta(minutes=5),  # 上传后 5 分钟才提交: 版本时间取提交时间
            },
            {
                **common,
                "id": "XERO-journal_entries-v1",
                "kind": "journal_entries",
                "source": "xero",
                "records": 900,
                "status": "committed",
                "data_version": 1,  # 没有提交时间 (如直接写入的版本): 取创建时间
            },
            {**common, "id": "IMP-3", "source": "import", "file_name": "draft.xlsx", "status": "parsed"},
        ]
        for row in imports:  # 各行的列不同, 逐行插入
            await conn.execute(insert(m.data_imports).values(**row))
        await conn.execute(
            insert(m.data_checks),
            [
                {
                    "engagement_id": first.id,
                    "id": "mapping_complete",
                    "status": "failed",
                    "params": {"mapped": 20, "total": 23},
                    "data_version": 2,
                    "checked_at": at,
                },
                {
                    "engagement_id": first.id,
                    "id": "debits_equal_credits",
                    "status": "passed",
                    "params": {"debit_cents": 5, "credit_cents": 5},
                    "data_version": 2,
                    "checked_at": at,
                },
            ],
        )
        base = {
            "engagement_id": first.id,
            "kind": "table",
            "title": "t",
            "used_for": [],
            "reason_code": "missing_table",
            "reason_params": {},
            "raised_by": "A3",
            "created_at": at,
        }
        await conn.execute(
            insert(m.data_requests),
            [
                {**base, "id": "REQ-a", "status": "open"},
                {**base, "id": "REQ-b", "status": "received"},
                {**base, "id": "REQ-c", "status": "open"},
            ],
        )

    versions = await store.versions(first.id)
    assert [(v.id, v.kind, v.source, v.data_version, v.created_at) for v in versions] == [
        ("XERO-journal_entries-v1", TableKind.journal_entries, "xero", 1, at),
        ("IMP-2", TableKind.accounts, "import", 2, at + timedelta(minutes=5)),
    ]
    assert [c.id.value for c in await store.checks(first.id)] == ["debits_equal_credits", "mapping_complete"]
    assert await store.open_requests(first.id) == 2
    assert await store.versions(second.id) == []  # 行级安全: 别的项目看不到


# ── 需求 R13: 账表导入与新数据版本写入 ──


def import_service(engine_url: str, tmp_path: Path) -> DataService:
    """真实存储上的数据服务: 依赖按组装根建 (测试库, 证据根为临时目录), runner 只用来取项目锁."""
    from backend.core.config import Settings
    from backend.main import build_deps

    deps = build_deps(Settings(DATABASE_URL=engine_url, EVIDENCE_ROOT=tmp_path, LLM_MODE="mock"))
    return DataService(deps, GraphRunner(cast(Any, None), deps, {"mode": "mock", "model": "mock"}))


async def table_rows(engine: AsyncEngine, eid: str, name: str) -> list[dict[str, Any]]:
    table = m.FINANCIAL[name]
    keys = [c for c in table.primary_key.columns if c.name != "engagement_id"]
    async with transaction(engine, eid) as conn:
        rows = (await conn.execute(select(table).order_by(*keys))).mappings()
        return [{k: v for k, v in r.items() if k != "engagement_id"} for r in rows]


async def test_importing_demo_csvs_matches_dataset_loader(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """验收: data/demo 的 7 个 CSV 依次经 POST /data/imports 与 commit 的流程导入 (按系统建议的表种类, 对应与日期写法),
    结果与 CsvDatasetLoader 载入的一致; 每次导入数据版本 +1, 原文件登记为证据文件."""
    engine, (imported, loaded), loader, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    names = list(FINANCIAL_ROWS)
    for number, name in enumerate(names, start=1):
        preview = await service.start_import(
            imported.id, f"{name}.csv", (DEMO / f"{name}.csv").read_bytes(), actor="U-1"
        )
        assert preview.suggested_kind == name
        body = ImportCommit(
            kind=TableKind(name),
            sheet=preview.sheet,
            date_format=preview.suggested_date_format,
            mapping=preview.suggested_mapping,
        )
        result = await service.commit_import(imported.id, preview.import_id, body, "U-1")
        assert result.records == len(csv_rows(name))
        assert (await service.deps.data.readiness(imported.id)).data_version == number
    await loader.load(loaded.id, "demo")

    for name in names:
        assert await table_rows(engine, imported.id, name) == await table_rows(engine, loaded.id, name), name
    docs = await PostgresFinancialDataStore(engine).documents(imported.id)
    assert sorted((d.file_name, d.media_type, d.status, d.page_count) for d in docs) == sorted(
        (f"{name}.csv", "text/csv", "extracted", 1) for name in names
    )
    versions = await service.deps.data.versions(imported.id)
    assert [(v.kind.value, v.source, v.file_name, v.data_version) for v in versions] == [
        (name, "import", f"{name}.csv", n) for n, name in enumerate(names, start=1)
    ]


async def test_reimport_keeps_versions_and_auditor_categories(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    engine, (first, _), _, _ = storage
    store = PostgresDataStore(tmp_path, engine)
    v1 = [
        Account(code="4000", name="Sales", type="revenue", category="revenue", category_basis="name"),
        Account(
            code="6300", name="Freight outwards", type="expense", category="operating_expenses", category_basis="name"
        ),
    ]
    assert await store.write_tables(first.id, {TableKind.accounts: v1}, source="import", actor="U-1") == 1
    async with transaction(engine, first.id) as conn:  # 审计师把 6300 改成销售成本 (R16 的修改先直接写历史)
        await conn.execute(
            insert(m.account_category_history).values(
                engagement_id=first.id,
                account_code="6300",
                version=2,
                category="cost_of_sales",
                basis="auditor",
                changed_by="U-1",
                changed_at=now(),
                reason="运费计入销售成本",
            )
        )
    v2 = [
        Account(code="4000", name="Interest income", type="revenue", category="other_income", category_basis="name"),
        Account(
            code="6300", name="Freight outwards", type="expense", category="operating_expenses", category_basis="name"
        ),
    ]
    assert await store.write_tables(first.id, {TableKind.accounts: v2}, source="xero", actor="U-1") == 2

    accounts = {r["code"]: r for r in await table_rows(engine, first.id, "accounts")}
    assert (accounts["4000"]["category"], accounts["4000"]["category_basis"]) == ("other_income", "name")
    assert (accounts["6300"]["category"], accounts["6300"]["category_basis"]) == ("cost_of_sales", "auditor")
    async with transaction(engine, first.id) as conn:
        h = m.account_category_history
        history = (
            await conn.execute(
                select(h.c.account_code, h.c.version, h.c.category, h.c.basis, h.c.changed_by).order_by(
                    h.c.account_code, h.c.version
                )
            )
        ).all()
    assert [tuple(r) for r in history] == [
        ("4000", 1, "revenue", "name", "A3"),
        ("4000", 2, "other_income", "name", "A3"),
        ("6300", 1, "operating_expenses", "name", "A3"),
        ("6300", 2, "cost_of_sales", "auditor", "U-1"),  # 重新写入不覆盖审计师的, 也不再记新版
    ]
    versions = await store.versions(first.id)
    assert [(v.id, v.source, v.records, v.data_version) for v in versions] == [
        ("IMPORT-accounts-v1", "import", 2, 1),
        ("XERO-accounts-v2", "xero", 2, 2),
    ]
    async with transaction(engine, first.id) as conn:  # 旧版本的行快照仍在
        old = (await conn.execute(select(m.data_imports.c.rows).where(m.data_imports.c.data_version == 1))).scalar_one()
    assert [r["name"] for r in old] == ["Sales", "Freight outwards"]


async def test_approval_log_fills_journal_and_is_reapplied(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    engine, (first, _), _, _ = storage
    store = PostgresDataStore(tmp_path, engine)

    def line(entry: str, n: int, code: str, debit: int, credit: int) -> JournalEntry:
        return JournalEntry(
            id=f"{entry}-{n}",
            entry_no=entry,
            posting_date=date(2026, 7, 1),
            created_at=None,
            created_by=None,
            approved_by=None,
            account_code=code,
            debit_cents=debit,
            credit_cents=credit,
            description=None,
            source_doc=None,
        )

    journal = [line("J1", 1, "1010", 500, 0), line("J1", 2, "4000", 0, 500), line("J2", 1, "6300", 100, 0)]
    await store.write_tables(first.id, {TableKind.journal_entries: journal}, source="import", actor="U-1")
    approved = datetime(2026, 7, 2, 1, tzinfo=UTC)
    log = [
        ApprovalLogRow(entry_no="J1", created_by="ap.clerk", approved_by="cfo", approved_at=approved),
        ApprovalLogRow(entry_no="J2", approved_by="cfo"),  # 只对应了审批人: 录入人不动
        ApprovalLogRow(entry_no="J9", approved_by="ghost"),  # 总账里没有的凭证号忽略
    ]
    await store.write_tables(first.id, {TableKind.approval_log: log}, source="import", actor="U-1")

    def approvals(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [(r["id"], r["created_by"], r["approved_by"], r["approved_at"]) for r in rows]

    expected = [
        ("J1-1", "ap.clerk", "cfo", approved),
        ("J1-2", "ap.clerk", "cfo", approved),
        ("J2-1", None, "cfo", None),
    ]
    assert approvals(await table_rows(engine, first.id, "journal_entries")) == expected
    stats = await store.stats(first.id)
    assert stats[TableKind.approval_log].records == 3
    await store.write_tables(first.id, {TableKind.journal_entries: journal}, source="xero", actor="U-1")
    assert approvals(await table_rows(engine, first.id, "journal_entries")) == expected  # 新总账套用最新一版审批日志


async def test_import_session_staging_and_evidence_of_spreadsheet(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    engine, (first, second), _, _ = storage
    store = PostgresDataStore(tmp_path, engine)
    session = ImportSession(
        id="IMP-abc", file_name="tb.csv", sheet="tb", request_id="REQ-1", created_by="U-1", created_at=now()
    )
    await store.create_import(first.id, session, b"period,account_code,balance_cents\n")

    found = await store.import_session(first.id, "IMP-abc")
    assert found is not None and found[0] == session and found[1].startswith(b"period")
    assert await store.import_session(second.id, "IMP-abc") is None  # 行级安全
    await store.update_import(
        first.id, "IMP-abc", sheet="tb", date_format="dmy", mapping={"period": "period"}, row_count=0
    )
    await store.write_tables(first.id, {TableKind.trial_balance: []}, source="import", actor="U-1", import_id="IMP-abc")
    found = await store.import_session(first.id, "IMP-abc")
    assert found is not None and found[0].status == "committed"
    assert [(v.id, v.request_id, v.file_name) for v in await store.versions(first.id)] == [
        ("IMP-abc", "REQ-1", "tb.csv")
    ]

    evidence = FileEvidenceStore(tmp_path, engine)
    doc = await evidence.save(
        first.id,
        "tb.csv",
        b"a,b\n1,2\n",
        type=DocumentType.other,
        reliability=Reliability.internal_copy,
        media_type="text/csv",
        uploaded_by="U-1",
        status="extracted",
    )
    assert (doc.media_type, doc.page_count, doc.status, doc.uploaded_by, doc.channel) == (
        "text/csv",
        1,
        "extracted",
        "U-1",
        "auditor_upload",
    )
    path = await evidence.path(first.id, doc.id)
    assert path.suffix == ".csv" and path.read_bytes() == b"a,b\n1,2\n"
