"""数据管理存储 (PostgresDataStore) 的真实 PostgreSQL 测试. 设 TEST_DATABASE_URL 为已迁移到最新的独立测试库."""

import asyncio
import csv
import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.contracts.data import (
    ApprovalLogRow,
    BatchItemEdit,
    CheckId,
    DataChange,
    DataInput,
    DocumentChange,
    DocumentDetailsEdit,
    ImportCommit,
    ImportSession,
    TableKind,
)
from backend.contracts.errors import EngagementLocked
from backend.contracts.models import DocumentType, Reliability
from backend.contracts.records import Account, JournalEntry, SupplierBill
from backend.orchestration.runner import GraphRunner
from backend.persistence import models as m
from backend.persistence.data_store import PostgresDataStore
from backend.persistence.db import transaction
from backend.persistence.stores import (
    FINANCIAL_ROWS,
    OPTIONAL_CSV,
    CsvDatasetLoader,
    FileEvidenceStore,
    PostgresEngagementStore,
    PostgresFinancialDataStore,
    now,
)
from backend.services.account_mapping import with_category
from backend.services.data_service import DataService
from tests.external import supplements_dir
from tests.test_supplier_bills import Q1_BILLS

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
    engine, (first, second), loader, _ = storage
    store = PostgresDataStore(tmp_path, engine)
    before = await store.readiness(first.id)
    assert (before.data_version, before.ready_at, before.ready_by) == (0, None, None)
    assert all(s.records == 0 for s in (await store.stats(first.id)).values())

    await loader.load(first.id, "demo")

    readiness = await store.readiness(first.id)
    assert (readiness.data_version, readiness.ready_by) == (1, "dataset")
    assert (readiness.ready_reason, readiness.ready_failed_checks) == (None, [])  # 迁移 0005 的列取缺省值
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
    # 程序等数据按各类型的文件数判断 (需求 R272): 数据集载入登记 3 份合同; 没载入的项目为空
    assert await store.document_counts(first.id) == {DocumentType.contract: 3}
    assert await store.document_counts(second.id) == {}
    assert await financial.data_version(first.id) == 1


async def test_confirm_ready_keeps_first_confirmation_and_respects_lock(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    engine, (clean, second), _, engagements = storage
    store = PostgresDataStore(tmp_path, engine)

    # 第一次确认时没有未通过项, 再确认时有: 补记那次的理由与未通过项, 人与时间不改 (审核 R273-02)
    plain = await store.confirm_ready(clean.id, "U-1")
    supplemented = await store.confirm_ready(
        clean.id, "U-2", reason="差额待客户更正", failed_checks=[CheckId.debits_equal_credits]
    )
    assert (supplemented.ready_at, supplemented.ready_by) == (plain.ready_at, "U-1")
    assert (supplemented.ready_reason, supplemented.ready_failed_checks) == (
        "差额待客户更正",
        [CheckId.debits_equal_credits],
    )
    assert await store.confirm_ready(clean.id, "U-3", reason="x", failed_checks=[CheckId.journals_agree_tb]) == (
        supplemented
    )

    failed = [CheckId.debits_equal_credits, CheckId.journals_agree_tb]
    first = await store.confirm_ready(second.id, "U-1", reason="差额待客户更正", failed_checks=failed)
    again = await store.confirm_ready(second.id, "U-2", reason="另一个理由", failed_checks=[])

    assert (first.data_version, first.ready_by) == (0, "U-1")
    assert (first.ready_reason, first.ready_failed_checks) == ("差额待客户更正", failed)
    assert again == first  # 已确认的不改: 人, 时间, 理由与未通过项都是第一次的
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
    # 标客户无此类资料 (需求 R280): 迁移 0006 的两列往返, 只改仍为 open 的这一个请求; 并发标同一条只有一方改成
    [request_a] = [r for r in await store.requests(first.id) if r.id == "REQ-a"]
    assert (request_a.resolved_by, request_a.unavailable_reason) == (None, None)
    results = await asyncio.gather(
        *(store.mark_unavailable(first.id, "REQ-a", reason=f"客户没有 {n}", actor=f"U-{n}", at=at) for n in (1, 2))
    )
    [marked] = [r for r in results if r is not None]
    n = 1 if results[0] is not None else 2
    assert marked == request_a.model_copy(
        update={
            "status": "unavailable",
            "resolved_at": at,
            "resolved_by": f"U-{n}",
            "unavailable_reason": f"客户没有 {n}",
        }
    )
    assert {r.id: r for r in await store.requests(first.id)}["REQ-a"] == marked
    assert await store.open_requests(first.id) == 1
    for eid, rid in ((first.id, "REQ-a"), (first.id, "REQ-b"), (first.id, "REQ-none"), (second.id, "REQ-c")):
        # 已标的, 已收到的, 不存在的, 别的项目的 (行级安全) 都不改
        assert await store.mark_unavailable(eid, rid, reason="再标", actor="U-3", at=at) is None
    assert {r.id: r.status for r in await store.requests(first.id)} == {
        "REQ-a": "unavailable",
        "REQ-b": "received",
        "REQ-c": "open",
    }


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


async def test_csv_dataset_may_omit_only_the_supplier_bills(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """CSV 数据集可以没有 supplier_bills.csv (Numbat 即是), 有就照样载入; 其余七张缺一张仍报错, 也不写入 (需求 R178)."""
    engine, (e, _), _, _ = storage
    copy = tmp_path / "datasets" / "demo"
    copy.mkdir(parents=True)
    for path in DEMO.glob("*.csv"):
        shutil.copy(path, copy)
    loader = CsvDatasetLoader(tmp_path / "datasets", engine, FileEvidenceStore(tmp_path / "evidence", engine))
    (copy / "bank_accounts.csv").rename(copy / "bank_accounts.bak")
    with pytest.raises(FileNotFoundError, match=r"bank_accounts\.csv"):
        await loader.load(e.id, "demo")
    assert await table_rows(engine, e.id, "accounts") == []
    (copy / "bank_accounts.bak").rename(copy / "bank_accounts.csv")
    with (copy / "supplier_bills.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(SupplierBill.model_fields))
        writer.writeheader()
        writer.writerows({k: "" if v is None else v for k, v in b.model_dump(mode="json").items()} for b in Q1_BILLS)
    await loader.load(e.id, "demo")
    assert [SupplierBill.model_validate(r) for r in await table_rows(engine, e.id, "supplier_bills")] == sorted(
        Q1_BILLS, key=lambda b: b.id
    )


async def test_importing_demo_csvs_matches_dataset_loader(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """验收: data/demo 的 7 个 CSV 依次经 POST /data/imports 与 commit 的流程导入 (按系统建议的表种类, 对应与日期写法),
    结果与 CsvDatasetLoader 载入的一致; 每次导入数据版本 +1, 原文件登记为证据文件."""
    engine, (imported, loaded), loader, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    names = [name for name in FINANCIAL_ROWS if name not in OPTIONAL_CSV]  # Numbat 没有应付账单明细 (需求 R178)
    assert len(names) == 7
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

    for name in FINANCIAL_ROWS:
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


# ── 需求 R14: 上传文件的处理与抽取结果的版本 ──


async def test_uploaded_real_contract_matches_doc04_and_versions(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """验收: 真实存储上传演示 CON-001.pdf, A3 分类并抽取 (演示固定输出), 合同与编排里 DOC-04 对数据集登记的同一份
    合同的抽取一致 (只差执行编号); 改条款文本出第 2 版, 历史两版; 文件登记的类型与状态按处理结果更新."""
    from dataclasses import replace

    from backend.agents.ingest import IngestAgent
    from backend.contracts.data import ExtractionEdit
    from backend.services.judgment_engine import JudgmentEngine, OutputValidator
    from tests.fakes import LayeredMockLLM, make_ctx

    _, (uploaded, loaded), loader, engagements = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    demo_llm = ROOT / "data" / "demo" / "llm"
    llm = LayeredMockLLM(demo_llm)
    deps = service.deps
    judgments = JudgmentEngine(
        llm, OutputValidator(deps.financial_store, deps.library, deps.trail), deps.trail, deps.library
    )
    deps = replace(deps, llm=llm, judgments=judgments)
    service = DataService(deps, service.runner)
    pdf = (DEMO / "contracts" / "CON-001.pdf").read_bytes()

    [received] = await service.upload_documents(uploaded.id, [("CON-001.pdf", pdf)], actor="U-1")
    await asyncio.gather(*list(service.tasks))

    [doc] = await deps.financial_store.documents(uploaded.id)
    assert (doc.id, doc.type, doc.status, doc.reliability, doc.page_count) == (
        received.id,
        DocumentType.contract,
        "extracted",
        Reliability.external_copy,
        3,
    )
    [contract] = await deps.financial_store.contracts(uploaded.id)
    await loader.load(loaded.id, "demo")  # 数据集登记同一份 PDF, 编排里的 DOC-04 抽取
    engagement = await engagements.get(loaded.id)
    assert engagement is not None
    await IngestAgent(deps).run(make_ctx(deps, "DOC-04", engagement))
    expected = next(c for c in await deps.financial_store.contracts(loaded.id) if c.id == "CON-001")
    assert contract.model_copy(update={"extracted_by_run": "X"}) == expected.model_copy(
        update={"extracted_by_run": "X"}
    )

    edit = ExtractionEdit(field="terms[0].text", value="Changed by auditor.", reason="原文")
    await service.edit_contract(uploaded.id, "CON-001", edit, "U-1")
    history = await service.contract_history(uploaded.id, "CON-001")
    assert [(v.version, v.changed_by, v.field) for v in history] == [(1, "A3", None), (2, "U-1", "terms[0].text")]
    assert history[0].contract == contract
    # 上传后抽出合同 (需求 R274: 程序执行之外写进的合同记数据变动) 0 → 1, 修改抽取结果 1 → 2, 各记 contracts
    assert (await deps.data.readiness(uploaded.id)).data_version == 2
    assert [(c.data_version, c.input) for c in await deps.data.changes(uploaded.id)] == [
        (1, DataInput.contracts),
        (2, DataInput.contracts),
    ]


async def test_contract_edit_rolls_back_together_when_version_bump_fails(
    storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """修改合同时数据版本写失败: 合同新版本与版本记录在同一事务里一起回滚 (审核 FD-R14 第 1 轮)."""
    from backend.contracts.data import ExtractionEdit
    from backend.contracts.models import Contract

    _, (first, _), _, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    contract = Contract(
        id="CON-777",
        document_id="DOC-X",
        counterparty="Before",
        counterparty_abn=None,
        amount_cents=None,
        sign_date=None,
        terms=[],
        extracted_by_run="RUN-DOC-04-1",
    )
    await service.deps.financial_store.save_contract(first.id, contract)

    async def broken(eid: str, input: DataInput) -> int:
        raise RuntimeError("数据版本写失败")

    monkeypatch.setattr(service.deps.data, "bump_version", broken)
    with pytest.raises(RuntimeError):
        await service.edit_contract(
            first.id, "CON-777", ExtractionEdit(field="counterparty", value="After", reason="r"), "U-1"
        )

    [current] = await service.deps.financial_store.contracts(first.id, ["CON-777"])
    assert (current.version, current.counterparty) == (1, "Before")
    assert [v.version for v in await service.deps.financial_store.contract_history(first.id, "CON-777")] == [1]
    assert (await service.deps.data.readiness(first.id)).data_version == 0


async def test_contract_terms_read_in_extraction_order_and_edit_retry(
    storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """11 条条款: 读出按 T1, T2, ..., T11 (不按文本排序); 留痕失败后同一修改重试返回当前版本 (审核 FD-R14 第 2 轮)."""
    from backend.contracts.data import ExtractionEdit
    from backend.contracts.models import Contract, ContractTerm, TermType, TrailKind

    _, (first, _), _, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    terms = [
        ContractTerm(id=f"CON-778-T{n}", term_type=TermType.payment, text=f"Clause {n}", page=1) for n in range(1, 12)
    ]
    contract = Contract(
        id="CON-778",
        document_id="DOC-X",
        counterparty="Before",
        counterparty_abn=None,
        amount_cents=None,
        sign_date=None,
        terms=terms,
        extracted_by_run="RUN-DOC-04-1",
    )
    await service.deps.financial_store.save_contract(first.id, contract)
    [stored] = await service.deps.financial_store.contracts(first.id, ["CON-778"])
    assert [t.id for t in stored.terms] == [t.id for t in terms]

    write = service.deps.trail.record

    async def no_edit_trail(entry):  # type: ignore[no-untyped-def]
        if entry.kind is TrailKind.data and entry.payload.get("action") == "edit":
            raise RuntimeError("留痕写失败")
        return await write(entry)

    edit = ExtractionEdit(field="terms[9].text", value="Clause ten revised", reason="原文")
    monkeypatch.setattr(service.deps.trail, "record", no_edit_trail)
    with pytest.raises(RuntimeError):
        await service.edit_contract(first.id, "CON-778", edit, "U-1")
    monkeypatch.setattr(service.deps.trail, "record", write)
    [current] = await service.deps.financial_store.contracts(first.id, ["CON-778"])
    assert (current.version, current.terms[9].id, current.terms[9].text) == (2, "CON-778-T10", "Clause ten revised")

    assert await service.edit_contract(first.id, "CON-778", edit, "U-1") == current
    history = await service.deps.financial_store.contract_history(first.id, "CON-778")
    assert [v.version for v in history] == [1, 2] and history[0].contract == contract


async def test_checkup_on_real_store_matches_rules_and_accepts_requests(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """需求 R15 (真实库): check_inputs 的 SQL 汇总与 data_quality.summarize 一致; 验收: Numbat 经 Xero 同步后出现
    审批日志, 报表草稿两条请求, 导入审批日志与上传报表草稿后都转为 received; 体检与请求留痕不重复."""
    import asyncio

    from backend.adapters.xero_dataset import CREATED_BY
    from backend.contracts.models import TrailKind
    from tests.fakes import numbat_inputs, numbat_tables
    from tests.test_data_checkup import approval_log_csv

    _, (synced, loaded), loader, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    period = (date(2025, 7, 1), date(2026, 6, 30))
    await loader.load(loaded.id, "demo")
    assert await service.deps.data.check_inputs(loaded.id, *period) == numbat_inputs(*period)

    tables = {kind: list(numbat_tables()[kind]) for kind in (TableKind.accounts, TableKind.trial_balance)}
    tables[TableKind.journal_entries] = [
        cast(JournalEntry, j).model_copy(update={"created_by": CREATED_BY, "approved_by": None})
        for j in numbat_tables()[TableKind.journal_entries]
    ]
    async with service.holding(synced.id, [k.value for k in tables]):
        await service.write_tables(synced.id, tables, source="xero", actor="U-1")
    requests = await service.deps.data.requests(synced.id)
    assert [(r.id, r.reason_code, r.reason_params, r.status) for r in requests] == [
        ("REQ-approval_log", "field_unavailable", {"fields": ["created_by", "approved_by"], "source": "Xero"}, "open"),
        ("REQ-financial_statements", "not_provided", {}, "open"),
    ]
    run = await service.deps.data.check_run(synced.id)
    assert run is not None and run.data_version == 1
    assert {c.id.value: c.status for c in run.checks}["journals_agree_tb"] == "passed"
    assert await service.deps.data.open_requests(synced.id) == 2

    preview = await service.start_import(
        synced.id, "approval_log.csv", approval_log_csv(), actor="U-1", request_id="REQ-approval_log"
    )
    mapping = {"entry_no": "entry_no", "created_by": "created_by", "approved_by": "approved_by"}
    body = ImportCommit(kind=TableKind.approval_log, sheet=preview.sheet, date_format="ymd", mapping=mapping)
    await service.commit_import(synced.id, preview.import_id, body, "U-1")
    await service.upload_documents(
        synced.id, [("draft statements.pdf", b"%PDF draft")], actor="U-1", request_id="REQ-financial_statements"
    )
    await asyncio.gather(*list(service.tasks))

    assert {r.id: r.status for r in await service.deps.data.requests(synced.id)} == {
        "REQ-approval_log": "received",
        "REQ-financial_statements": "received",
    }
    assert await service.deps.data.open_requests(synced.id) == 0
    checks = {c.id.value: c.status for c in await service.deps.data.checks(synced.id)}
    assert (checks["preparer_approver_available"], checks["tb_agrees_statements"]) == ("passed", "pending")

    async def checkup_actions() -> list[str]:
        entries = await service.deps.trail.list(synced.id, TrailKind.data, limit=500)
        return [
            e.payload["action"]
            for e in entries
            if e.payload["action"] in {"check", "request_raised", "request_received"}
        ]

    before = await checkup_actions()
    async with service.holding(synced.id, []):  # 补写: 同键已有的不再写
        pass
    assert await checkup_actions() == before
    assert before.count("check") == 3 and before.count("request_raised") == 2 and before.count("request_received") == 2


async def test_table_view_and_category_edit_on_real_store(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """需求 R16 (真实库): 数据集载入的科目表带两期余额; 搜索的 % 与 _ 按字面匹配; 验收: 6300 改为 cost_of_sales,
    历史两版, 留痕一条, 数据版本 +1; 重新导入科目表保留审计师的分类; 审批日志取最新一版的行快照."""
    from backend.contracts.data import ApprovalLogRow
    from backend.contracts.models import TrailKind

    _, (loaded, _), loader, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    await loader.load(loaded.id, "demo")
    eid = loaded.id

    page = await service.table_page(eid, TableKind.accounts, "freight", 0, 50)
    assert page.rows == [
        {
            "code": "6300",
            "name": "Freight outwards",
            "type": "expense",
            "category": "operating_expenses",
            "category_basis": "name",
            "balance_current_cents": 24000000,
            "balance_prior_cents": 20500000,
            "edited": False,
        }
    ]
    assert (await service.table_page(eid, TableKind.accounts, None, 0, 50)).total == 23
    assert (await service.table_page(eid, TableKind.journal_entries, "%", 0, 5)).total == 0  # 按字面, 不是通配
    journals = await service.table_page(eid, TableKind.journal_entries, "koala", 0, 3)
    assert journals.total > 0 and len(journals.rows) == 3
    assert isinstance(journals.rows[0]["posting_date"], date) and journals.rows[0]["debit_cents"] is not None

    version = await service.set_account_category(eid, "6300", "cost_of_sales", "运费属于销售成本", "U-1")
    history = await service.account_history(eid, "6300")
    assert [(v.version, v.category, v.basis, v.changed_by) for v in history] == [
        (1, "operating_expenses", "name", "A3"),
        (2, "cost_of_sales", "auditor", "U-1"),
    ]
    assert version == history[-1]
    assert (await service.deps.data.readiness(eid)).data_version == 2
    entries = await service.deps.trail.list(eid, TrailKind.data, limit=500)
    assert [e.idempotency_key for e in entries if e.payload["action"] == "edit"] == [f"{eid}:data:edit:account:6300:v2"]
    [row] = (await service.table_page(eid, TableKind.accounts, "6300", 0, 50)).rows
    assert (row["category"], row["category_basis"], row["edited"]) == ("cost_of_sales", "auditor", True)

    async with service.holding(eid, ["accounts"]):  # 重新导入科目表: 系统按名称归类, 审计师改过的保留
        accounts = [with_category(Account.model_validate(r)) for r in csv_rows("accounts")]
        await service.write_tables(eid, {TableKind.accounts: accounts}, source="import", actor="U-1")
    [row] = (await service.table_page(eid, TableKind.accounts, "6300", 0, 50)).rows
    assert (row["category"], row["edited"]) == ("cost_of_sales", True)
    assert [v.version for v in await service.account_history(eid, "6300")] == [1, 2]

    log = [ApprovalLogRow(entry_no="CP-AP-2025-07-A", approved_by="cfo")]
    async with service.holding(eid, ["approval_log"]):
        await service.write_tables(eid, {TableKind.approval_log: log}, source="import", actor="U-1")
    approvals = await service.table_page(eid, TableKind.approval_log, "cfo", 0, 50)
    assert approvals.total == 1 and approvals.rows[0]["entry_no"] == "CP-AP-2025-07-A"


async def test_table_pages_do_not_repeat_or_skip_rows_with_duplicate_numbers(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """发票号与开票日期相同, 发运单号相同的行: 逐页 (每页 1 行) 查看, 各页合起来不重不漏 (审核 FD-R16 第 1 轮)."""
    from backend.contracts.records import DeliveryNote, SalesInvoice

    _, (_, eid_holder), _, _ = storage
    eid = eid_holder.id
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    invoices = [
        SalesInvoice(
            id=f"SI-{n}",
            invoice_no="INV-1",
            customer="Same Pty Ltd",
            customer_abn=None,
            contract_id=None,
            invoice_date=date(2026, 3, 1),
            posting_date=date(2026, 3, 1),
            amount_cents=100 * n,
            journal_entry_id=None,
        )
        for n in (3, 1, 4, 2, 5)
    ]
    notes = [
        DeliveryNote(
            id=f"DN-{n}", delivery_no="DN-1", invoice_id=f"SI-{n}", shipped_date=None, signed_date=None, signed_by=None
        )
        for n in (2, 5, 1, 4, 3)
    ]
    tables: dict[TableKind, list[Any]] = {TableKind.sales_invoices: invoices, TableKind.delivery_notes: notes}
    async with service.holding(eid, [k.value for k in tables]):
        await service.write_tables(eid, tables, source="import", actor="U-1")

    for kind in tables:
        pages = [await service.table_page(eid, kind, None, offset, 1) for offset in range(5)]
        ids = [row["id"] for page in pages for row in page.rows]
        assert ids == sorted(ids) and len(set(ids)) == 5 and all(p.total == 5 for p in pages), kind


# ── 迁移 0006 往返 (需求 R280, 审核 R280-04) ──


def migration(name: str) -> Any:
    """按文件名载入一个迁移模块 (文件名以数字开头, 不能直接 import)."""
    import importlib.util

    path = ROOT / "src" / "backend" / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_migration_0006_round_trip(storage) -> None:  # type: ignore[no-untyped-def]
    """以属主角色在一个事务里走 0006 降级再升级, 最后回滚 (不动测试库的表结构与 alembic 版本): 降级后标了客户无此类
    资料的请求改回 open (0005 的代码只认 open, received, withdrawn), 理由与标记人随两列删掉; 再升级两列回来, 为空.
    需另设 TEST_DATABASE_OWNER_URL (测试库的属主角色)."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    _, (first, _second), _, _ = storage
    module = migration("0006_request_unavailable")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    engine = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    at = now()
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
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                await conn.execute(text("SELECT set_config('app.engagement_id', :eid, true)"), {"eid": first.id})
                marked = {"resolved_at": at, "resolved_by": "U-1", "unavailable_reason": "客户没有"}
                for row in (
                    {**base, "id": "REQ-open", "status": "open"},
                    {**base, "id": "REQ-got", "status": "received", "resolved_at": at},
                    {**base, "id": "REQ-none", "status": "unavailable", **marked},
                ):  # 各行的列不同, 逐行插入
                    await conn.execute(insert(m.data_requests).values(**row))
                found = await conn.execute(
                    text("SELECT resolved_by, unavailable_reason FROM data_requests WHERE id = 'REQ-none'")
                )
                assert found.one() == ("U-1", "客户没有")

                async def columns() -> set[str]:
                    found = await conn.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'data_requests'")
                    )
                    return {row[0] for row in found}

                async def rows() -> dict[str, tuple[Any, ...]]:
                    found = await conn.execute(
                        text("SELECT id, status, resolved_at IS NOT NULL FROM data_requests WHERE engagement_id = :e"),
                        {"e": first.id},
                    )
                    return {row[0]: tuple(row[1:]) for row in found}

                # 迁移时不设 app.engagement_id (同 alembic upgrade 命令), 降级要改全部项目的行
                await conn.execute(text("SELECT set_config('app.engagement_id', '', true)"))
                await conn.run_sync(run("downgrade"))
                await conn.execute(text("SELECT set_config('app.engagement_id', :eid, true)"), {"eid": first.id})

                async def row_security() -> tuple[Any, ...]:
                    found = await conn.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE oid = 'data_requests'::regclass"
                        )
                    )
                    return tuple(found.one())

                assert await row_security() == (True, True)  # 降级时暂时关掉的 FORCE 已恢复 (审核 R280-09)
                assert not {"resolved_by", "unavailable_reason"} & await columns()
                assert await rows() == {
                    "REQ-open": ("open", False),
                    "REQ-got": ("received", True),
                    "REQ-none": ("open", False),
                }
                await conn.run_sync(run("upgrade"))
                assert {"resolved_by", "unavailable_reason"} <= await columns()
                found = await conn.execute(
                    text("SELECT id, resolved_by, unavailable_reason FROM data_requests WHERE engagement_id = :e"),
                    {"e": first.id},
                )
                assert {row[0]: (row[1], row[2]) for row in found} == {
                    "REQ-open": (None, None),
                    "REQ-got": (None, None),
                    "REQ-none": (None, None),
                }
                await conn.run_sync(run("upgrade"))  # 可重复执行
                assert await row_security() == (True, True)
            finally:
                await outer.rollback()
        async with engine.connect() as conn:
            assert {"resolved_by", "unavailable_reason"} <= await columns_of(conn)
            version = await conn.execute(text("SELECT version_num FROM alembic_version"))
            assert version.scalar_one() >= "0006"
    finally:
        await engine.dispose()


async def columns_of(conn: Any) -> set[str]:
    from sqlalchemy import text

    found = await conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'data_requests'")
    )
    return {row[0] for row in found}


# ── 迁移在 FORCE 行级安全下改数据: 共用工具与 0007 回填 (需求 R283) ──


async def test_backfill_0007_fixes_only_rows_0004_missed(storage) -> None:  # type: ignore[no-untyped-def]
    """以属主角色在一个事务里跑 0007 的 upgrade (两次) 后回滚, 不动测试库的表结构与 alembic 版本. 第一个项目放
    "当年漏改的行" (0004 之前的数据集载入: ready_at 为空且没有 data_version 1 的已提交导入; CSH-04 存档的回函: channel
    仍为 auditor_upload, 没有上传人), 第二个项目放 "之后正常产生的行" (经导入接入而未确认就绪的项目, 审计师上传的
    回函, 银行直达回函, 数据集载入的合同). 回填只改前者, 第二次是空操作, 三张表的 FORCE 行级安全都已恢复. 去掉
    "关掉 FORCE 行级安全" 时属主看不到任何行, 前者不会被改, 这条失败. 需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    _, (old, new), _, _ = storage
    module = migration("0007_backfill_force_rls")

    def upgrade(sync_conn: Any) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            module.upgrade()

    loaded = datetime(2026, 1, 5, tzinfo=UTC)
    engine = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:

                async def as_project(eid: str | None) -> None:
                    await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": eid or ""})

                async def document(eid: str, doc_id: str, kind: str, channel: str, uploader: str | None) -> None:
                    await as_project(eid)
                    row = {
                        "engagement_id": eid,
                        "id": doc_id,
                        "type": kind,
                        "file_name": f"{doc_id}.pdf",
                        "file_path": f"{doc_id}.pdf",
                        "sha256": doc_id,
                        "page_count": 1,
                        "reliability": "external_copy",
                        "status": "extracted",
                        "uploaded_at": loaded,
                        "media_type": "application/pdf",
                        "channel": channel,
                        "uploaded_by": uploader,
                    }
                    await conn.execute(insert(m.documents).values(**row))

                await as_project(old.id)
                await conn.execute(
                    insert(m.dataset_loads).values(
                        engagement_id=old.id, dataset="demo", data_version=2, loaded_at=loaded
                    )
                )
                await document(old.id, "DOC-OLD-REPLY", "confirmation", "auditor_upload", None)
                await as_project(new.id)
                await conn.execute(
                    insert(m.dataset_loads).values(
                        engagement_id=new.id, dataset="demo", data_version=1, loaded_at=loaded
                    )
                )
                for version, status in ((1, "committed"), (None, "parsed")):
                    await conn.execute(
                        insert(m.data_imports).values(
                            engagement_id=new.id,
                            id=f"IMP-{status}",
                            kind="accounts",
                            source="import",
                            status=status,
                            data_version=version,
                            created_by="U-1",
                            created_at=loaded,
                        )
                    )
                await document(new.id, "DOC-UP-REPLY", "confirmation", "auditor_upload", "U-1")
                await document(new.id, "DOC-BANK-REPLY", "confirmation", "bank_direct", None)
                await document(new.id, "DOC-SEED-CONTRACT", "contract", "auditor_upload", None)

                async def state() -> dict[str, Any]:
                    found: dict[str, Any] = {}
                    for eid in (old.id, new.id):
                        await as_project(eid)
                        [load] = (await conn.execute(text("SELECT ready_at, ready_by FROM dataset_loads"))).all()
                        found[eid] = tuple(load)
                        for doc_id, channel in await conn.execute(text("SELECT id, channel FROM documents")):
                            found[doc_id] = channel
                    return found

                async def forced() -> set[tuple[Any, ...]]:
                    found = await conn.execute(
                        text(
                            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname IN ('dataset_loads', 'data_imports', 'documents')"
                        )
                    )
                    return {tuple(row) for row in found}

                await as_project(None)  # 同真实迁移: 不设项目
                await conn.run_sync(upgrade)
                after = await state()
                assert after == {
                    old.id: (loaded, "dataset"),  # 0004 之前的数据集载入: 视同已确认
                    "DOC-OLD-REPLY": "bank_direct",  # CSH-04 存档的银行直达回函
                    new.id: (None, None),  # 经导入接入, 审计师还没确认: 不动
                    "DOC-UP-REPLY": "auditor_upload",
                    "DOC-BANK-REPLY": "bank_direct",
                    "DOC-SEED-CONTRACT": "auditor_upload",
                }
                assert await forced() == {(name, True, True) for name in ("dataset_loads", "data_imports", "documents")}
                await as_project(None)
                await conn.run_sync(upgrade)  # 重复执行无副作用
                assert await state() == after
                assert await forced() == {(name, True, True) for name in ("dataset_loads", "data_imports", "documents")}
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


def test_all_projects_rejects_tables_without_project_isolation() -> None:
    """工具只收按项目隔离的表, 写错表名在执行任何语句之前报错."""
    from backend.persistence.migration_tools import all_projects

    with pytest.raises(ValueError, match="users, dataset_load"), all_projects("data_requests", "users", "dataset_load"):
        pass


async def test_data_changes_record_what_each_version_changed(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """需求 R274: 数据版本每 +1 都在 data_changes 记下变了哪类数据 (过期判断只读 changes): 数据集载入为版本 1 的
    全部各类; 写入账表记各表, 审批日志另记总账分录; 改科目分类记 accounts; bump_version 记所给的种类 (改合同, 回函);
    按版本升序, 同版本按 DataInput 顺序. 另一个项目读不到 (行级安全)."""
    engine, (first, other), loader, _ = storage
    store = PostgresDataStore(tmp_path, engine)
    await loader.load(first.id, "demo")
    assert await store.changes(first.id) == [DataChange(data_version=1, input=i) for i in DataInput]

    accounts = [
        Account(code="6300", name="Freight", type="expense", category="operating_expenses", category_basis="name")
    ]
    assert await store.write_tables(first.id, {TableKind.accounts: accounts}, source="import", actor="U-1") == 2
    log = [ApprovalLogRow(entry_no="J1", approved_by="cfo")]
    assert await store.write_tables(first.id, {TableKind.approval_log: log}, source="import", actor="U-1") == 3
    await store.set_category(first.id, "6300", "cost_of_sales", actor="U-1", reason="运费计入销售成本")
    assert await store.bump_version(first.id, DataInput.contracts) == 5
    assert await store.bump_version(first.id, DataInput.confirmations) == 6

    later = [(c.data_version, c.input) for c in await store.changes(first.id) if c.data_version > 1]
    assert later == [
        (2, DataInput.accounts),
        (3, DataInput.journal_entries),
        (3, DataInput.approval_log),
        (4, DataInput.accounts),
        (5, DataInput.contracts),
        (6, DataInput.confirmations),
    ]
    assert (await store.readiness(first.id)).data_version == 6
    assert await store.changes(other.id) == []
    # 补登记记 documents 带文件类型 (需求 R304): 按 DocumentType 顺序去重; 其余种类与数据集载入的 documents 不带
    kinds = [DocumentType.confirmation, DocumentType.contract, DocumentType.confirmation]
    assert await store.bump_version(first.id, DataInput.documents, document_types=kinds) == 7
    changes = await store.changes(first.id)
    assert changes[-1] == DataChange(
        data_version=7, input=DataInput.documents, document_types=[DocumentType.contract, DocumentType.confirmation]
    )
    assert {c.document_types for c in changes[:-1]} == {None}


async def test_migration_0008_backfills_data_changes_under_force_rls(storage) -> None:  # type: ignore[no-untyped-def]
    """需求 R274 (第 2 轮 R274-14): 以属主角色在一个事务里跑 0008 的 upgrade 后回滚, 不动测试库的表结构与 alembic 版本.
    已有导入记录的项目补到了行 (版本 1 查不到种类记全部各类, 版本 2 科目表, 版本 3 审批日志另记总账分录), 没有
    dataset_loads 的项目不补; 迁移不设 app.engagement_id (all_projects 块内暂时关掉 FORCE), 三张表的 FORCE 行级安全
    事后都已恢复; 再跑一次不多出行. 需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    _, (first, second), _, _ = storage
    module = migration("0008_data_changes")

    def upgrade(sync_conn: Any) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            module.upgrade()

    at = now()
    engine = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:

                async def as_project(eid: str) -> None:
                    await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": eid})

                await as_project(first.id)
                await conn.execute(
                    insert(m.dataset_loads).values(engagement_id=first.id, dataset="demo", data_version=3, loaded_at=at)
                )
                for n, (version, kind) in enumerate(((2, "accounts"), (3, "approval_log")), start=1):
                    await conn.execute(
                        insert(m.data_imports).values(
                            engagement_id=first.id,
                            id=f"IMP-BACKFILL-{n}",
                            kind=kind,
                            source="import",
                            status="committed",
                            data_version=version,
                            created_by="U-1",
                            created_at=at,
                        )
                    )
                await conn.execute(text("DELETE FROM data_changes"))  # 迁移之前的库上没有这些行
                await as_project("")
                await conn.run_sync(upgrade)
                assert (await conn.execute(text("SELECT current_setting('app.engagement_id', true)"))).scalar() == ""

                async def changes(eid: str) -> list[tuple[int, str]]:
                    await as_project(eid)
                    found = await conn.execute(
                        text("SELECT data_version, input FROM data_changes ORDER BY data_version, input")
                    )
                    return [(int(v), str(i)) for v, i in found]

                every = sorted(i.value for i in DataInput)
                expected = [(1, i) for i in every] + [(2, "accounts"), (3, "approval_log"), (3, "journal_entries")]
                assert await changes(first.id) == expected
                assert await changes(second.id) == []
                forced = await conn.execute(
                    text(
                        "SELECT relname, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('data_changes', 'dataset_loads', 'data_imports')"
                    )
                )
                assert {row[1] for row in forced} == {True}
                await as_project("")
                await conn.run_sync(upgrade)  # 可重复执行
                assert await changes(first.id) == expected
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


# ── 需求 R277: 智能导入的批次 ──


async def test_import_batch_on_real_store(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """真实存储上的一批 (两张 Numbat CSV, 演示 CON-001.pdf 与两份回函副本): 文件暂存 (staged), 批次按 ID 存取且按
    项目隔离; 确认前财务表, 合同与抽取结果不变; 确认的提交一个事务写成, 其中一步出错 (一份不存在的文件) 时账表, 合同,
    文件登记与批次都不变; 确认后两张表一次写入 (一个数据版本, 各表版本行的原文件名与证据文件取各自的文件), 合同写入,
    回函记 reply 抽取结果, 文件按类型登记, 取消勾选的回函转为未导入, 这一版的数据变动记两张表与合同 (需求 R274);
    已 committed 的批次再提交什么都不写."""
    from dataclasses import replace

    from backend.services.judgment_engine import JudgmentEngine, OutputValidator
    from tests.fakes import LayeredMockLLM

    _, (first, second), _, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    llm = LayeredMockLLM(ROOT / "data" / "demo" / "llm")  # 演示运行副本: 账表识别, 文件分类与合同抽取
    deps = service.deps
    judgments = JudgmentEngine(
        llm, OutputValidator(deps.financial_store, deps.library, deps.trail), deps.trail, deps.library
    )
    service = DataService(replace(deps, llm=llm, judgments=judgments), service.runner)
    files = [
        ("accounts.csv", (DEMO / "accounts.csv").read_bytes()),
        ("bank_accounts.csv", (DEMO / "bank_accounts.csv").read_bytes()),
        ("CON-001.pdf", (DEMO / "contracts" / "CON-001.pdf").read_bytes()),
        ("12345601.pdf", (DEMO / "confirmations" / "12345601.pdf").read_bytes()),  # 回函副本
        ("23456702.pdf", (DEMO / "confirmations" / "23456702.pdf").read_bytes()),  # 取消勾选, 未导入
    ]

    started = await service.start_batch(first.id, files, actor="U-1")
    await asyncio.gather(*list(service.tasks))
    batch = await deps.data.batch(first.id, started.id)
    assert batch is not None and batch.status == "ready" and len(batch.items) == 5
    assert [i.selected for i in batch.items] == [True] * 5
    assert batch.items[2].contract is not None and batch.items[2].contract.id == "CON-001"
    assert batch.items[3].document_type is DocumentType.confirmation and batch.items[3].reply is not None
    batch = await service.edit_batch_item(first.id, started.id, batch.items[4].id, BatchItemEdit(selected=False), "U-1")
    assert await deps.data.batch(second.id, started.id) is None  # 行级安全
    assert [b.id for b in await deps.data.batches(first.id)] == [started.id]
    documents = await deps.financial_store.documents(first.id)
    assert {d.status for d in documents} == {"staged"}
    assert await deps.financial_store.contracts(first.id) == []
    assert await deps.data.versions(first.id) == [] and await deps.data.extraction_versions(first.id, "reply") == []

    tables, origins = await service._batch_tables(first.id, batch, batch.items[:2])
    changes = await service._batch_documents(first.id, batch, batch.items[:2], batch.items[2:4])
    assert [c.status for c in changes] == ["extracted"] * 4 + ["not_imported"] and changes[2].contracts
    missing = DocumentChange(
        document_id="DOC-missing", type=DocumentType.other, reliability=Reliability.internal_copy, status="classified"
    )
    committed = batch.model_copy(
        update={"status": "committed", "committed_by": "U-1", "committed_at": datetime.now(UTC)}
    )
    with pytest.raises(FileNotFoundError):
        await deps.data.commit_batch(
            first.id, committed, tables, actor="U-1", origins=origins, documents=[*changes, missing]
        )
    assert await deps.data.versions(first.id) == [] and await deps.financial_store.contracts(first.id) == []
    assert {d.status for d in await deps.financial_store.documents(first.id)} == {"staged"}
    assert (await deps.data.batch(first.id, started.id)) == batch
    assert (await deps.data.readiness(first.id)).data_version == 0
    assert (await deps.data.stats(first.id))[TableKind.accounts].records == 0

    done = await service.confirm_batch(first.id, started.id, "U-1")

    assert (done.status, done.data_version) == ("committed", 1)
    assert await deps.data.batch(first.id, started.id) == done
    by_name = {d.file_name: d for d in await deps.financial_store.documents(first.id)}
    assert [(v.kind, v.file_name, v.document_id, v.data_version) for v in await deps.data.versions(first.id)] == [
        (TableKind.accounts, "accounts.csv", by_name["accounts.csv"].id, 1),
        (TableKind.bank_accounts, "bank_accounts.csv", by_name["bank_accounts.csv"].id, 1),
    ]
    assert (by_name["CON-001.pdf"].type, by_name["CON-001.pdf"].status) == (DocumentType.contract, "extracted")
    assert by_name["accounts.csv"].status == "extracted"
    assert [c.id for c in await deps.financial_store.contracts(first.id)] == ["CON-001"]
    assert (by_name["12345601.pdf"].type, by_name["12345601.pdf"].status) == (DocumentType.confirmation, "extracted")
    [reply] = await deps.data.extraction_versions(first.id, "reply")
    assert (reply.target_id, reply.version, reply.data) == (by_name["12345601.pdf"].id, 1, batch.items[3].reply)
    assert (by_name["23456702.pdf"].type, by_name["23456702.pdf"].status) == (DocumentType.other, "not_imported")
    assert [f.not_imported for f in done.files] == [False, False, False, False, True]
    assert (await deps.data.stats(first.id))[TableKind.accounts].records == 23
    changed = [DataInput.accounts, DataInput.bank_accounts, DataInput.contracts]
    assert await deps.data.changes(first.id) == [DataChange(data_version=1, input=i) for i in changed]
    again = await deps.data.commit_batch(first.id, committed, tables, actor="U-1", origins=origins, documents=changes)
    assert again == done and len(await deps.data.versions(first.id)) == 2
    assert (await deps.data.readiness(first.id)).data_version == 1
    assert len(await deps.data.changes(first.id)) == 3


async def test_migration_0011_round_trip(storage) -> None:  # type: ignore[no-untyped-def]
    """需求 R288: 以属主角色在一个事务里走 0011 降级再升级, 最后回滚 (不动测试库的表结构与 alembic 版本). 降级时没有
    数据集的项目 (engagements 与 dataset_loads 的 dataset 为空) 记为空字符串, 两列恢复非空, client_profiles 的 source,
    updated_by 两列删掉; 再升级两列回来且为空, dataset 又可为空; 可重复执行; dataset_loads 的 FORCE 行级安全事后已恢复.
    需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    _, (first, _second), _, _ = storage
    module = migration("0011_client_registration")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    engine = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:

                async def as_project(eid: str) -> None:
                    await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": eid})

                async def scalar(sql: str) -> Any:
                    return (await conn.execute(text(sql), {"e": first.id, "c": first.client_id})).scalar()

                async def nullable(table: str) -> str:
                    return str(
                        await scalar(
                            "SELECT is_nullable FROM information_schema.columns "
                            f"WHERE table_name = '{table}' AND column_name = 'dataset'"
                        )
                    )

                async def profile_columns() -> set[str]:
                    found = await conn.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'client_profiles'")
                    )
                    return {row[0] for row in found}

                await conn.execute(text("UPDATE engagements SET dataset = NULL WHERE id = :e"), {"e": first.id})
                await as_project(first.id)
                await conn.execute(
                    insert(m.dataset_loads).values(
                        engagement_id=first.id, dataset=None, data_version=1, loaded_at=now()
                    )
                )
                await conn.execute(
                    text("UPDATE client_profiles SET source = 'manual', updated_by = 'U-1' WHERE id = :c"),
                    {"c": first.client_id},
                )
                await as_project("")  # 迁移时不设 app.engagement_id (同 alembic 命令)
                await conn.run_sync(run("downgrade"))
                assert (await nullable("engagements"), await nullable("dataset_loads")) == ("NO", "NO")
                assert not {"source", "updated_by"} & await profile_columns()
                assert await scalar("SELECT dataset FROM engagements WHERE id = :e") == ""
                await as_project(first.id)
                assert await scalar("SELECT dataset FROM dataset_loads WHERE engagement_id = :e") == ""
                assert await scalar("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'dataset_loads'") is True

                await as_project("")
                await conn.run_sync(run("upgrade"))
                await conn.run_sync(run("upgrade"))  # 可重复执行
                assert (await nullable("engagements"), await nullable("dataset_loads")) == ("YES", "YES")
                assert {"source", "updated_by"} <= await profile_columns()
                assert await scalar("SELECT count(*) FROM client_profiles WHERE id = :c AND source IS NULL") >= 1
                await conn.execute(text("UPDATE engagements SET dataset = NULL WHERE id = :e"), {"e": first.id})
                assert await scalar("SELECT dataset IS NULL FROM engagements WHERE id = :e") is True
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


# ── 需求 R90: 文件的补登记 ──


@pytest.mark.external
async def test_document_details_round_trip(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """真实库上传 (FileEvidenceStore.save) 一份 PDF, 读回文件日期为空, 关联对象为 []; 补登记后读回两字段与可靠性;
    抽取版本表两版 (A3 补记的修改前一版, 审计师的一版); 按关联对象过滤 (与类型同时给时都须满足); 数据版本 +1 并记
    documents; 写了留痕; 另一项目读不到 (行级安全)."""
    engine, (first, second), _, _ = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    pdf = supplements_dir("20261017-q1") / "documents" / "evidence" / "fixed_asset_register.pdf"
    saved = await service.deps.evidence_store.save(
        first.id,
        pdf.name,
        pdf.read_bytes(),
        type=DocumentType.internal_record,
        reliability=Reliability.internal_copy,
        uploaded_by="U-1",
        status="classified",
    )
    financial = PostgresFinancialDataStore(engine)
    assert await financial.documents(first.id) == [saved]
    assert (saved.document_date, saved.related_ids) == (None, [])

    edit = DocumentDetailsEdit(
        reliability=Reliability.internal_original,
        document_date=date(2026, 9, 30),
        related_ids=["XJ-499", "XJ-626", "XJ-676"],
        reason="按补造清单",
    )
    [updated] = await service.set_document_details(first.id, {saved.id: edit}, "U-1")

    assert (updated.reliability, updated.document_date, updated.related_ids) == (
        Reliability.internal_original,
        date(2026, 9, 30),
        ["XJ-499", "XJ-626", "XJ-676"],
    )
    assert await financial.documents(first.id, related_id="XJ-626") == [updated]
    assert await financial.documents(first.id, DocumentType.internal_record, "XJ-499") == [updated]
    assert await financial.documents(first.id, DocumentType.contract, "XJ-499") == []
    assert await financial.documents(first.id, related_id="XJ-6") == []  # 整个 ID 相同才算
    history = await service.deps.data.extraction_versions(first.id, "document", saved.id)
    assert [(r.version, r.changed_by, r.field, r.reason) for r in history] == [
        (1, "A3", None, None),
        (2, "U-1", "reliability,document_date,related_ids", "按补造清单"),
    ]
    assert history[1].data == {
        "type": "internal_record",
        "reliability": "internal_original",
        "document_date": "2026-09-30",
        "related_ids": ["XJ-499", "XJ-626", "XJ-676"],
    }
    changes = await service.deps.data.changes(first.id)
    assert changes[-1] == DataChange(  # 带补登记的文件类型 (需求 R304)
        data_version=changes[-1].data_version, input=DataInput.documents, document_types=[DocumentType.internal_record]
    )
    key = f"{first.id}:data:edit:document:{saved.id}:v2"
    assert await service.deps.trail.find_success(first.id, key) is not None
    assert await financial.documents(second.id) == []
    assert await financial.documents(second.id, related_id="XJ-499") == []
    assert await service.deps.data.extraction_versions(second.id, "document") == []
    with pytest.raises(LookupError):  # 别的项目的文件当作不存在
        await service.set_document_details(second.id, {saved.id: edit}, "U-1")


async def test_migration_0020_round_trip(storage) -> None:  # type: ignore[no-untyped-def]
    """需求 R304: 以属主角色在一个事务里走 0020 降级再升级 (两次), 最后回滚. 降级后 data_changes 没有 document_types;
    降级时已有的 documents 变动升级后读回为空 (不知道是哪类文件, 过期照旧按 reads 判). 需另设
    TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    _, (first, _second), _, _ = storage
    module = migration("0020_data_change_document_types")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    engine = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:

                async def columns() -> set[str]:
                    found = await conn.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'data_changes'")
                    )
                    return {row[0] for row in found}

                assert "document_types" in await columns()  # 0001 按当前模型建库, 新库已有
                await conn.run_sync(run("downgrade"))
                assert "document_types" not in await columns()
                await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": first.id})
                await conn.execute(
                    text("INSERT INTO data_changes (engagement_id, data_version, input) VALUES (:e, 99, 'documents')"),
                    {"e": first.id},
                )
                await conn.execute(text("SELECT set_config('app.engagement_id', '', true)"))
                await conn.run_sync(run("upgrade"))
                await conn.run_sync(run("upgrade"))  # 可重复执行
                assert "document_types" in await columns()
                await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": first.id})
                found = await conn.execute(text("SELECT document_types FROM data_changes WHERE data_version = 99"))
                assert found.one() == (None,)
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


async def test_migration_0012_round_trip(storage) -> None:  # type: ignore[no-untyped-def]
    """需求 R90: 以属主角色在一个事务里走 0012 降级再升级 (两次), 最后回滚. 降级后 documents 没有两列; 降级时已有的行
    升级后读回文件日期为空, 关联对象为 []. 需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    _, (first, _second), _, _ = storage
    module = migration("0012_document_details")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    engine = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:

                async def columns() -> set[str]:
                    found = await conn.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'documents'")
                    )
                    return {row[0] for row in found}

                await conn.run_sync(run("downgrade"))
                assert not {"document_date", "related_ids"} & await columns()
                await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": first.id})
                await conn.execute(
                    text(
                        "INSERT INTO documents (engagement_id, id, type, file_name, file_path, sha256, page_count, "
                        "reliability, status, uploaded_at) VALUES (:e, 'DOC-old000000000', 'minutes', 'old.pdf', "
                        "'/x/old.pdf', 'old', 1, 'internal_copy', 'classified', now())"
                    ),
                    {"e": first.id},
                )
                await conn.execute(text("SELECT set_config('app.engagement_id', '', true)"))
                await conn.run_sync(run("upgrade"))
                await conn.run_sync(run("upgrade"))  # 可重复执行
                assert {"document_date", "related_ids"} <= await columns()
                await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": first.id})
                found = await conn.execute(
                    text("SELECT document_date, related_ids FROM documents WHERE id = 'DOC-old000000000'")
                )
                assert found.one() == (None, [])
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


# ── 需求 R96, R178, R136, R215, R217: 新建的项目表 (工资册, 应付账单, 股权与任职关系图, 签字前提的产出, 待审财报) ──

NEW_TABLES = [
    pytest.param(
        "0013_employees",
        "employees",
        "INSERT INTO employees (engagement_id, employee_id, name, start_date, employment_type, pay_period_days, "
        "gross_per_period_cents) VALUES (:e, 'E001', 'Oliver Gray', '2026-07-20', 'full_time', 14, 240000)",
        id="employees",
    ),
    pytest.param(
        "0014_supplier_bills",
        "supplier_bills",
        "INSERT INTO supplier_bills (engagement_id, id, bill_no, supplier, bill_date, due_date, amount_cents, "
        "outstanding_cents) VALUES (:e, 'PB-1', '3395', 'PowerDirect', '2026-09-30', '2026-10-16', 33110, 33110)",
        id="supplier_bills",
    ),
    pytest.param(
        "0016_ownership_graphs",
        "ownership_graphs",
        "INSERT INTO ownership_graphs (engagement_id, id, version, kind, data, created_by, created_at) "
        "VALUES (:e, 'OWNERSHIP', 1, 'OwnershipGraph', '{}', 'A5', now())",
        id="ownership_graphs",
    ),
    pytest.param(
        "0017_completion_records",
        "completion_records",
        "INSERT INTO completion_records (engagement_id, id, version, kind, data, created_by, created_at) "
        "VALUES (:e, 'S307C', 1, 'IndependenceDeclaration', '{}', 'A10', now())",
        id="completion_records",
    ),
    pytest.param(
        "0019_financial_statements",
        "financial_statements",
        "INSERT INTO financial_statements (engagement_id, id, version, prepared_at, directors_declaration_date, "
        "document_id, comparative_restatement_entry_nos) VALUES (:e, 'FS', 1, '2026-10-22', NULL, NULL, '[]')",
        id="financial_statements",
    ),
    pytest.param(
        "0019_financial_statements",
        "financial_statement_lines",
        "INSERT INTO financial_statement_lines (engagement_id, fs_id, fs_version, line_no, code, statement, name, "
        "amount_cents, sign, account_codes, subtotal, prior_amount_cents) "
        "VALUES (:e, 'FS', 1, 1, 'SOFP-OREC', 'financial_position', 'Other receivables', 4853, 1, "
        "'[\"620\"]', false, 0)",
        id="financial_statement_lines",
    ),
]


@pytest.mark.parametrize(("name", "table", "insert_row"), NEW_TABLES)
async def test_migration_of_a_new_table_round_trip(storage, name: str, table: str, insert_row: str) -> None:  # type: ignore[no-untyped-def]
    """需求 R96 (0013 工资册), R178 (0014 应付账单), R136 (0016 关系图), R215 (0017 签字前提的产出), R217 (0019 待审
    财报的两张表, 各查一次): 以属主角色
    在一个事务里走该迁移降级再升级 (两次), 最后回滚.
    降级后没有这张表; 升级后开了并强制行级安全, 有项目隔离策略, 应用角色可增删改查 (先撤掉 0001 的默认授权, 权限须来自
    迁移的 GRANT); 一个项目写的行另一项目看不到. 需另设 TEST_DATABASE_OWNER_URL."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text

    owner_url = os.getenv("TEST_DATABASE_OWNER_URL")
    if not owner_url:
        pytest.skip("需设 TEST_DATABASE_OWNER_URL: 测试库的属主角色 (迁移用)")
    _, (first, second), _, _ = storage
    module = migration(name)

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    engine = create_async_engine(owner_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:

                async def security() -> list[tuple[bool, bool]]:
                    found = await conn.execute(
                        text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                        {"t": table},
                    )
                    return [(bool(a), bool(b)) for a, b in found]

                async def count(eid: str) -> int:
                    await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": eid})
                    return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())

                assert await security() == [(True, True)]  # 0001 按当前模型建库, 新库已有
                # 0001 给属主新建的表设了默认授权; 先在本事务里撤掉, 应用角色的权限只能来自迁移自己的 GRANT
                await conn.execute(
                    text(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM audit_app"
                    )
                )
                await conn.run_sync(run("downgrade"))
                assert await security() == []
                await conn.run_sync(run("upgrade"))
                await conn.run_sync(run("upgrade"))  # 可重复执行
                assert await security() == [(True, True)]
                policies = await conn.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": table}
                )
                assert [row[0] for row in policies] == ["engagement_isolation"]
                grants = await conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = :t AND grantee = 'audit_app'"
                    ),
                    {"t": table},
                )
                assert {row[0] for row in grants} == {"SELECT", "INSERT", "UPDATE", "DELETE"}
                await conn.execute(text("SELECT set_config('app.engagement_id', :e, true)"), {"e": first.id})
                await conn.execute(text(insert_row), {"e": first.id})
                assert (await count(first.id), await count(second.id)) == (1, 0)
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


# ── 需求 R268: 先有版本行再载入 ──


@pytest.mark.external
async def test_seed_load_after_a_version_row_still_loads(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """R268 验收 5, 6: 先补登记一份上传文件 (建了数据版本 1 的 dataset_loads 行, 还没有财务表行) 再载入, 照常载入,
    不静默跳过: 各表与直接载入的项目逐行相同, 数据版本 2 且这一版记全部各类数据, 视同就绪; 再载入不变. 已由审计师确认
    就绪的项目载入后保留原确认记录, 只把数据版本 +1."""
    engine, (first, second), loader, engagements = storage
    service = import_service(os.environ["TEST_DATABASE_URL"], tmp_path)
    data = service.deps.data
    pdf = supplements_dir("20261017-q1") / "documents" / "evidence" / "fixed_asset_register.pdf"
    saved = await service.deps.evidence_store.save(
        first.id,
        pdf.name,
        pdf.read_bytes(),
        type=DocumentType.internal_record,
        reliability=Reliability.internal_copy,
        uploaded_by="U-1",
        status="classified",
    )
    edit = DocumentDetailsEdit(document_date=date(2026, 6, 30), reason="按客户提供的登记表")
    await service.set_document_details(first.id, {saved.id: edit}, "U-1")
    assert ((await data.readiness(first.id)).data_version, (await data.readiness(first.id)).ready_at) == (1, None)

    await loader.load(first.id, "demo")
    await loader.load(second.id, "demo")  # 对照: 没有版本行时直接载入

    for name in FINANCIAL_ROWS:
        assert await table_rows(engine, first.id, name) == await table_rows(engine, second.id, name), name
    loaded = await data.readiness(first.id)
    assert (loaded.data_version, loaded.ready_by, loaded.ready_at is not None) == (2, "dataset", True)
    assert [c.input for c in await data.changes(first.id) if c.data_version == 2] == list(DataInput)
    docs = await PostgresFinancialDataStore(engine).documents(first.id)
    assert (
        saved.id in {d.id for d in docs}
        and len(docs) == len(await PostgresFinancialDataStore(engine).documents(second.id)) + 1
    )
    await loader.load(first.id, "demo")  # 已有财务表行: 不写
    assert await data.readiness(first.id) == loaded
    assert len(await table_rows(engine, first.id, "journal_entries")) == len(csv_rows("journal_entries"))

    third = await engagements.create(
        owner_id=first.owner_id,
        period_start=date(2025, 7, 1),
        period_end=date(2026, 6, 30),
        dataset="demo",
        profile=loader.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex}),
    )
    confirmed = await data.confirm_ready(third.id, "U-1", reason="客户说数据已齐")
    await loader.load(third.id, "demo")
    after = await data.readiness(third.id)
    assert after.data_version == confirmed.data_version + 1
    assert (after.ready_at, after.ready_by, after.ready_reason) == (confirmed.ready_at, "U-1", "客户说数据已齐")


async def partial_table(ctx: dict[str, Any]) -> None:
    """只导入了一张账表 (银行账户, 不是科目表) 的项目再载入: 已有财务表行, 不写也不报错, 数据版本不变."""
    data, loader, eid = ctx["data"], ctx["loader"], ctx["eid"]
    row = FINANCIAL_ROWS["bank_accounts"].model_validate(csv_rows("bank_accounts")[0])
    await data.write_tables(eid, {TableKind.bank_accounts: [row]}, source="import", actor="U-1")
    await loader.load(eid, "demo")
    assert [len(await table_rows(ctx["engine"], eid, n)) for n in ("bank_accounts", "accounts")] == [1, 0]
    assert (await data.readiness(eid)).data_version == 1


async def payroll_first(ctx: dict[str, Any]) -> None:
    """先导入工资册 (不算数据集载入的财务表) 再载入: 照常载入, 版本 +1, loaded_at (第一次接入的时间) 不改."""
    from tests.test_employees import EXPECTED

    data, loader, eid = ctx["data"], ctx["loader"], ctx["eid"]
    await data.write_tables(eid, {TableKind.employees: EXPECTED}, source="import", actor="U-1")
    before = await data.readiness(eid)
    await loader.load(eid, "demo")
    after = await data.readiness(eid)
    assert len(await table_rows(ctx["engine"], eid, "accounts")) == len(csv_rows("accounts"))
    assert (after.data_version, after.loaded_at) == (before.data_version + 1, before.loaded_at)


async def other_dataset(ctx: dict[str, Any]) -> None:
    """已载入 demo 的项目以另一个数据集载入: 抛 ValueError, 不当作已载入静默返回."""
    loader, eid = ctx["loader"], ctx["eid"]
    await loader.load(eid, "demo")
    other = CsvDatasetLoader(ROOT / "data", ctx["engine"], loader.evidence_store)
    with pytest.raises(ValueError, match="different dataset"):
        await other.load(eid, "other")


async def adjustments_need_tables(ctx: dict[str, Any]) -> None:
    """R268-01: 有版本行 (确认就绪) 而还没有财务表行时, 更正分录载入同样报尚未载入, 不写分录 (否则之后的种子载入
    见分录有行, 会把整个项目判为已载入而跳过). 更正分录取 Xero 补造的一份, 放在 tmp 的 demo 数据集目录下."""
    data, eid, tmp_path = ctx["data"], ctx["eid"], ctx["tmp_path"]
    await data.confirm_ready(eid, "U-1", reason="先确认")
    target = tmp_path / "datasets" / "demo" / "adjustments" / "journal_entries.csv"
    target.parent.mkdir(parents=True)
    target.write_bytes((supplements_dir("20261017-q1") / "adjustments" / "journal_entries.csv").read_bytes())
    adjustments = CsvDatasetLoader(tmp_path / "datasets", ctx["engine"], ctx["loader"].evidence_store)
    with pytest.raises(ValueError, match="尚未载入数据集"):
        await adjustments.load_adjustments(eid, "demo", ["AJE-X01"])
    assert await table_rows(ctx["engine"], eid, "journal_entries") == []


@pytest.mark.parametrize(
    "case",
    [partial_table, payroll_first, other_dataset, pytest.param(adjustments_need_tables, marks=pytest.mark.external)],
    ids=lambda c: c.__name__,
)
async def test_already_loaded_means_financial_rows(storage, tmp_path: Path, case: Any) -> None:  # type: ignore[no-untyped-def]
    """R268 已载入的口径: 看数据集载入的七张财务表有没有行 (任一张有即已载入, 工资册不算), 种子载入与更正分录载入
    同一判断; 各例构造到刚好能区分对错 (审核 R268-01, R268-02)."""
    engine, (first, _second), loader, _ = storage
    data = PostgresDataStore(tmp_path, engine)
    await case({"engine": engine, "data": data, "loader": loader, "eid": first.id, "tmp_path": tmp_path})
