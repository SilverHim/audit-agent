"""A6 REV-02 收入截止性判断: 判断对象与上下文 (合成数据集设计第 5.1 节, 第 7 节第 9 行; Q13 纳入 REV-08 全部例外)."""

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.models import (
    AgentId,
    AuditException,
    ExceptionDisposition,
    Judgment,
    Phase,
    ProposedAction,
    ReviewStatus,
    RiskLevel,
    RunStatus,
    TargetType,
    TaskKind,
)
from backend.contracts.records import DeliveryNote, SalesInvoice
from backend.contracts.services import RuleResult
from backend.orchestration.nodes import GraphNodes
from tests.fakes import (
    NUMBAT_CONTRACTS,
    NUMBAT_OBJECTIVES,
    NUMBAT_RECORDS,
    NUMBAT_RISKS,
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    make_ctx,
    make_deps,
    make_run,
    numbat_contracts,
    numbat_rev08_exception,
)
from tests.test_fieldwork_judging import failed, passed, pending_task

INVOICES = ["SI-2026-0612", "SI-2026-0618"]
EXTRACTED = [d.model_copy(update={"status": "extracted"}) for d in NUMBAT_CONTRACTS]
X1 = numbat_rev08_exception(make_run("REV-08"))  # X-REV-08-1-R01-1, SI-2026-0612 跨期
NOW = datetime(2026, 10, 5, tzinfo=UTC)


def rev03(contract_id: str, run_no: int = 1, **changes: Any) -> Judgment:
    """该合同的 REV-03 判断 (已确认); changes 覆盖任意字段."""
    base = Judgment(
        id=f"J-REV-03-{run_no}-{contract_id}",
        procedure_id="REV-03",
        run_id=f"RUN-REV-03-{run_no}",
        subject_id=contract_id,
        conclusion=f"{contract_id}: control transfers when the customer signs the delivery note.",
        confidence=0.9,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="none",
        proposed_action=ProposedAction.accept,
        autonomy_level="L2",
        review_status=ReviewStatus.confirmed,
        performed_by="U-1",
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    return base.model_copy(update=changes)


def setup(
    outcomes: dict[str, Any] | None = None,
    exceptions: list[AuditException] | None = None,
    judgments: list[Judgment] | None = None,
    records: dict[tuple[str, str], dict[str, Any]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    rerun: list[str] | None = None,
    documents: list[Any] | None = None,
) -> tuple[FieldworkAgent, RunContext, FakeJudgmentEngine]:
    """A6 与 REV-02 第 1 次执行: 财务数据为 Numbat 的发票, 发运单与 3 份合同; 状态有抽取过的合同文件,
    REV-08 的跨期例外 X1 与两份合同已确认的 REV-03 判断. outcomes 缺省两张发票都判断通过 (L2, 建 normal 任务).
    rows 替换规则 S01 选中的行 (缺省为 Numbat 的两张)."""
    engine = FakeJudgmentEngine(outcomes if outcomes is not None else {i: passed() for i in INVOICES})
    financial = FakeFinancialStore(
        list(NUMBAT_CONTRACTS), records=NUMBAT_RECORDS if records is None else records, contracts=numbat_contracts()
    )
    rules = FakeRuleEngine()
    if rows is not None:
        rules.results["REV-02"] = lambda run: RuleResult(exceptions=[], rows={"S01": rows})
    deps = replace(make_deps(FakeRegistry(), FakeTrail(), financial_store=financial, rules=rules), judgments=engine)  # type: ignore[arg-type]
    ctx = make_ctx(deps, "REV-02")
    procedure = ctx.procedure.model_copy(
        update={"phase": Phase.P2_Executing, "objective_ids": ["OBJ-REV-CUT"], "rerun_subject_ids": rerun or []}
    )
    state = ctx.state | {
        "procedure_pack": [procedure],
        "audit_objectives": NUMBAT_OBJECTIVES,
        "risk_register": NUMBAT_RISKS,
        "documents": EXTRACTED if documents is None else documents,
        "exceptions": [X1] if exceptions is None else exceptions,
        "judgments": [rev03("CON-001"), rev03("CON-002")] if judgments is None else judgments,
        "performance_materiality_cents": 9000000,
    }
    params = deps.library.resolve_params(ctx.template, ctx.engagement, state, RiskLevel.significant)
    run = ctx.run.model_copy(update={"params_snapshot": params})
    return FieldworkAgent(deps), RunContext(ctx.engagement, procedure, ctx.template, run, state), engine  # type: ignore[arg-type]


def contexts(engine: FakeJudgmentEngine) -> dict[str, Any]:
    return {jctx.subject_id: jctx for _, jctx, _ in engine.calls}


def invoice_json(invoice_id: str) -> dict[str, Any]:
    row = SalesInvoice.model_validate(NUMBAT_RECORDS[("sales_invoice", invoice_id)])
    return {"source_type": "sales_invoice", **row.model_dump(mode="json")}


def note_json(note_id: str, records: dict[tuple[str, str], dict[str, Any]] = NUMBAT_RECORDS) -> dict[str, Any]:
    return {
        "source_type": "delivery_note",
        **DeliveryNote.model_validate(records[("delivery_note", note_id)]).model_dump(mode="json"),
    }


# ── 判断对象 ─────────────────────────────────────────


async def test_numbat_subjects_are_s01_union_rev08_sorted() -> None:
    """验收 1: S01 选中两张, REV-08 跨期例外指向其中一张; 并集去重后按发票 ID 排序."""
    agent, ctx, engine = setup()
    await agent.run(ctx)
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == INVOICES
    assert {jctx.fixture_key for _, jctx, _ in engine.calls} == set(INVOICES)


async def test_all_open_rev08_exceptions_bring_their_invoices() -> None:
    """Q13: REV-08 的 R02, R03 例外所指的发票也是判断对象, 即使不在 S01 中; 已归因的例外不带入."""
    r02 = X1.model_copy(update={"id": "X-REV-08-1-R02-1", "rule_id": "R02", "source_ids": ["SI-2026-0547"]})
    r03 = X1.model_copy(update={"id": "X-REV-08-1-R03-1", "rule_id": "R03", "source_ids": ["SI-2026-0547"]})
    done = X1.model_copy(
        update={
            "id": "X-REV-08-1-R03-2",
            "rule_id": "R03",
            "source_ids": ["SI-2026-9999"],
            "disposition": ExceptionDisposition.attributed,
        }
    )
    outcomes = {i: passed() for i in [*INVOICES, "SI-2026-0547"]}
    agent, ctx, engine = setup(outcomes, exceptions=[X1, r03, done, r02])
    await agent.run(ctx)
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == ["SI-2026-0547", *INVOICES]
    assert [x.id for x in contexts(engine)["SI-2026-0547"].rule_findings] == [r02.id, r03.id]


async def test_only_rev08_exceptions_count_as_findings() -> None:
    """别的程序 (不在 depends_on 中) 的例外不带入, 即使 source_ids 相同."""
    other = X1.model_copy(update={"id": "X-CSH-05-1-B01-1", "procedure_id": "CSH-05"})
    agent, ctx, engine = setup(exceptions=[X1, other])
    await agent.run(ctx)
    assert [x.id for x in contexts(engine)["SI-2026-0612"].rule_findings] == [X1.id]


async def test_rerun_only_listed_invoices() -> None:
    agent, ctx, engine = setup(rerun=["SI-2026-0618"])
    await agent.run(ctx)
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == ["SI-2026-0618"]


async def test_no_subjects_gives_succeeded_run_without_judgments() -> None:
    agent, ctx, engine = setup(exceptions=[], rows=[])
    patch = await agent.run(ctx)
    assert engine.calls == [] and patch["judgments"] == [] and patch["review_tasks"] == []
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


# ── 上下文 ─────────────────────────────────────────


async def test_si0612_context() -> None:
    """验收 2: 发票, 发运单 (按 ID 排序), 合同, 合同文件, REV-03 判断与跨期例外; 任务说明含参数与风险."""
    records = dict(NUMBAT_RECORDS)
    extra = DeliveryNote(
        id="DN-2026-0612-A",
        delivery_no="DN-2026-0612-A",
        invoice_id="SI-2026-0612",
        shipped_date=date(2026, 7, 2),
        signed_date=None,
        signed_by=None,
    )
    records[("delivery_note", extra.id)] = extra.model_dump()
    agent, ctx, engine = setup(records=records)
    await agent.run(ctx)
    jctx = contexts(engine)["SI-2026-0612"]
    assert jctx.data == {
        "invoice": invoice_json("SI-2026-0612"),
        "delivery_notes": [note_json("DN-2026-0612", records), note_json("DN-2026-0612-A", records)],
        "contract": numbat_contracts()[0].model_dump(mode="json"),
        "document": EXTRACTED[0].model_dump(mode="json"),
        "contract_judgment": {
            "id": "J-REV-03-1-CON-001",
            "conclusion": rev03("CON-001").conclusion,
            "review_status": "confirmed",
        },
    }
    assert jctx.data["delivery_notes"][0]["signed_date"] == "2026-07-03"
    assert jctx.rule_findings == [X1]
    assert jctx.task["params"] == {"window_days": 20, "threshold_cents": 9000000, "period_end": "2026-06-30"}
    assert [r["id"] for r in jctx.task["risks"]] == ["RISK-REV-CUT"]


async def test_si0618_context() -> None:
    """验收 3: SI-2026-0618 没有规则例外, 合同为 CON-002."""
    agent, ctx, engine = setup()
    await agent.run(ctx)
    jctx = contexts(engine)["SI-2026-0618"]
    assert jctx.rule_findings == []
    assert jctx.data["contract"]["id"] == "CON-002" and jctx.data["document"]["id"] == EXTRACTED[1].id
    assert jctx.data["contract_judgment"]["id"] == "J-REV-03-1-CON-002"
    assert jctx.data["delivery_notes"] == [note_json("DN-2026-0618")]


async def test_invoice_without_contract_has_null_contract_fields() -> None:
    """验收 3: 发票没有合同时 contract, document, contract_judgment 为 null."""
    r02 = X1.model_copy(update={"id": "X-REV-08-1-R02-1", "rule_id": "R02", "source_ids": ["SI-2026-0547"]})
    agent, ctx, engine = setup({"SI-2026-0547": passed()}, exceptions=[r02], rows=[])
    await agent.run(ctx)
    data = contexts(engine)["SI-2026-0547"].data
    assert data["invoice"] == invoice_json("SI-2026-0547")
    assert (data["contract"], data["document"], data["contract_judgment"]) == (None, None, None)


@pytest.mark.parametrize(
    ("judgments", "expected"),
    [
        ([], None),
        (
            [rev03("CON-001"), rev03("CON-001", run_no=2, created_at=datetime(2026, 7, 20, tzinfo=UTC))],
            "J-REV-03-2-CON-001",
        ),
        (
            [
                rev03(
                    "CON-001", version=2, review_status=ReviewStatus.overridden, superseded_by="J-REV-03-1-CON-001-H1"
                ),
                rev03(
                    "CON-001",
                    id="J-REV-03-1-CON-001-H1",
                    conclusion="human",
                    created_at=datetime(2026, 7, 5, tzinfo=UTC),
                ),
            ],
            "J-REV-03-1-CON-001-H1",
        ),
        ([rev03("CON-001", review_status=ReviewStatus.pending)], "J-REV-03-1-CON-001"),
        ([rev03("CON-002")], None),
    ],
    ids=["none", "latest_run", "override_takes_human", "pending_still_given", "other_contract"],
)
async def test_contract_judgment_choice(judgments: list[Judgment], expected: str | None) -> None:
    """该合同未被推翻的 REV-03 判断中最新的一条; 被 override 时为人工判断 (即使它的时间更早); 没有为 null."""
    agent, ctx, engine = setup(judgments=judgments)
    await agent.run(ctx)
    chosen = contexts(engine)["SI-2026-0612"].data["contract_judgment"]
    assert (chosen and chosen["id"]) == expected


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("invoice", "发票不在财务数据中: SI-2026-0618"),
        ("contract", "合同 CON-002 不在财务数据中"),
        ("document", "合同 CON-002 的文件"),
    ],
)
async def test_missing_data_raises(change: str, message: str) -> None:
    records = dict(NUMBAT_RECORDS)
    documents = list(EXTRACTED)
    if change == "invoice":
        del records[("sales_invoice", "SI-2026-0618")]
    if change == "document":
        documents = [EXTRACTED[0], EXTRACTED[2]]
    agent, ctx, _ = setup(records=records, documents=documents)
    if change == "contract":
        agent.deps.financial_store.saved_contracts.pop("CON-002")  # type: ignore[attr-defined]
    with pytest.raises(LookupError, match=message):
        await agent.execute(ctx)


# ── Codex R43 探针: 筛选在取数之前, 缺数据的处理 ─────────────────────────────────────────


async def test_s01_invoice_kept_but_attributed_findings_omitted() -> None:
    """跨期例外已归因: SI-2026-0612 仍因 S01 选中而判断, 但不再带入该例外."""
    agent, ctx, engine = setup(exceptions=[X1.model_copy(update={"disposition": ExceptionDisposition.attributed})])
    await agent.run(ctx)
    assert list(contexts(engine)) == INVOICES and contexts(engine)["SI-2026-0612"].rule_findings == []


async def test_subjects_awaiting_review_filtered_before_lookup() -> None:
    """已交人处理的发票 (有待决的带对象任务) 不取数也不报错, 即使它在财务数据中查不到."""
    missing = "SI-2026-9999"
    agent, ctx, engine = setup(rows=[], exceptions=[X1.model_copy(update={"source_ids": [missing]})])
    state = ctx.state | {"review_tasks": [pending_task(subject_id=missing)]}
    store = agent.deps.financial_store
    for name in ("sales_invoices", "delivery_notes", "contracts"):
        setattr(store, name, AsyncMock(side_effect=AssertionError("不应取数")))
    patch = await agent.run(RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, state))  # type: ignore[arg-type]
    assert engine.calls == [] and patch["procedure_runs"][0].status is RunStatus.succeeded


async def test_rerun_scope_filters_before_lookup() -> None:
    agent, ctx, engine = setup(rows=[{"id": "SI-2026-9999"}, {"id": INVOICES[1]}], rerun=[INVOICES[1]])
    store = agent.deps.financial_store
    store.sales_invoices = AsyncMock(wraps=store.sales_invoices)  # type: ignore[method-assign]
    await agent.run(ctx)
    store.sales_invoices.assert_awaited_once_with(ctx.engagement.id, [INVOICES[1]])
    assert list(contexts(engine)) == [INVOICES[1]]


async def test_invoices_without_contract_do_not_query_contracts() -> None:
    subject = "SI-2026-0547"
    agent, ctx, _ = setup(rows=[], exceptions=[X1.model_copy(update={"source_ids": [subject]})])
    agent.deps.financial_store.contracts = AsyncMock(side_effect=AssertionError("不应取合同"))  # type: ignore[method-assign]
    [subject_found] = await agent.rev02_subjects(ctx)
    assert subject_found.subject_id == subject and subject_found.data["contract"] is None


async def test_missing_linked_contract_fails_whole_run_by_a1() -> None:
    """发票关联的合同不在财务数据中: 整次执行失败, 由 A1 建失败任务, 不调判断引擎."""
    agent, ctx, engine = setup()
    agent.deps.financial_store.saved_contracts.pop("CON-002")  # type: ignore[attr-defined]
    patch, writer = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1 and engine.calls == [] and set(patch) == {"procedure_runs", "review_tasks"}
    assert patch["procedure_runs"][0].status is RunStatus.failed


# ── 判断结果 ─────────────────────────────────────────


async def test_fake_engine_gives_two_normal_tasks() -> None:
    """验收 4: 两张发票各一条待复核判断与 normal 任务."""
    agent, ctx, _ = setup()
    patch = await agent.run(ctx)
    assert [j.id for j in patch["judgments"]] == [f"J-REV-02-1-{i}" for i in INVOICES]
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [
        ("T-J-REV-02-1-SI-2026-0612-v1", TaskKind.normal),
        ("T-J-REV-02-1-SI-2026-0618-v1", TaskKind.normal),
    ]
    assert patch["exceptions"] == []


async def test_failures_mark_exception_or_fail_subject() -> None:
    """验收 5: SI-2026-0612 判断失败, 跨期例外出 v2 unattributed 并建任务; SI-2026-0618 没有规则例外, 建 failed 任务."""
    agent, ctx, _ = setup({i: failed("unattributed") for i in INVOICES})
    patch = await agent.run(ctx)
    [marked] = patch["exceptions"]
    assert (marked.id, marked.version, marked.disposition) == (X1.id, 2, ExceptionDisposition.unattributed)
    assert [(t.id, t.kind, t.subject_id) for t in patch["review_tasks"]] == [
        ("T-X-REV-08-1-R01-1-v2", TaskKind.unattributed, "SI-2026-0612"),
        ("T-RUN-REV-02-1-SI-2026-0618-v1", TaskKind.failed, "SI-2026-0618"),
    ]
    assert patch["review_tasks"][1].target_type is TargetType.ProcedureRun
    assert patch["judgments"] == [] and patch["procedure_runs"][0].status is RunStatus.succeeded
