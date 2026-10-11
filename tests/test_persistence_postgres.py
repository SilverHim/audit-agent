"""Real PostgreSQL acceptance tests. Set TEST_DATABASE_URL to a migrated disposable database."""

import asyncio
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.adapters.confirmation_mock import MockConfirmationChannel
from backend.adapters.registry_mock import MockRegistrySource
from backend.contracts.errors import EngagementLocked, VersionConflict
from backend.contracts.models import AuditObjective, DocumentType, Reliability, TrailEntry, TrailKind
from backend.contracts.ports import ConfirmationRequest
from backend.persistence import models as m
from backend.persistence.db import transaction
from backend.persistence.stores import (
    CsvDatasetLoader,
    FileEvidenceStore,
    PostgresAuditOutputStore,
    PostgresEngagementStore,
    PostgresFinancialDataStore,
    PostgresUserStore,
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
        revenue_account_codes=["4100"],
        cost_of_sales_account_codes=["5100"],
        income_tax_account_codes=["8100"],
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
    assert (await registry.abn("68 900 042 015")).status == "Active"
    assert await registry.abn("missing") is None
    req = ConfirmationRequest(
        bank_account_id="BA-01", bank_name="Harbourline", bsb="999-101", account_no="12345601", as_of=date(2026, 6, 30)
    )
    channel = MockConfirmationChannel(ROOT / "data/demo", tmp_path / "receipts")
    first = await channel.submit(e.id, req, "key")
    assert first == await MockConfirmationChannel(ROOT / "data/demo", tmp_path / "receipts").submit(e.id, req, "key")
    with pytest.raises(ValueError):
        await channel.submit(e.id, req.model_copy(update={"account_no": "23456702"}), "key")
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
    from tests.test_decisions import acc01_decision, pln_decision
    from tests.test_flow import FIN_APPROVE, REVIEWS, RPT_APPROVE, RPT_REJECT

    _engine, e, _, _ = storage
    settings = Settings(
        DATABASE_URL=os.environ["TEST_DATABASE_URL"],
        EVIDENCE_ROOT=tmp_path,
        LLM_MODE="mock",
        LLM_FIXTURES_ROOT=ROOT / "data/demo/llm",
        CHECKPOINT_BACKEND=checkpoint_backend,
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
