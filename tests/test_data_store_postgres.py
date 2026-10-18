"""数据管理存储 (PostgresDataStore) 的真实 PostgreSQL 测试. 设 TEST_DATABASE_URL 为已迁移到最新的独立测试库."""

import csv
import os
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from backend.contracts.data import TableKind
from backend.contracts.errors import EngagementLocked
from backend.persistence import models as m
from backend.persistence.data_store import PostgresDataStore
from backend.persistence.db import transaction
from backend.persistence.stores import (
    CsvDatasetLoader,
    FileEvidenceStore,
    PostgresEngagementStore,
    PostgresFinancialDataStore,
    now,
)

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


async def test_dataset_load_counts_as_ready_and_stats_match_csv(storage) -> None:  # type: ignore[no-untyped-def]
    engine, (first, _), loader, _ = storage
    store = PostgresDataStore(engine)
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


async def test_confirm_ready_keeps_first_confirmation_and_respects_lock(storage) -> None:  # type: ignore[no-untyped-def]
    engine, (_, second), _, engagements = storage
    store = PostgresDataStore(engine)

    first = await store.confirm_ready(second.id, "U-1")
    again = await store.confirm_ready(second.id, "U-2")

    assert (first.data_version, first.ready_by) == (0, "U-1")
    assert again == first  # 已确认的不改
    await engagements.update(second.id, locked=True)
    with pytest.raises(EngagementLocked):
        await store.confirm_ready(second.id, "U-3")


async def test_versions_checks_and_open_requests(storage) -> None:  # type: ignore[no-untyped-def]
    """数据概览读的三项 (需求 R11): 已提交的表版本 (不含解析中的会话), 体检结果按体检项顺序, 待补交请求数."""
    engine, (first, second), _, _ = storage
    store = PostgresDataStore(engine)
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
