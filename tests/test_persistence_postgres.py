"""Real PostgreSQL acceptance tests. Set TEST_DATABASE_URL to a migrated disposable database."""

import asyncio
import json
import os
import shutil
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.adapters.confirmation_mock import DatasetConfirmationChannel, MockConfirmationChannel
from backend.adapters.registry_mock import DatasetRegistrySource, MockRegistrySource
from backend.adapters.xero_supplements import SupplementMismatch
from backend.agents.ingest import contract_id
from backend.contracts.errors import EngagementLocked, VersionConflict
from backend.contracts.models import AuditObjective, DocumentType, LlmSettings, Reliability, TrailEntry, TrailKind
from backend.contracts.ports import ConfirmationRequest
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
from backend.services.audit_trail import PostgresAuditTrail
from backend.services.procedure_library import ProcedureLibrary

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
        min_change_cents=10000000,
        revenue_categories=["revenue"],
        cost_of_sales_categories=["cost_of_sales"],
        income_tax_categories=["income_tax"],
        subsequent_days=10,
        lookback_days=5,
        threshold_cents=9000000,
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
    with pytest.raises(DBAPIError, match="read-only"):
        await financial.query(e.id, "DELETE FROM sales_invoices", {})
    with pytest.raises(DBAPIError):
        await financial.query(e.id, "SELECT 1; DELETE FROM sales_invoices", {})


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
            await engagements.save_profile(profile.model_copy(update={"version": 2}))
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


@pytest.mark.parametrize("checkpoint_backend", ["memory", "postgres"])
async def test_full_workflow_real_storage(storage, tmp_path, checkpoint_backend):
    from contextlib import AsyncExitStack

    from backend.contracts.models import (
        AuditException,
        AuditReportVersion,
        GateVerdict,
        Judgment,
        Misstatement,
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
    analytics = await financial.query(e.id, sql("PLN-07", "A01"), dict(change_pct=0.2, min_change_cents=10000000))
    assert analytics[0]["change_pct"] is None


XERO_SNAPSHOT = "xero/20261017-mock"  # 本地 xero pull --paid mock 的快照, 不入库; 没有时跳过


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
    """R70: 同一库先后以 xero/20261017-mock 与 xero/20261017-q1 建项目都成功, 快照画像 CLIENT-XERO-3C1332BB v1 与
    补造画像 CLIENT-XERO-3C1332BB-Q1 v1 并存, 不报 VersionConflict; 再建一次 (画像已在库中, 内容相同) 也照常."""
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
    for _ in range(2):
        for dataset, end in periods.items():
            await store.create(
                owner_id=user,
                period_start=date(2026, 7, 1),
                period_end=end,
                dataset=dataset,
                profile=loader.client_profile(dataset),
            )
    mock = await store.client_profile("CLIENT-XERO-3C1332BB", 1)
    q1 = await store.client_profile("CLIENT-XERO-3C1332BB-Q1", 1)
    assert mock is not None and mock.legal_name == "Demo Company (AU)"
    assert q1 is not None and q1.legal_name == "Demo Company (AU) Pty Ltd" and q1.abn == "25900043021"
    await engine.dispose()


async def test_xero_q1_loads_supplements(tmp_path):
    """R72: 真实库载入 xero/20261017-q1: 行数同验收 2; 每个科目 gl_balance 等于本期试算表; 3 份合同登记为证据文件;
    服务完成记录与带合同关联的发票可按行取."""
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
        "delivery_notes": 24,
        "bank_accounts": 2,
        "bank_transactions": 146,
    }
    current = {r.account_code: r.balance_cents for r in await financial.trial_balance(e.id, "current")}
    assert {c: v for c, v in current.items() if v != await financial.gl_balance(e.id, c, date(2026, 9, 30))} == {}
    documents = await financial.documents(e.id)
    assert sorted((d.file_name, d.type, d.reliability) for d in documents) == [
        (f"CON-X0{n}.pdf", DocumentType.contract, Reliability.external_copy) for n in (1, 2, 3)
    ]
    manifest = json.loads((ROOT / "data/xero_demo/20261017-q1/manifest.json").read_text(encoding="utf-8"))
    ids = {d["path"]: d["document_id"] for d in manifest["documents"]}
    assert {d.id for d in documents} == {ids[f"contracts/CON-X0{n}.pdf"] for n in (1, 2, 3)}
    assert sorted(contract_id(d) for d in documents) == ["CON-X01", "CON-X02", "CON-X03"]
    note = await financial.record(e.id, "delivery_note", "SR-ORC1026")
    assert note is not None and note["invoice_id"] == "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611"
    invoice = await financial.record(e.id, "sales_invoice", "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611")
    assert invoice is not None and invoice["contract_id"] == "CON-X01"
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
    尚未载入, 数据集不同, 同 ID 内容不同, 项目锁定都拒绝且不写."""
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
    second = await xero.load_adjustments(e.id, "xero/20261017-q1", ["AJE-X02", "AJE-X03", "AJE-X04"])
    assert len(second) == 6 and await state(e.id) == (1028, 3)
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
    assert [await financial.gl_balance(fresh.id, code, as_of) for code in ("620", "270", "710", "820")] == [
        4853,
        -4853,
        0,
        -126697,
    ]

    # 项目锁定
    locked = await engagement()
    await xero.load(locked.id, "xero/20261017-q1")
    await store.update(locked.id, locked=True)
    with pytest.raises(EngagementLocked):
        await xero.load_adjustments(locked.id, "xero/20261017-q1", ["AJE-X01"])
    assert await state(locked.id) == (1017, 1)
    await engine.dispose()
