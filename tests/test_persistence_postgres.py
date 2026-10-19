"""Real PostgreSQL acceptance tests. Set TEST_DATABASE_URL to a migrated disposable database."""

import asyncio
import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.adapters.confirmation_mock import DatasetConfirmationChannel, MockConfirmationChannel
from backend.adapters.registry_mock import DatasetRegistrySource, MockRegistrySource
from backend.adapters.xero_supplements import SupplementMismatch
from backend.contracts.errors import EngagementLocked, VersionConflict
from backend.contracts.models import (
    AuditObjective,
    ClientProfile,
    DocumentType,
    LlmSettings,
    ProfileSource,
    Reliability,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import ConfirmationRequest
from backend.contracts.records import Account, JournalEntry
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
from tests.fakes import NUMBAT_ACCOUNTS, NUMBAT_ROWS
from tests.test_fakes import demo_rows

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


async def test_new_document_types_round_trip(storage):
    """documents.type 是 text 列 (迁移 0001 无约束, 0004 也未加), 新取值不需迁移: 各存一份后按类型取回, 读回仍是枚举;
    没补登记的文件读回文件日期为空, 关联对象为 [] (补登记见 test_data_store_postgres)."""
    engine, e, _loader, evidence = storage
    supplements = ROOT / "data/xero_demo/20261017-q1"
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
async def test_full_workflow_real_storage(storage, tmp_path, checkpoint_backend):
    from contextlib import AsyncExitStack

    from backend.contracts.models import (
        AuditException,
        AuditReportVersion,
        GateVerdict,
        Judgment,
        Misstatement,
        OpinionType,
        Phase,
        ReviewTask,
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
            if checkpoint_backend == "postgres" and decision.id == REVIEWS[0].id:
                await settle()
                await runner.shutdown()
                await deps.financial_store.engine.dispose()
                await stack.aclose()
                deps = build_deps(settings)
                saver = await stack.enter_async_context(open_checkpointer(settings))
                graph = compile_graph(GraphNodes(deps, 4), saver)
                runner = GraphRunner(graph, deps, {"mode": "mock", "model": "mock"})
                decisions = GraphDecisionSubmit(graph, deps, runner)
                await runner.resume(e.id)
        await settle()
        final = await deps.engagement_store.get(e.id)
        assert final.phase == Phase.Archived and final.locked
        # 检查点读回的顶层枚举字段 (checkpoint.TOP_LEVEL_ENUMS) 是枚举而不是字符串; Postgres 由 AuditPostgresSaver 转回
        values = (await graph.aget_state(runner.config(e.id))).values
        assert values["phase"] is Phase.Archived and values["opinion_route"] is OpinionType.qualified
        assert len(await deps.output_store.latest(e.id, AuditException)) == 4
        assert len(await deps.output_store.latest(e.id, Judgment)) == 6
        assert all(t.status == TaskStatus.decided for t in await deps.output_store.latest(e.id, ReviewTask))
        assert [m.amount_cents for m in await deps.output_store.latest(e.id, Misstatement)] == [14200000]
        assert len(await deps.output_store.latest(e.id, AuditReportVersion)) == 2
        assert len(await deps.financial_store.documents(e.id)) == 5
    finally:
        await runner.shutdown()
        await deps.financial_store.engine.dispose()
        await stack.aclose()


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
    NUMBAT_D03_ROWS (6 行), NUMBAT_D04_ROWS (74 行) 整行相同, 给不给作废对的格式都一样 (Numbat 没有冲回凭证). 插入
    VX-1 (06-29, 1010 借 700000 / 4200 贷 700000) 与镜像的冲回凭证 VX-2 (只有 4200 一行的说明为 "Reversal of ID 1",
    1010 一行的说明不匹配: 冲回凭证按凭证号整张剔除, 不按行), 以及原凭证不存在的冲回凭证 VX-3 后: 给格式
    "Reversal of ID (\\d+)" 与模板 "VX-{1}" 时两条规则都不含这些行, 其余行与插行前整行相同; 不给 (为空) 时都含 (D03
    只有银行科目 1010 的两行). 两条规则的 WITH 段写得不一致即失败."""
    import yaml

    from backend.contracts.knowledge import ProcedureTemplate
    from tests.fakes import NUMBAT_D03_ROWS, NUMBAT_D04_ROWS

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


async def test_xero_snapshot_loads_and_ties_to_trial_balance(tmp_path):
    """Xero 快照经 XeroDatasetLoader 载入真实库: 各表有数, 科目带标准分类,
    生产的 gl_balance 对每个科目等于本期试算表."""
    url = os.getenv("TEST_DATABASE_URL")
    snapshot = ROOT / "data/external/xero/api" / XERO_SNAPSHOT.removeprefix("xero/")
    if not url or not snapshot.exists():
        pytest.skip("需要 TEST_DATABASE_URL 与本地 Xero 快照")
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


async def test_xero_mock_and_q1_profiles_coexist(tmp_path):
    """R70: 同一库先后以 xero/20261017-mock 与 xero/20261017-q1 建项目都成功, 快照画像 CLIENT-XERO-3C1332BB 与
    补造画像 CLIENT-XERO-3C1332BB-Q1 并存; 再建一次 (画像已在库中, 内容相同) 也照常. 不写死版本号 (R127): 库里
    若存着补造画像改内容之前的版本, 建项目时会出新版本, 断言最新一版的内容等于补造画像, 项目采用最新一版."""
    url = os.getenv("TEST_DATABASE_URL")
    api = ROOT / "data/external/xero/api"
    if not url or not (api / "20261017-mock").exists() or not (api / "20261017-q1").exists():
        pytest.skip("需要 TEST_DATABASE_URL 与本地 Xero 快照 20261017-mock, 20261017-q1")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="Xero", password_hash="x", created_at=now()
            )
        )
    loader = XeroDatasetLoader(
        api,
        ROOT / "src/backend/knowledge/planning",
        engine,
        FileEvidenceStore(tmp_path, engine),
        supplements=ROOT / "data/xero_demo",
    )
    store = PostgresEngagementStore(engine)
    periods = {"xero/20261017-mock": date(2026, 11, 30), "xero/20261017-q1": date(2026, 9, 30)}
    created = []
    for _ in range(2):
        for dataset, end in periods.items():
            created.append(
                await store.create(
                    owner_id=user,
                    period_start=date(2026, 7, 1),
                    period_end=end,
                    dataset=dataset,
                    profile=loader.client_profile(dataset),
                )
            )
    mock = await store.client_profile("CLIENT-XERO-3C1332BB")
    q1 = await store.client_profile("CLIENT-XERO-3C1332BB-Q1")
    assert mock is not None and mock.legal_name == "Demo Company (AU)"
    assert q1 is not None and q1.legal_name == "Demo Company (AU) Pty Ltd" and q1.abn == "25900043021"
    supplied = loader.client_profile("xero/20261017-q1")
    assert q1.model_dump(exclude={"version"}) == supplied.model_dump(exclude={"version"})
    assert q1.approval_limit_cents == 500000  # 扩展字段 (R127) 写进了库
    adopted = {e.client_id: e.client_profile_version for e in created[2:]}  # 第二轮: 两份画像都已是最新一版
    assert adopted == {mock.id: mock.version, q1.id: q1.version}
    await engine.dispose()


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


async def test_xero_q1_seed_load_is_snapshot_only(tmp_path):
    """R269 验收 11: 真实库载入 xero/20261017-q1 只载快照: 服务完成记录与工资册为 0 行, 发票的合同关联为空, 开户行取
    快照的科目名, 不登记文件 (补造部分由演示数据导入写入, 见 test_demo_import_db); 每个科目 gl_balance 等于本期试算表.
    补造与快照不匹配时不载入."""
    url = os.getenv("TEST_DATABASE_URL")
    api = ROOT / "data/external/xero/api"
    if not url or not (api / "20261017-q1").exists():
        pytest.skip("需要 TEST_DATABASE_URL 与本地 Xero 快照 20261017-q1")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="Xero", password_hash="x", created_at=now()
            )
        )
    evidence = FileEvidenceStore(tmp_path, engine)
    loader = XeroDatasetLoader(
        api, ROOT / "src/backend/knowledge/planning", engine, evidence, supplements=ROOT / "data/xero_demo"
    )
    e = await PostgresEngagementStore(engine).create(
        owner_id=user,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30),
        dataset="xero/20261017-q1",
        profile=loader.client_profile("xero/20261017-q1"),
    )
    await loader.load(e.id, "xero/20261017-q1")
    financial = PostgresFinancialDataStore(engine)
    counts = {t: (await financial.query(e.id, f"SELECT count(*) AS n FROM {t}", {}))[0]["n"] for t in m.FINANCIAL}
    assert counts == {
        "accounts": 62,
        "trial_balance": 33,
        "journal_entries": 1017,
        "sales_invoices": 42,
        "delivery_notes": 0,
        "bank_accounts": 2,
        "bank_transactions": 146,
        "employees": 0,  # 工资册不随数据集载入 (需求 R96)
    }
    current = {r.account_code: r.balance_cents for r in await financial.trial_balance(e.id, "current")}
    assert {c: v for c, v in current.items() if v != await financial.gl_balance(e.id, c, date(2026, 9, 30))} == {}
    assert await financial.documents(e.id) == [] and await financial.contracts(e.id) == []
    linked = await financial.query(e.id, "SELECT count(*) AS n FROM sales_invoices WHERE contract_id IS NOT NULL", {})
    assert linked[0]["n"] == 0
    snapshot = loader.merged("xero/20261017-q1", None)  # 快照映射出的七张表, 不并补造表
    banks = await financial.query(e.id, "SELECT id, bank_name FROM bank_accounts ORDER BY id", {})
    assert [(b["id"], b["bank_name"]) for b in banks] == sorted((b.id, b.bank_name) for b in snapshot["bank_accounts"])
    # 补造与快照不匹配时不载入: 各表, 载入标记与证据文件都没有写入
    demo = tmp_path / "xero_demo"
    shutil.copytree(ROOT / "data/xero_demo/20261017-q1", demo / "20261017-q1")
    links = demo / "20261017-q1" / "contract_links.csv"
    links.write_bytes(links.read_bytes() + b" ")
    planning = ROOT / "src/backend/knowledge/planning"
    broken = XeroDatasetLoader(api, planning, engine, FileEvidenceStore(tmp_path / "ev", engine), supplements=demo)
    other = await PostgresEngagementStore(engine).create(
        owner_id=user,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30),
        dataset="xero/20261017-q1",
        profile=loader.client_profile("xero/20261017-q1"),
    )
    with pytest.raises(SupplementMismatch, match=r"contract_links\.csv"):
        await broken.load(other.id, "xero/20261017-q1")
    for t in m.FINANCIAL:
        assert (await financial.query(other.id, f"SELECT count(*) AS n FROM {t}", {}))[0]["n"] == 0
    assert await financial.documents(other.id) == []
    assert not (tmp_path / "ev").exists() or not any((tmp_path / "ev").rglob("*.pdf"))
    await engine.dispose()


async def test_load_adjustments_two_rounds(tmp_path):
    """R73: 真实库载入 xero/20261017-q1 后按两轮载入更正分录: 行数, data_version, 幂等, gl_balance, 试算表不变;
    尚未载入, 数据集不同, 同 ID 内容不同, 项目锁定都拒绝且不写. R271: 每轮有新行时总账整表换一版 (版本行的来源,
    原文件名, 行数与写入人), 两轮之后体检的借贷平衡, 分录与试算表, 行数与来源都通过, 计入更正分录时分录与试算表不符."""
    url = os.getenv("TEST_DATABASE_URL")
    api = ROOT / "data/external/xero/api"
    if not url or not (api / "20261017-q1").exists():
        pytest.skip("需要 TEST_DATABASE_URL 与本地 Xero 快照 20261017-q1")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="Xero", password_hash="x", created_at=now()
            )
        )
    evidence = FileEvidenceStore(tmp_path, engine)
    xero = XeroDatasetLoader(
        api, ROOT / "src/backend/knowledge/planning", engine, evidence, supplements=ROOT / "data/xero_demo"
    )
    store = PostgresEngagementStore(engine)
    profile = xero.client_profile("xero/20261017-q1")

    async def engagement(dataset: str = "xero/20261017-q1"):
        chosen = profile if dataset.startswith("xero/") else CsvDatasetLoader(ROOT / "data").client_profile(dataset)
        return await store.create(
            owner_id=user, period_start=date(2026, 7, 1), period_end=date(2026, 9, 30), dataset=dataset, profile=chosen
        )

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
        result = run_checks(inputs, statements=0, journal_version=journals[-1], now=now())
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
    source = ROOT / "data/xero_demo/20261017-q1/adjustments/journal_entries.csv"
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
    await engine.dispose()


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
