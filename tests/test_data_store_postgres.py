"""数据管理存储 (PostgresDataStore) 的真实 PostgreSQL 测试. 设 TEST_DATABASE_URL 为已迁移到最新的独立测试库."""

import asyncio
import csv
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.contracts.data import ApprovalLogRow, CheckId, ImportCommit, ImportSession, TableKind
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
from backend.services.account_mapping import with_category
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
    assert (await deps.data.readiness(uploaded.id)).data_version == 1  # 修改抽取结果: 数据版本 0 → 1


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

    async def broken(eid: str) -> int:
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
