"""Xero 网页连接 (需求 R279) 的真实 PostgreSQL 测试: 连接表的读写与行级安全, 库里只有令牌密文; 真实存储上走一次
连接, 同步, 断开, 同步结果与该快照作为数据集载入时逐行相同. 设 TEST_DATABASE_URL 为已迁移到最新 (0009) 的独立测试库.
Xero 用 tests/test_xero_link.FakeXero (假值, 回放快照)."""

import os
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.contracts.data import XERO_TABLES, XeroConnection, XeroTenant
from backend.orchestration.runner import GraphRunner
from backend.persistence import models as m
from backend.persistence.db import transaction
from backend.persistence.stores import FileEvidenceStore, PostgresEngagementStore, XeroDatasetLoader, now
from backend.persistence.xero_connections import PostgresXeroConnectionStore
from backend.services.data_service import DataService
from backend.services.xero_link import XeroLink
from tests.fakes import XERO_SNAPSHOT, XERO_SNAPSHOTS, xero_engagement, xero_snapshot
from tests.test_data_store_postgres import table_rows
from tests.test_xero_link import ACCESS, CODE, DEMO, REFRESH, SECRETS, START, FakeXero, settings

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def storage(tmp_path: Path):  # type: ignore[no-untyped-def]
    """测试库上的一个新用户与它的两个项目 (客户与期间取 Xero 快照, 尚未载入数据)."""
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
    profile, template = xero_engagement(xero_snapshot())
    profile = profile.model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagements = PostgresEngagementStore(engine)
    created = [
        await engagements.create(
            owner_id=user,
            period_start=template.period_start,
            period_end=template.period_end,
            dataset=template.dataset,
            profile=profile,
        )
        for _ in range(2)
    ]
    yield engine, created, user
    await engine.dispose()


def connection(tenant: dict[str, str] = DEMO) -> XeroConnection:
    tenants = [XeroTenant(id=tenant["tenantId"], name=tenant["tenantName"])]
    return XeroConnection(
        tenants=tenants,
        tenant_id=tenants[0].id,
        tenant_name=tenants[0].name,
        scope="offline_access",
        connected_by="U-1",
        connected_at=START,
        refreshed_at=START,
    )


async def test_store_roundtrip_row_level_security_and_ciphertext_only(storage) -> None:  # type: ignore[no-untyped-def]
    """连接表的读写: put 整行替换, update 不动令牌, set_tokens 记换得时间, delete 删行; 另一项目读不到 (行级安全);
    没有连接时 update 与 set_tokens 抛 LookupError."""
    engine, (first, second), _ = storage
    store = PostgresXeroConnectionStore(engine)
    assert await store.get(first.id) is None and await store.tokens(first.id) is None
    await store.put(first.id, connection(), "sealed-1")
    assert await store.get(first.id) == connection() and await store.tokens(first.id) == "sealed-1"
    synced = connection().model_copy(update={"last_synced_at": START, "synced_tables": list(XERO_TABLES)})
    await store.update(first.id, synced)
    later = START + timedelta(days=1)
    await store.set_tokens(first.id, "sealed-2", later)
    assert await store.get(first.id) == synced.model_copy(update={"refreshed_at": later})
    assert await store.tokens(first.id) == "sealed-2"
    assert await store.get(second.id) is None
    async with transaction(engine, second.id) as conn:  # 绑定别的项目: 行级安全下一行也看不到
        assert (await conn.execute(select(m.xero_connections))).all() == []
    with pytest.raises(LookupError):
        await store.update(second.id, connection())
    with pytest.raises(LookupError):
        await store.set_tokens(second.id, "x", later)
    assert await store.delete(first.id) and not await store.delete(first.id)
    assert await store.get(first.id) is None


async def test_connect_sync_disconnect_on_real_stores(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """真实存储上: 回调后库里的令牌列只有 Fernet 密文; 同步写一个新数据版本, 六张表逐行与该快照作为数据集载入另一项目
    时相同, 发运单不写; 断开后连接行删除, 数据与版本保留; 留痕里没有任何假密钥."""
    from backend.core.config import Settings
    from backend.main import build_deps

    engine, (synced, loaded), user = storage
    url = os.environ["TEST_DATABASE_URL"]
    deps = build_deps(Settings(_env_file=None, DATABASE_URL=url, EVIDENCE_ROOT=tmp_path, LLM_MODE="mock"))  # type: ignore[call-arg]
    data = DataService(deps, GraphRunner(cast(Any, None), deps, {"mode": "mock", "model": "mock"}))
    store = PostgresXeroConnectionStore(engine)
    fake = FakeXero()
    clock = [START]
    link = XeroLink(
        deps, data, store, settings(tmp_path), transport=httpx.MockTransport(fake), clock=lambda: clock[0].timestamp()
    )
    data.xero = link.connections

    url_ = await link.authorize(synced.id, user)
    state = httpx.URL(url_).params["state"]
    location = await link.callback(CODE, state, None)
    assert location.endswith(f"/engagements/{synced.id}/data?xero=connected")
    async with transaction(engine, synced.id) as conn:
        sealed = (await conn.execute(text("SELECT tokens_encrypted FROM xero_connections"))).scalar_one()
    assert sealed.startswith("gAAAA") and ACCESS not in sealed and REFRESH not in sealed

    await link.sync(synced.id, user)
    for task in list(data.tasks):
        await task
    assert (await deps.data.readiness(synced.id)).data_version == 1
    planning = ROOT / "src" / "backend" / "knowledge" / "planning"
    loader = XeroDatasetLoader(XERO_SNAPSHOTS, planning, engine, FileEvidenceStore(tmp_path / "loaded", engine))
    await loader.load(loaded.id, f"xero/{XERO_SNAPSHOT}")
    for kind in XERO_TABLES:
        assert await table_rows(engine, synced.id, kind.value) == await table_rows(engine, loaded.id, kind.value), kind
    assert await table_rows(engine, synced.id, "delivery_notes") == []
    source = await link.connections.source(synced.id)
    assert (source.status, source.organisation, source.scope) == ("connected", DEMO["tenantName"], list(XERO_TABLES))

    clock[0] += timedelta(minutes=5)
    assert await link.disconnect(synced.id, user)
    assert await store.get(synced.id) is None
    assert (await link.connections.source(synced.id)).status == "not_connected"
    assert (await deps.data.readiness(synced.id)).data_version == 1
    entries = await deps.trail.list(synced.id, limit=500)
    actions = {e.payload.get("action") for e in entries}
    assert {"xero_connect", "sync", "xero_disconnect"} <= actions
    assert not [s for s in SECRETS if any(s in e.model_dump_json() for e in entries)], "留痕里有假密钥"
