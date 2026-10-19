"""需求 R269 验收 9: 真实 PostgresDataStore, FileEvidenceStore 与 DataService 上的演示数据导入 (承接与外勤). 依赖按
组装根建 (测试库, 证据根为临时目录, mock 大模型读补造目录的运行副本), runner 只用来取项目锁. 设 TEST_DATABASE_URL
为已迁移到最新的独立测试库; 要本地 Xero 快照与补造目录."""

import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from backend.agents.ingest import DETAILS
from backend.contracts.data import XERO_TABLES
from backend.contracts.errors import DataRejected
from backend.contracts.models import DocumentType
from backend.orchestration.runner import GraphRunner
from backend.persistence import models as m
from backend.persistence.stores import PostgresEngagementStore, RoutingDatasetLoader, now
from backend.services.data_service import DataService
from tests.external import snapshot_dir, supplements_dir
from tests.xero_fakes import XERO_DATASET, xero_uploads
from tests.xero_flow import demo_source, import_stage

pytestmark = [pytest.mark.slow, pytest.mark.external]


async def test_demo_import_on_real_stores(tmp_path: Path) -> None:
    """两阶段后: 行数 (含服务完成记录 24 与工资册 3), 每个科目 gl_balance 等于本期试算表, 版本行为快照六张 (v2) 与
    补造四张 (v3-v6), 21 份文件的登记同清单, 抽取版本表中文件登记每份两版与合同各一版, 服务完成记录与带合同关联的
    发票可按行取; 补造目录被改过时什么都不写."""
    snapshot_dir("20261017-q1")  # 先核对数据: 跟踪了却找不到时失败, 不因没配库而跳过
    supplements_dir("20261017-q1")
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    from backend.core.config import Settings
    from backend.main import build_deps

    deps = build_deps(Settings(DATABASE_URL=url, EVIDENCE_ROOT=tmp_path / "evidence", LLM_MODE="mock"))
    service = DataService(
        deps,
        GraphRunner(cast(Any, None), deps, {"mode": "mock", "model": "mock"}),
        demo=cast(RoutingDatasetLoader, deps.datasets),
    )
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="Jordan Ellis", password_hash="x", created_at=now()
            )
        )
    store = PostgresEngagementStore(engine)
    profile = deps.datasets.client_profile(XERO_DATASET).model_copy(update={"id": "CLIENT-" + uuid4().hex})

    async def engagement() -> str:
        created = await store.create(
            owner_id=user,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
            dataset=XERO_DATASET,
            profile=profile,
        )
        return created.id

    try:
        eid = await engagement()
        assert (await import_stage(service, eid, "acceptance", user)) is not None
        assert (await import_stage(service, eid, "fieldwork", user)) is not None
        financial = deps.financial_store
        counts = {t: (await financial.query(eid, f"SELECT count(*) AS n FROM {t}", {}))[0]["n"] for t in m.FINANCIAL}
        assert counts == {
            "accounts": 62,
            "trial_balance": 33,
            "journal_entries": 1017,
            "sales_invoices": 42,
            "delivery_notes": 24,
            "bank_accounts": 2,
            "bank_transactions": 146,
            "employees": 3,
        }
        current = {r.account_code: r.balance_cents for r in await financial.trial_balance(eid, "current")}
        assert {c: v for c, v in current.items() if v != await financial.gl_balance(eid, c, date(2026, 9, 30))} == {}
        versions = await deps.data.versions(eid)
        assert sorted((v.data_version, v.kind.value, v.source, v.file_name) for v in versions) == sorted(
            [(2, k.value, "xero", None) for k in XERO_TABLES]
            + [
                (3, "sales_invoices", "import", "contract_links.csv"),
                (4, "bank_accounts", "import", "bank_accounts.csv"),
                (5, "delivery_notes", "import", "delivery_notes.csv"),
                (6, "employees", "import", "payroll/employees.csv"),
            ]
        )
        planned = {u.document_id: u for u in xero_uploads() if u.stage in ("acceptance", "fieldwork")}
        documents = await financial.documents(eid)
        assert sorted(d.id for d in documents) == sorted(planned)
        for d in documents:
            u = planned[d.id]
            assert (d.type, d.reliability, d.document_date, d.related_ids, d.uploaded_by) == (
                u.document_type,
                u.reliability,
                u.document_date,
                u.related_ids,
                user,
            ), d.file_name
            assert d.status == ("extracted" if d.type is DocumentType.contract else "classified"), d.file_name
        records = await deps.data.extraction_versions(eid)
        assert len([r for r in records if r.target_type == DETAILS]) == 42  # 每份: 补登记前一版, 补登记一版
        assert sorted(r.target_id for r in records if r.target_type == "contract") == ["CON-X01", "CON-X02", "CON-X03"]
        note = await financial.record(eid, "delivery_note", "SR-ORC1026")
        assert note is not None and note["invoice_id"] == "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611"
        invoice = await financial.record(eid, "sales_invoice", "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611")
        assert invoice is not None and invoice["contract_id"] == "CON-X01"
        assert (await deps.data.readiness(eid)).ready_at is None

        copy = tmp_path / "xero_demo"
        shutil.copytree(supplements_dir("20261017-q1"), copy / "20261017-q1")
        links = copy / "20261017-q1" / "contract_links.csv"
        links.write_bytes(links.read_bytes() + b" ")
        tampered = DataService(deps, service.runner, demo=demo_source(copy))
        other = await engagement()
        with pytest.raises(DataRejected, match=r"contract_links\.csv"):
            await tampered.demo_import(other, "fieldwork", user)
        for t in m.FINANCIAL:
            assert (await financial.query(other, f"SELECT count(*) AS n FROM {t}", {}))[0]["n"] == 0
        assert await financial.documents(other) == [] and await deps.data.versions(other) == []
    finally:
        await engine.dispose()
        await cast(Any, deps.financial_store).engine.dispose()
