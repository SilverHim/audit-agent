"""需求 R274 验收 1, 2 (连库, 真实规则引擎与存储, 大模型为 mock): Numbat 全流程到 FIN-01 后, 经 DataService 重新导入
sales_invoices.csv, 只改 SI-2026-0612 一张发票的日期; 只有 REV-08, REV-02 过期; 全部重跑后两条用新数据新建执行,
例外按合成数据集设计推算, 旧例外与任务由新结果取代, 新例外进入复核, FIN-01 重新呈报.

推算 (合成数据集设计第 7 节第 7, 9 行): REV-08 的窗口为 2026-06-10 至 06-30 (window_days 20). 发票日期与入账日改为
2026-06-26 后仍在窗口内, 发运单 DN-2026-0612 仍于 2026-07-03 签收, 晚于期末: R01 仍命中, 新例外 X-REV-08-2-R01-1
的 posting_date 为 2026-06-26 (第 1 轮为 06-28), 金额 14200000; R02, R03 仍无. REV-02 的 S01 仍选中 SI-2026-0612
与 SI-2026-0618 (窗口内超过执行重要性的只有这两张, 第 7 节规则 10), 两条新判断与两个新 normal 任务; 0612 的判断引用
新例外. 复核同第 1 轮后, 汇总表只计新错报 14200000, 仍为保留意见候选.
"""

import asyncio
import os
from contextlib import AsyncExitStack
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from backend.contracts.data import ImportCommit, TableKind
from backend.contracts.models import (
    AuditException,
    GateVerdict,
    Judgment,
    Misstatement,
    ProcedureRun,
    ReviewTask,
    RunTrigger,
    TaskStatus,
    UncorrectedSchedule,
)
from backend.contracts.views import CompletionMaterial
from backend.core.config import Settings
from backend.main import build_deps
from backend.orchestration.checkpoint import open_checkpointer
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.persistence import models as m
from backend.persistence.stores import CsvDatasetLoader, PostgresEngagementStore, now
from backend.services.data_service import DataService
from tests.fakes import NUMBAT_FEATURES
from tests.test_decisions import acc01_decision, pln_decision
from tests.test_flow import REVIEWS
from tests.test_rerun_stale import SECOND_REVIEWS

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]
CHANGED_DATE = "2026-06-26"  # SI-2026-0612 原为 2026-06-28 (发票日期与入账日)


def changed_invoices() -> bytes:
    """data/demo/sales_invoices.csv 的副本内容, 只改 SI-2026-0612 的发票日期与入账日 (原文件不改)."""
    lines = (ROOT / "data/demo/sales_invoices.csv").read_text(encoding="utf-8").splitlines(keepends=True)
    target = [i for i, line in enumerate(lines) if line.startswith("SI-2026-0612,")]
    assert len(target) == 1
    before = lines[target[0]]
    after = before.replace(",2026-06-28,2026-06-28,", f",{CHANGED_DATE},{CHANGED_DATE},")
    assert after != before
    lines[target[0]] = after
    return "".join(lines).encode("utf-8")


async def test_reimported_invoice_date_reruns_only_revenue_programs(tmp_path: Path) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    owner = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=owner, email=owner + "@example.com", full_name="Test Auditor", password_hash="test", created_at=now()
            )
        )
    profile = CsvDatasetLoader(ROOT / "data").client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    e = await PostgresEngagementStore(engine).create(
        owner_id=owner, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
    )
    await engine.dispose()
    settings = Settings(
        DATABASE_URL=url,
        EVIDENCE_ROOT=tmp_path,
        LLM_MODE="mock",
        LLM_FIXTURES_ROOT=ROOT / "data/demo/llm",
        CHECKPOINT_BACKEND="memory",
        FEATURES=",".join(sorted(NUMBAT_FEATURES)),
    )
    deps = build_deps(settings)
    stack = AsyncExitStack()
    saver = await stack.enter_async_context(open_checkpointer(settings))
    graph = compile_graph(GraphNodes(deps, 4), saver)
    runner = GraphRunner(graph, deps, {"mode": "mock", "model": "mock"})
    decisions = GraphDecisionSubmit(graph, deps, runner)
    query = GraphStateQuery(graph, deps, runner)
    store = deps.output_store

    async def settle() -> tuple[str, ...]:
        for _ in range(1000):
            if runner.run_error(e.id):
                pytest.fail(str(runner.run_error(e.id)))
            if not runner.is_running(e.id):
                return (await graph.aget_state(runner.config(e.id))).next
            await asyncio.sleep(0.02)
        pytest.fail("Workflow did not reach a decision point")

    async def submit_all(items: list[Any]) -> None:
        for decision in items:
            await settle()
            field = "reviewed_by" if hasattr(decision, "reviewed_by") else "decided_by"
            await decisions.submit(e.id, decision.model_copy(update={field: owner}))

    try:
        await deps.datasets.load(e.id, e.dataset)
        await runner.start(e.id)
        await settle()
        await submit_all([acc01_decision(GateVerdict.approve, id="D-ACC"), pln_decision(GateVerdict.approve), *REVIEWS])
        assert await settle() == ("gate_fin",)
        assert (await query.overview(e.id)).stale_procedure_ids == []
        first = await store.get(e.id, AuditException, "X-REV-08-1-R01-1")
        assert first is not None and first.detail["posting_date"] == "2026-06-28"

        # 验收 1: 经导入流程重新导入发票表, 只改一张发票的日期
        service = DataService(deps, runner)
        preview = await service.start_import(e.id, "sales_invoices.csv", changed_invoices(), actor=owner)
        assert preview.suggested_kind == TableKind.sales_invoices
        body = ImportCommit(
            kind=TableKind.sales_invoices,
            sheet=preview.sheet,
            date_format=preview.suggested_date_format,
            mapping=preview.suggested_mapping,
        )
        await service.commit_import(e.id, preview.import_id, body, owner)
        version = (await deps.data.readiness(e.id)).data_version
        assert version == 2
        overview = await query.overview(e.id)
        assert overview.stale_procedure_ids == ["REV-02", "REV-08"] and overview.can_rerun
        assert {p.id for p in overview.procedures if p.rerunnable} == {"REV-02", "REV-08"}

        # 验收 2: 全部重跑
        request = await decisions.rerun_stale(e.id, "RR-" + uuid4().hex, None, requested_by=owner)
        assert request.procedure_ids == ["REV-08", "REV-02"]
        assert await settle() == ("await_events",)
        for pid in ("REV-08", "REV-02"):
            run = await store.get(e.id, ProcedureRun, f"RUN-{pid}-2")
            assert run is not None and (run.trigger, run.data_version) == (RunTrigger.rerun, 2)
        new = [x for x in await store.latest(e.id, AuditException, procedure_id="REV-08") if x.run_id == "RUN-REV-08-2"]
        assert [(x.id, x.rule_id, x.source_ids, x.amount_cents) for x in new] == [
            ("X-REV-08-2-R01-1", "R01", ["SI-2026-0612"], 14200000)
        ]
        assert (new[0].detail["posting_date"], new[0].detail["signed_date"]) == (CHANGED_DATE, "2026-07-03")
        judged = {j.id: j for j in await store.latest(e.id, Judgment, procedure_id="REV-02") if j.run_id.endswith("-2")}
        assert sorted(judged) == ["J-REV-02-2-SI-2026-0612", "J-REV-02-2-SI-2026-0618"]
        assert judged["J-REV-02-2-SI-2026-0612"].exception_ids == ["X-REV-08-2-R01-1"]
        pending = await store.latest(e.id, ReviewTask, status=TaskStatus.pending)
        assert sorted(t.id for t in pending) == [d.task_id for d in SECOND_REVIEWS]
        assert (await query.overview(e.id)).stale_procedure_ids == []

        await submit_all(SECOND_REVIEWS)
        assert await settle() == ("gate_fin",)
        schedule = await store.get(e.id, UncorrectedSchedule, "SCHEDULE")
        assert schedule is not None and schedule.version == 2
        assert [(i.amount_cents) for i in schedule.items] == [14200000] and schedule.total_cents == 14200000
        view = await query.gate(e.id)
        assert view is not None and (view.target_id, view.target_version) == ("SCHEDULE", 2)
        assert isinstance(view.material, CompletionMaterial)
        assert [x.judgment_id for x in view.material.misstatements] == ["J-REV-02-2-SI-2026-0612"]
        assert len(await store.latest(e.id, Misstatement)) == 2  # 第 1 轮的留作历史
        attributed = await store.get(e.id, AuditException, "X-REV-08-2-R01-1")
        assert attributed is not None and attributed.attributed_by == "J-REV-02-2-SI-2026-0612"
    finally:
        await runner.shutdown()
        await deps.financial_store.engine.dispose()
        await stack.aclose()
