"""A6 REV-02 收入截止性判断: 判断对象与上下文 (合成数据集设计第 5.1 节, 第 7 节第 9 行; Q13 纳入 REV-08 全部例外)."""

import re
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
    StepKind,
    StepStatus,
    TargetType,
    TaskKind,
    TrailKind,
)
from backend.contracts.records import DatasetTraits, DeliveryNote, JournalEntry, SalesInvoice
from backend.contracts.services import RuleResult
from backend.orchestration.nodes import GraphNodes
from tests.fakes import (
    NUMBAT_ACCOUNTS,
    NUMBAT_CONTRACTS,
    NUMBAT_CUTOFF_DRAFT_WITH_CORRECTION,
    NUMBAT_OBJECTIVES,
    NUMBAT_RECORDS,
    NUMBAT_RISKS,
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    contexts,
    failed,
    make_ctx,
    make_deps,
    make_run,
    numbat_contracts,
    numbat_rev08_exception,
    passed,
)
from tests.test_fieldwork_judging import pending_task

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
    real: bool = False,
) -> tuple[FieldworkAgent, RunContext, FakeJudgmentEngine]:
    """A6 与 REV-02 第 1 次执行: 财务数据为 Numbat 的发票, 发运单与 3 份合同; 状态有抽取过的合同文件,
    REV-08 的跨期例外 X1 与两份合同已确认的 REV-03 判断. outcomes 缺省两张发票都判断通过 (L2, 建 normal 任务).
    rows 替换规则 S01 选中的行 (缺省为 Numbat 的两张). real 为真时用真实判断引擎 (大模型为 mock 模式,
    读 tests/fixtures/llm/REV-02), 返回的假引擎不被调用."""
    engine = FakeJudgmentEngine(outcomes if outcomes is not None else {i: passed() for i in INVOICES})
    financial = FakeFinancialStore(  # 存储里的文件登记为 DOC-04 抽取后的 (状态写回存储), 与状态里的相同
        list(EXTRACTED),
        records=NUMBAT_RECORDS if records is None else records,
        contracts=numbat_contracts(),
        accounts=list(NUMBAT_ACCOUNTS),  # 销售分录的两个科目 (journal_lines 带科目名称与分类)
    )
    rules = FakeRuleEngine()
    if rows is not None:
        rules.results["REV-02"] = lambda run: RuleResult(exceptions=[], rows={"S01": rows})
    deps = make_deps(FakeRegistry(), FakeTrail(), financial_store=financial, rules=rules)
    if not real:
        deps = replace(deps, judgments=engine, review_llm=None)  # type: ignore[arg-type]
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
    params = deps.library.resolve_params(
        ctx.template, ctx.engagement, state, RiskLevel.significant, traits=DatasetTraits(), profile=None
    )
    run = ctx.run.model_copy(update={"params_snapshot": params})
    return FieldworkAgent(deps), RunContext(ctx.engagement, procedure, ctx.template, run, state), engine  # type: ignore[arg-type]


def invoice_json(invoice_id: str) -> dict[str, Any]:
    row = SalesInvoice.model_validate(NUMBAT_RECORDS[("sales_invoice", invoice_id)])
    return {"source_type": "sales_invoice", **row.model_dump(mode="json")}


def line_json(line_id: str, name: str, category: str) -> dict[str, Any]:
    """journal_lines 的一行: 分录行各列加科目名称与分类."""
    row = JournalEntry.model_validate(NUMBAT_RECORDS[("journal_entry", line_id)])
    return {
        "source_type": "journal_entry",
        **row.model_dump(mode="json"),
        "account_name": name,
        "account_category": category,
    }


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
        "journal_lines": [
            line_json("SJ-2026-0612-1", "Trade receivables", "receivables"),
            line_json("SJ-2026-0612-2", "Sales revenue", "revenue"),
        ],
        "contract": numbat_contracts()[0].model_dump(mode="json"),
        "document": EXTRACTED[0].model_dump(mode="json"),
        "contract_judgment": {
            "id": "J-REV-03-1-CON-001",
            "conclusion": rev03("CON-001").conclusion,
            "review_status": "confirmed",
        },
    }
    assert jctx.data["delivery_notes"][0]["signed_date"] == "2026-07-03"
    lines = [(x["account_code"], x["debit_cents"], x["credit_cents"]) for x in jctx.data["journal_lines"]]
    assert lines == [("1100", 14200000, 0), ("4100", 0, 14200000)]  # 需求 R100 验收 1
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
    assert [x["id"] for x in jctx.data["journal_lines"]] == ["SJ-2026-0618-1", "SJ-2026-0618-2"]


async def test_invoice_without_contract_has_null_contract_fields() -> None:
    """验收 3: 发票没有合同时 contract, document, contract_judgment 为 null."""
    r02 = X1.model_copy(update={"id": "X-REV-08-1-R02-1", "rule_id": "R02", "source_ids": ["SI-2026-0547"]})
    agent, ctx, engine = setup({"SI-2026-0547": passed()}, exceptions=[r02], rows=[])
    await agent.run(ctx)
    data = contexts(engine)["SI-2026-0547"].data
    assert data["invoice"] == invoice_json("SI-2026-0547")
    assert (data["contract"], data["document"], data["contract_judgment"]) == (None, None, None)
    # 只取该发票过账分录 (按凭证号) 的行: 收款分录 CR-SI-2026-0547 的单据号同为该发票, 不是它的过账分录
    assert [x["id"] for x in data["journal_lines"]] == ["SJ-2026-0547-1", "SJ-2026-0547-2"]


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


def setup_missing(change: str) -> tuple[FieldworkAgent, RunContext, FakeJudgmentEngine]:
    """同 setup, 但判断对象要用的数据缺一样: invoice 缺发票 SI-2026-0618, contract 缺合同 CON-002, document 缺合同
    CON-002 的文件 (状态与存储里都没有; 只在存储里的会补查, 需求 R14), journal 缺发票 SI-2026-0618 指向的分录行
    SJ-2026-0618-2 (需求 R100)."""
    records = dict(NUMBAT_RECORDS)
    documents = list(EXTRACTED)
    if change == "invoice":
        del records[("sales_invoice", "SI-2026-0618")]
    if change == "journal":
        del records[("journal_entry", "SJ-2026-0618-2")]
    if change == "document":
        documents = [EXTRACTED[0], EXTRACTED[2]]
    agent, ctx, engine = setup(records=records, documents=documents)
    if change == "document":
        financial = agent.deps.financial_store
        financial.document_refs[:] = [d for d in financial.document_refs if d.file_name != "CON-002.pdf"]  # type: ignore[attr-defined]
    if change == "contract":
        agent.deps.financial_store.saved_contracts.pop("CON-002")  # type: ignore[attr-defined]
    return agent, ctx, engine


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("invoice", "发票不在财务数据中: SI-2026-0618"),
        ("contract", "合同 CON-002 不在财务数据中"),
        ("document", "合同 CON-002 的文件"),
        ("journal", "发票 SI-2026-0618 的分录行 SJ-2026-0618-2 不在财务数据中"),
    ],
)
async def test_missing_data_raises(change: str, message: str) -> None:
    agent, ctx, _ = setup_missing(change)
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


async def test_invoice_without_journal_entry_has_no_journal_lines() -> None:
    """需求 R100 验收 2: 发票没有 journal_entry_id 时 journal_lines 为 [], 不取分录."""
    records = dict(NUMBAT_RECORDS)
    key = ("sales_invoice", "SI-2026-0618")
    records[key] = {**records[key], "journal_entry_id": None}
    agent, ctx, engine = setup(records=records)
    await agent.run(ctx)
    found = contexts(engine)
    assert found["SI-2026-0618"].data["journal_lines"] == []
    assert len(found["SI-2026-0612"].data["journal_lines"]) == 2  # 别的发票照常


@pytest.mark.parametrize("change", ["contract", "journal"])
async def test_missing_data_fails_whole_run_by_a1(change: str) -> None:
    """发票关联的合同, 或发票指向的分录行 (需求 R100 验收 2) 不在财务数据中: 整次执行失败, 由 A1 建失败任务
    T-RUN-REV-02-1-v1, 不调判断引擎. 抛的错见 test_missing_data_raises."""
    agent, ctx, engine = setup_missing(change)
    patch, writer, _ = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1 and engine.calls == [] and set(patch) == {"procedure_runs", "review_tasks"}
    assert patch["procedure_runs"][0].status is RunStatus.failed
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [("T-RUN-REV-02-1-v1", TaskKind.failed)]


# ── 提示词 (需求 R100) ─────────────────────────────────────────


async def rendered_prompt(invoice_id: str, **options: Any) -> str:
    """REV-02 对该发票渲染出的提示词 (上下文取 A6 实际组装的, 渲染用真实判断引擎, 同 JudgmentEngine.judge)."""
    agent, ctx, engine = setup(**options)
    await agent.run(ctx)
    jctx = contexts(engine)[invoice_id]
    real = make_deps(FakeRegistry(), FakeTrail()).judgments
    variables = {
        "subject_id": jctx.subject_id,
        "task": jctx.task,
        "data": jctx.data,
        "rule_findings": [x.model_dump(mode="json") for x in jctx.rule_findings],
        "standards": real.standards(ctx.template),
    }
    return real.render(ctx.template, variables)


def prompt_sections(prompt: str) -> dict[str, list[str]]:
    """提示词按节切开, 每节为它的各句 (按句号切, 不分大小写): opening 开头一句, task 任务一节, criterion 1-4 各条
    判断标准, misstatement 与 more_evidence 两种处理, evidence table 证据表的各行 (每行一项)."""
    lines = prompt.splitlines()

    def between(start: str, end: str) -> list[str]:
        first = next(i for i, x in enumerate(lines) if x.startswith(start))
        return lines[first + 1 : next(i for i in range(first + 1, len(lines)) if lines[i].startswith(end))]

    def sentences(chosen: list[str]) -> list[str]:
        return [c.casefold() for line in chosen for c in re.split(r"(?<=\.)\s+", line) if c]

    criteria = between("## Criteria", "## proposed_action")
    actions = between("## proposed_action", "## Output")
    sections = {
        "opening": sentences(lines[:1]),
        "task": sentences(between("## Task", "## Inputs")),
        "misstatement": sentences([x for x in actions if x.startswith("- `misstatement`")]),
        "more_evidence": sentences([x for x in actions if x.startswith("- `more_evidence`")]),
        "evidence table": [x.casefold() for x in lines if x.startswith("| ")],
    }
    for n in range(1, 5):
        sections[f"criterion {n}"] = sentences([x for x in criteria if x.startswith(f"{n}. ")])
    return sections


# REV-02 提示词口径的关键词 (需求 R100): 所在节里须有一句同时含这几个词 (不分大小写), 不比逐字原文
REV02_RULES = {
    "期间为财务年度": ("opening", ("financial year ended",)),
    "商品或服务": ("task", ("goods or services",)),
    "列举转移事件": ("criterion 1", ("signature", "acceptance", "completion", "over time")),
    "时点转移引 P38": ("criterion 1", ("point in time", "aasb15-p38")),
    "随时间转移引 P35": ("criterion 1", ("over time", "aasb15-p35")),
    "没有合同取发运或服务完成记录": ("criterion 1", ("no contract", "service-completion")),
    "服务业务两列的含义": ("criterion 2", ("service", "shipped_date", "start", "signed_date", "complete")),
    "随时间转移按服务期间": ("criterion 3", ("over time", "service period", "aasb15-p35")),
    "R03 类例外不单独构成截止错报": ("criterion 4", ("r03", "not", "cutoff misstatement")),
    "错报金额取不含 GST 的收入行": ("misstatement", ("amount_cents", "journal line", "revenue", "gst")),
    "缺发运或服务完成记录才要更多证据": ("more_evidence", ("shipping", "service-completion record")),
    "证据表的分录行": ("evidence table", ("journal_entry", "internal_original")),
    "证据表的银行流水": ("evidence table", ("bank_transaction", "external_copy")),
}


async def test_prompt_carries_journal_lines_and_service_rules() -> None:
    """验收 4: SI-2026-0612 的提示词渲染一次, 按节查各条口径的关键词 (REV02_RULES); 另查数据: 分录行块带收入行
    SJ-2026-0612-2, Standards 段有 AASB15-P35 一行 (程序 YAML 的 standards, 不是正文里的引用)."""
    prompt = await rendered_prompt("SI-2026-0612")
    sections = prompt_sections(prompt)
    missing = [
        rule
        for rule, (name, words) in REV02_RULES.items()
        if not any(all(w in c for w in words) for c in sections[name])
    ]
    assert missing == []
    assert '"id": "SJ-2026-0612-2"' in prompt and "\n- AASB15-P35 (" in prompt


async def test_prompt_without_contract_does_not_ask_for_one() -> None:
    """验收 4: 发票没有挂合同时, more_evidence 的条件里没有 "没有合同" (按发运或服务完成记录判断)."""
    r02 = X1.model_copy(update={"id": "X-REV-08-1-R02-1", "rule_id": "R02", "source_ids": ["SI-2026-0547"]})
    prompt = await rendered_prompt("SI-2026-0547", outcomes={"SI-2026-0547": passed()}, exceptions=[r02], rows=[])
    assert "No contract is linked to this invoice." in prompt  # 输入里照实写明没有合同
    [line] = [x for x in prompt.splitlines() if x.startswith("- `more_evidence`")]
    assert "contract" not in line


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


# ── R44 模拟模型输出 ─────────────────────────────────────────


async def test_fixtures_pass_real_engine_in_one_attempt() -> None:
    """验收 3: 两份 mock 输出经真实判断引擎各一次通过, 无失败; SI-2026-0612 解释跨期例外."""
    agent, ctx, _ = setup(real=True)
    patch = await agent.run(ctx)
    trail = agent.deps.trail
    assert isinstance(trail, FakeTrail)
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert [e.payload["fixture_key"] for e in calls] == INVOICES
    assert all(e.payload["attempt"] == 1 and e.payload["level_failed"] is None for e in calls)
    judgments = {j.subject_id: j for j in patch["judgments"]}
    assert sorted(judgments) == INVOICES
    for j in judgments.values():  # 每一步的指针都解析得了 (页码在范围内, 行存在), 没有步骤被标为无支撑
        assert all(step.status is not StepStatus.unsupported for step in j.reasoning_chain), j.id
    assert judgments["SI-2026-0612"].exception_ids == [X1.id]
    assert judgments["SI-2026-0618"].exception_ids == []
    assert [t.id for t in patch["review_tasks"]] == [f"T-J-REV-02-1-{i}-v1" for i in INVOICES]


async def test_si0612_fixture_is_cutoff_misstatement() -> None:
    """验收 1: 建议认定错报, 草稿 {factual, Revenue, Cutoff, 14200000, overstated}; 证据为发票入账日, 发运单签收日,
    CON-001 第 5.1 条 (第 2 页) 与两条准则."""
    agent, ctx, _ = setup(real=True)
    [j] = [j for j in (await agent.run(ctx))["judgments"] if j.subject_id == "SI-2026-0612"]
    assert j.proposed_action is ProposedAction.misstatement
    assert j.misstatement_draft == NUMBAT_CUTOFF_DRAFT_WITH_CORRECTION
    assert [(p.source_type, p.source_id, p.locator, p.page) for p in j.evidence] == [
        ("sales_invoice", "SI-2026-0612", "posting_date", None),
        ("delivery_note", "DN-2026-0612", "signed_date", None),
        ("document", NUMBAT_CONTRACTS[0].id, "CON-001-T1", 2),
        ("standard", "AASB15-P38", "para 38", None),
        ("standard", "AASB15-P31", "para 31", None),
    ]
    assert "X-REV-08-1-R01-1" in " ".join(step.text for step in j.reasoning_chain)  # 推理链回应了规则例外
    assert j.reasoning_chain[-1].kind is StepKind.inference


async def test_si0618_fixture_accepts() -> None:
    """验收 2: SI-2026-0618 于 2026-06-30 签收, 期间正确, accept, 无错报草稿."""
    agent, ctx, _ = setup(real=True)
    [j] = [j for j in (await agent.run(ctx))["judgments"] if j.subject_id == "SI-2026-0618"]
    assert j.proposed_action is ProposedAction.accept and j.misstatement_draft is None
    assert ("delivery_note", "DN-2026-0618", "signed_date") in {
        (p.source_type, p.source_id, p.locator) for p in j.evidence
    }
    assert "30 June 2026" in j.conclusion
