"""需求 R269: 演示数据导入 (services/demo_import). Xero 演示项目在空的假存储上按阶段导入: 承接文件, 快照与补造账表,
外勤文件, 待审财报; 再执行不重复写, 不盖掉之后的改动; 中途失败后接着做完; 与 R97 的预置状态逐对象相同. 不连库 (连库见
test_demo_import_db). 读快照与补造目录, 标 external.

整段流程 (启动 → ACC-01 闸门经数据管理页的接口导入承接 → approve → p1_await_data 导入外勤 → 再执行 → 确认数据已齐
→ 导入财报) 在模块级 fixture 里只跑一遍, 各用例看各停点留下的状态.
"""

import asyncio
import logging
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.adapters.xero_supplements import plan
from backend.agents.ingest import DocumentIntake
from backend.contracts.data import (
    XERO_TABLES,
    DataInput,
    DemoImportResult,
    DemoSummary,
    DocumentDetailsEdit,
    TableKind,
    TableVersion,
)
from backend.contracts.errors import DataRejected, DecisionRejected
from backend.contracts.models import (
    Contract,
    DocumentRef,
    DocumentType,
    Engagement,
    ProcedureStatus,
    Reliability,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import LLMClient
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.data_service import DataService
from backend.services.demo_import import DEMO_JOB, DETAILS_REASON, NO_DEMO, RETYPE_REASON, DemoImport
from tests.external import supplements_dir
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeTrail,
    LayeredMockLLM,
    api_client,
    wait_idle,
)
from tests.xero_fakes import (
    REPO,
    XERO_DATASET,
    XeroFakeDataStore,
    make_xero_deps,
    xero_engagement,
    xero_financial_store,
    xero_profile,
    xero_tables,
    xero_uploads,
)
from tests.xero_flow import (
    READY_REASON,
    approve_acceptance,
    confirm_ready,
    demo_source,
    drain,
    import_stage,
    offline_runner,
    to_acceptance_gate,
)

pytestmark = pytest.mark.external
EID = ENGAGEMENT.id
ACTOR = ENGAGEMENT.owner_id  # 发起导入的项目负责人 (同 R97 预置的上传人)
LLM_XERO = REPO / "tests" / "fixtures" / "llm_xero"  # 大模型固定输出正本 (验收 8: MockLLMClient 读它)
EMPTY = {k.value: [] for k in (*XERO_TABLES, TableKind.delivery_notes)}  # 还没有数据的项目


@dataclass
class Env:
    """一个空的 Xero 演示项目: 假存储, 主图与数据管理服务 (清单来源同组装根, 按数据集名分流)."""

    runner: GraphRunner
    service: DataService
    financial: FakeFinancialStore
    data: XeroFakeDataStore
    trail: FakeTrail


def env(llm: LLMClient | None = None, engagement: Engagement | None = None, demo: Path | None = None) -> Env:
    financial = FakeFinancialStore()
    data = XeroFakeDataStore(financial, EMPTY, ready=False)
    trail = FakeTrail()
    engagements = FakeEngagementStore([xero_profile()], [engagement or xero_engagement()])
    client = llm if llm is not None else MockLLMClient(LLM_XERO)
    deps = make_xero_deps(financial_store=financial, data=data, trail=trail, engagement_store=engagements, llm=client)
    runner = offline_runner(deps)
    source = demo_source(demo) if demo is not None else demo_source()
    return Env(runner, DataService(deps, runner, demo=source), financial, data, trail)


def snapshot(e: Env) -> dict[str, Any]:
    """一个停点的状态 (副本): 文件登记, 合同, save_contract 的调用 (合同编号), 账表版本, 数据版本, 就绪, 补交请求,
    留痕."""
    return {
        "documents": list(e.financial.document_refs),
        "contracts": dict(e.financial.saved_contracts),
        "save_calls": list(e.financial.save_calls),
        "versions": list(e.data.table_versions),
        "data_version": e.data.data_version,
        "ready_at": e.data.ready_at,
        "requests": {r.id: r.status for r in e.data.data_requests.values()},
        "trail": list(e.trail.entries),
    }


def copy_store(store: FakeFinancialStore) -> FakeFinancialStore:
    """财务存储的副本 (账表, 文件登记, 合同), 供之后与 R97 的预置比对."""
    copied = FakeFinancialStore(documents=list(store.document_refs), contracts=list(store.saved_contracts.values()))
    copied.account_rows, copied.trial_balance_rows = list(store.account_rows), list(store.trial_balance_rows)
    copied.employee_rows, copied.records = list(store.employee_rows), dict(store.records)
    return copied


def actions(entries: list[TrailEntry]) -> Counter[str]:
    return Counter(str(e.payload.get("action")) for e in entries if e.kind is TrailKind.data)


def by_name(documents: list[DocumentRef]) -> dict[str, DocumentRef]:
    return {d.file_name: d for d in documents}


async def import_acceptance_over_http(e: Env) -> tuple[dict[str, Any], DemoImportResult]:
    """停在 ACC-01 闸门时经数据管理页的接口导入承接 (需求 R270): 读概览, 发起导入, 作业停住时再读概览, 放行做完后
    再读一次. 返回各响应 (gate, started 另带状态码, during, accepted) 与这次 demo_import 留痕的结果."""
    base = f"/api/v1/engagements/{EID}/data"
    hold, run = asyncio.Event(), DemoImport.run

    async def held(self: DemoImport, *args: Any) -> DemoImportResult:  # 作业先停住, 好在作业期间读概览 (不靠时序)
        await hold.wait()
        return await run(self, *args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(DemoImport, "run", held)
        async with api_client(e.runner) as client:
            client._transport.app.state.data_service = e.service  # type: ignore[attr-defined]
            got = {"gate": (await client.get(f"{base}/summary")).json()}
            started = await client.post(f"{base}/demo-import", json={"stage": "acceptance"})
            got["started"] = {"status_code": started.status_code, **started.json()}
            got["during"] = (await client.get(f"{base}/summary")).json()
            hold.set()
            await drain(e.service)
            await wait_idle(e.runner, EID)
            got["accepted"] = (await client.get(f"{base}/summary")).json()
    entry = await e.trail.last_of(EID, TrailKind.data, [DEMO_JOB])
    assert entry is not None
    return got, DemoImportResult.model_validate({k: v for k, v in entry.payload.items() if k != "action"})


@dataclass
class Flow:
    http: dict[str, Any]  # 经接口导入承接时的各响应 (import_acceptance_over_http)
    acceptance: DemoImportResult | None
    after_acceptance: dict[str, Any]
    fieldwork: DemoImportResult | None
    after_fieldwork: dict[str, Any]
    awaiting: tuple[bool, list[TableKind]]  # 外勤导入后总览的 (awaiting_data, missing_tables)
    store_after_fieldwork: FakeFinancialStore
    again_by_service: DemoImportResult | None  # 经服务再执行一次 (没写就没有留痕, 为 None)
    again: DemoImportResult  # 直接调 DemoImport.run 再执行一次的结果
    after_again: dict[str, Any]
    doc04: ProcedureStatus
    state_documents: list[DocumentRef]  # 确认就绪, 图继续之后编排状态里的文件登记 (DOC-04 交回)
    after_ready: dict[str, Any]
    statements: DemoImportResult | None
    after_statements: dict[str, Any]
    demos: dict[str, DemoSummary]  # 各停点概览的 demo 一项 (需求 R270)


async def run_flow() -> Flow:
    e = env()
    demos: dict[str, DemoSummary] = {}
    await to_acceptance_gate(e.runner, EID)
    http, acceptance = await import_acceptance_over_http(e)
    demos["gate"] = DemoSummary.model_validate(http["gate"]["demo"])  # 字段恰为 DemoSummary 的
    demos["accepted"] = DemoSummary.model_validate(http["accepted"]["demo"])
    after_acceptance = snapshot(e)
    await approve_acceptance(e.runner, EID, ACTOR)
    assert (await e.runner.app.aget_state(GraphRunner.config(EID))).next == ("p1_await_data",)
    demos["awaiting"] = await e.service.demo_summary(EID)
    fieldwork = await import_stage(e.service, EID, "fieldwork", ACTOR)
    after_fieldwork, store = snapshot(e), copy_store(e.financial)
    overview = await GraphStateQuery(e.runner.app, e.runner.deps, e.runner).overview(EID)
    awaiting = (overview.awaiting_data, list(overview.missing_tables))
    again_by_service = await import_stage(e.service, EID, "fieldwork", ACTOR)
    importer = DemoImport(e.service, plan(demo_source(), XERO_DATASET))
    async with e.service.holding(EID, []):
        again = await importer.run(EID, "fieldwork", ACTOR)
    await wait_idle(e.runner, EID)  # holding 结束时把锁交给等数据的图, 图重查后照旧等
    after_again = snapshot(e)
    await confirm_ready(e.service, EID, ACTOR)
    overview = await GraphStateQuery(e.runner.app, e.runner.deps, e.runner).overview(EID)
    doc04 = next(p.status for p in overview.procedures if p.id == "DOC-04")
    state_documents = list((await e.runner.app.aget_state(GraphRunner.config(EID))).values.get("documents", []))
    after_ready = snapshot(e)
    demos["ready"] = await e.service.demo_summary(EID)
    statements = await import_stage(e.service, EID, "statements", ACTOR)
    demos["statements"] = await e.service.demo_summary(EID)
    return Flow(
        http,
        acceptance,
        after_acceptance,
        fieldwork,
        after_fieldwork,
        awaiting,
        store,
        again_by_service,
        again,
        after_again,
        doc04,
        state_documents,
        after_ready,
        statements,
        snapshot(e),
        demos,
    )


@pytest.fixture(scope="module")
def flow() -> Flow:
    return asyncio.run(run_flow())


def added(before: dict[str, Any], after: dict[str, Any]) -> list[TrailEntry]:
    """两个停点之间新增的留痕."""
    return after["trail"][len(before["trail"]) :]


# ── 拒绝 (验收 1) ──


@pytest.mark.parametrize(
    ("dataset", "stage", "locked", "code", "message"),
    [
        ("demo", "acceptance", False, "INVALID_VALUE", NO_DEMO),  # Numbat 没有补造目录
        ("xero/20261017-mock", "acceptance", False, "INVALID_VALUE", NO_DEMO),
        (None, "acceptance", False, "INVALID_VALUE", NO_DEMO),  # 新建的客户, 没有数据集
        (XERO_DATASET, "planning", False, "INVALID_VALUE", "没有阶段 planning"),
        (XERO_DATASET, "manual", False, "INVALID_VALUE", "人工任务页上传"),  # 管理层声明书不经演示导入
        (XERO_DATASET, "acceptance", True, "ENGAGEMENT_LOCKED", ""),
    ],
)
async def test_rejected_without_writing(dataset: str | None, stage: str, locked: bool, code: str, message: str) -> None:
    e = env(engagement=xero_engagement().model_copy(update={"dataset": dataset, "locked": locked}))
    with pytest.raises(DataRejected) as rejected:
        await e.service.demo_import(EID, stage, ACTOR)
    assert rejected.value.code == code and message in str(rejected.value)
    assert (e.data.table_versions, e.financial.document_refs, e.trail.entries) == ([], [], [])
    assert not e.runner.is_running(EID)  # 拒绝时不占项目锁


async def test_busy_and_tampered_supplements_are_rejected(tmp_path: Path) -> None:
    """项目锁被占用 ENGAGEMENT_BUSY; 补造目录被改过 (与 manifest 的哈希不符) INVALID_VALUE, 消息只列文件名. 都不写."""
    busy = env()
    busy.runner.acquire(EID)
    with pytest.raises(DecisionRejected) as taken:  # 取项目锁的拒绝, 接口同样报 409
        await busy.service.demo_import(EID, "fieldwork", ACTOR)
    assert taken.value.code == "ENGAGEMENT_BUSY" and busy.data.table_versions == []
    copy = tmp_path / "xero_demo"
    shutil.copytree(supplements_dir("20261017-q1"), copy / "20261017-q1")
    links = copy / "20261017-q1" / "contract_links.csv"
    links.write_bytes(links.read_bytes() + b" ")
    tampered = env(demo=copy)
    with pytest.raises(DataRejected) as rejected:
        await tampered.service.demo_import(EID, "fieldwork", ACTOR)
    assert rejected.value.code == "INVALID_VALUE" and "contract_links.csv" in str(rejected.value)
    assert (tampered.data.table_versions, tampered.financial.document_refs) == ([], [])


@pytest.mark.parametrize("call", ["run", "register_uploads", "process_uploads", "assign_type", "save_details"])
async def test_locked_steps_refuse_to_run_without_the_lock(call: str) -> None:
    """演示导入与拆出的持锁内函数没持项目锁时抛 RuntimeError, 什么都不写 (免得与图或别的作业并发写)."""
    e = env()
    service = e.service
    document = DocumentRef.model_validate(
        {**xero_financial_store().document_refs[0].model_dump(), "status": "received"}
    )
    calls = {
        "run": lambda: DemoImport(service, plan(demo_source(), XERO_DATASET)).run(EID, "acceptance", ACTOR),
        "register_uploads": lambda: service.register_uploads(EID, ["a.pdf"], [b"%PDF-1.4"], ACTOR, None),
        "process_uploads": lambda: service.process_uploads(EID, xero_engagement(), [document], {}, {}),
        "assign_type": lambda: service.assign_type(
            DocumentIntake(service.deps), EID, document, DocumentType.minutes, "x", ACTOR
        ),
        "save_details": lambda: service.save_details(EID, {document.id: DocumentDetailsEdit(reason="x")}, ACTOR),
    }
    with pytest.raises(RuntimeError, match="须先取项目锁"):
        await calls[call]()
    assert (e.financial.document_refs, e.trail.entries, e.data.data_version) == ([], [], 0)


# ── 承接 (验收 2) ──


def test_acceptance_uploads_the_engagement_documents(flow: Flow) -> None:
    """停在 ACC-01 闸门时导入承接: 上传 2 份 (上传人为发起人), A3 分类为 minutes, engagement_letter, 补登记文件日期;
    数据版本 0 → 1; 体检一次, 缺账表与待审财报的请求为 open (体检另对没有合同提出 REQ-contract)."""
    assert flow.acceptance == DemoImportResult(
        stage="acceptance", status="imported", uploaded=["appointment.pdf", "engagement_letter.pdf"], data_version=1
    )
    state = flow.after_acceptance
    assert [(d.file_name, d.type, d.status, d.uploaded_by, d.document_date) for d in state["documents"]] == [
        ("appointment.pdf", DocumentType.minutes, "classified", ACTOR, date(2026, 8, 27)),
        ("engagement_letter.pdf", DocumentType.engagement_letter, "classified", ACTOR, date(2026, 8, 28)),
    ]
    assert state["data_version"] == 1
    assert actions(state["trail"]) == Counter(
        {"upload": 2, "classify": 2, "edit": 2, "check": 1, "request_raised": 5, "demo_import": 1}
    )
    assert state["requests"] == dict.fromkeys(
        ["REQ-accounts", "REQ-trial_balance", "REQ-journal_entries", "REQ-contract", "REQ-financial_statements"], "open"
    )
    assert [e.procedure_id for e in state["trail"] if e.kind is TrailKind.llm_call] == ["DOC-CLASSIFY"] * 2
    edits = [e for e in state["trail"] if e.payload.get("action") == "edit"]  # 补登记: 记发起人与清单的理由
    assert [(e.actor, e.payload["reason"]) for e in edits] == [(ACTOR, DETAILS_REASON)] * 2


# ── 外勤 (验收 3) ──


def test_fieldwork_writes_the_snapshot_and_supplement_tables(flow: Flow) -> None:
    """快照七张表一版 (source xero, 数据版本 2, sync 留痕 7 条; 应付账单 6 行, 需求 R178), 补造表四版 (source
    import, 3-6, 按清单的 TableKind 顺序); 3 份合同各一版 (7-9, 上传后抽出合同记数据变动 contracts), 补登记一批 (10)."""
    versions: list[TableVersion] = flow.after_fieldwork["versions"]
    assert [(v.kind, v.source, v.file_name, v.records, v.data_version) for v in versions] == [
        (TableKind.accounts, "xero", None, 62, 2),
        (TableKind.trial_balance, "xero", None, 33, 2),
        (TableKind.journal_entries, "xero", None, 1017, 2),
        (TableKind.sales_invoices, "xero", None, 42, 2),
        (TableKind.supplier_bills, "xero", None, 6, 2),
        (TableKind.bank_accounts, "xero", None, 2, 2),
        (TableKind.bank_transactions, "xero", None, 146, 2),
        (TableKind.sales_invoices, "import", "contract_links.csv", 42, 3),
        (TableKind.bank_accounts, "import", "bank_accounts.csv", 2, 4),
        (TableKind.delivery_notes, "import", "delivery_notes.csv", 24, 5),
        (TableKind.employees, "import", "payroll/employees.csv", 3, 6),
    ]
    assert {v.created_by for v in versions} == {ACTOR}
    assert flow.fieldwork is not None and flow.fieldwork.tables == [v.kind for v in versions]
    assert flow.after_fieldwork["data_version"] == flow.fieldwork.data_version == 10
    assert actions(added(flow.after_acceptance, flow.after_fieldwork)) == Counter(
        {
            "sync": 7,
            "import": 4,
            "upload": 19,
            "classify": 19,
            "edit": 19,
            "check": 1,
            "request_received": 4,
            "request_raised": 1,
            "demo_import": 1,
        }
    )


def test_fieldwork_uploads_classifies_extracts_and_registers(flow: Flow) -> None:
    """19 份上传, A3 分类 19 次, 3 份合同随即抽取 (CON-X01 至 X03 第 1 版, 执行 ID 以 UPLOAD-{文件 ID}- 开头, 置
    extracted), 其余 16 份 classified; 补登记后类型, 可靠性, 文件日期, 关联对象等于清单; 账表与合同的请求收到, 审批
    日志与待审财报的请求 open; 不确认就绪, 总览仍在等数据而不缺表."""
    assert flow.fieldwork is not None and (flow.fieldwork.retyped, flow.fieldwork.needs_manual) == ([], [])
    planned = xero_uploads("fieldwork")
    assert flow.fieldwork.uploaded == [u.file_name for u in planned]
    found = by_name(flow.after_fieldwork["documents"])
    for u in planned:
        d = found[u.file_name]
        assert (d.id, d.type, d.reliability, d.document_date, d.related_ids) == (
            u.document_id,
            u.document_type,
            u.reliability,
            u.document_date,
            u.related_ids,
        ), u.file_name
        assert d.status == ("extracted" if u.document_type is DocumentType.contract else "classified"), u.file_name
    assert Counter(found[u.file_name].reliability for u in planned) == {
        Reliability.external_copy: 10,
        Reliability.internal_original: 8,
        Reliability.internal_copy: 1,
    }
    contracts: dict[str, Contract] = flow.after_fieldwork["contracts"]
    assert sorted(contracts) == ["CON-X01", "CON-X02", "CON-X03"]
    for c in contracts.values():
        assert c.version == 1 and c.extracted_by_run.startswith(f"UPLOAD-{c.document_id}-"), c.id
    calls = Counter(
        e.procedure_id for e in added(flow.after_acceptance, flow.after_fieldwork) if e.kind is TrailKind.llm_call
    )
    assert calls == {"DOC-CLASSIFY": 19, "DOC-04": 3}
    assert flow.after_fieldwork["requests"] == {
        "REQ-accounts": "received",
        "REQ-trial_balance": "received",
        "REQ-journal_entries": "received",
        "REQ-contract": "received",
        "REQ-financial_statements": "open",
        "REQ-approval_log": "open",  # Xero 的录入人为连接器占位值
    }
    assert flow.after_fieldwork["ready_at"] is None
    assert flow.awaiting == (True, [])


def test_contracts_extracted_on_upload_name_the_audited_entity(flow: Flow) -> None:
    """上传时 A3 对 3 份合同各按 DOC-04 调一次大模型 (fixture_key 为合同编号, 一次通过), 各 save_contract 一次, 提示词
    里的被审计单位是项目画像的法定名称 Demo Company (AU) Pty Ltd; 条款按页编号, CON-X01 有 T1 至 T3, CON-X02 与 X03 有
    T1 至 T4, 各合同 T1 为 control_transfer, T2 为 payment (需求 R108 验收 2). 防上传路径用错客户画像, 重试才过, 或同一
    合同写两次."""
    calls = [
        e.payload
        for e in added(flow.after_acceptance, flow.after_fieldwork)
        if e.kind is TrailKind.llm_call and e.procedure_id == "DOC-04"
    ]
    assert sorted((p["fixture_key"], p["attempt"]) for p in calls) == [(f"CON-X0{n}", 1) for n in (1, 2, 3)]
    assert all("The audited entity is Demo Company (AU) Pty Ltd;" in p["messages"][0]["content"] for p in calls)
    saves = flow.after_fieldwork["save_calls"][len(flow.after_acceptance["save_calls"]) :]
    assert sorted(saves) == [f"CON-X0{n}" for n in (1, 2, 3)]
    contracts: dict[str, Contract] = flow.after_fieldwork["contracts"]
    assert {k: [t.id for t in c.terms] for k, c in contracts.items()} == {
        k: [f"{k}-T{n}" for n in range(1, count + 1)] for k, count in (("CON-X01", 3), ("CON-X02", 4), ("CON-X03", 4))
    }
    assert {(t.id[-2:], t.term_type.value) for c in contracts.values() for t in c.terms[:2]} == {
        ("T1", "control_transfer"),
        ("T2", "payment"),
    }


def test_offline_import_equals_the_r97_preset(flow: Flow) -> None:
    """验收 8: 空的 Xero 假存储做完承接与外勤后, 与 R97 预置的 xero_financial_store("fieldwork", contracts=True)
    逐对象相同 (账表, 文件登记含补登记字段, 合同; 上传时间不比, 合同执行 ID 只比到文件 ID)."""
    got, want = flow.store_after_fieldwork, xero_financial_store("fieldwork", contracts=True)
    assert (got.account_rows, got.trial_balance_rows, got.employee_rows) == (
        want.account_rows,
        want.trial_balance_rows,
        want.employee_rows,
    )
    assert got.records == want.records

    def documents(store: FakeFinancialStore) -> list[DocumentRef]:
        return sorted((d.model_copy(update={"uploaded_at": NOW}) for d in store.document_refs), key=lambda d: d.id)

    def contracts(store: FakeFinancialStore) -> dict[str, Contract]:
        run = {c.id: f"UPLOAD-{c.document_id}" for c in store.saved_contracts.values()}
        return {k: c.model_copy(update={"extracted_by_run": run[k]}) for k, c in store.saved_contracts.items()}

    assert documents(got) == documents(want)
    assert contracts(got) == contracts(want)


# ── 再执行 (验收 7) 与确认就绪后图继续 (验收 4) ──


def test_running_again_writes_nothing(flow: Flow) -> None:
    """同一阶段再执行: 不新增账表版本, 文件, 留痕与数据版本; 经服务再执行不记 demo_import 留痕, 直接调 run 得
    already_imported."""
    assert flow.again_by_service is None
    assert flow.again == DemoImportResult(stage="fieldwork", status="already_imported", data_version=10)
    before, after = flow.after_fieldwork, flow.after_again
    assert (after["versions"], after["documents"], after["data_version"]) == (
        before["versions"],
        before["documents"],
        before["data_version"],
    )
    assert actions(after["trail"]) == actions(before["trail"])


def test_doc04_reuses_the_uploaded_contracts_after_ready(flow: Flow) -> None:
    """确认数据已齐后图继续: DOC-04 完成, 不调大模型, 不再 save_contract, 3 份合同沿用第 1 版, 文件登记交回状态
    (extracted)."""
    assert flow.doc04 is ProcedureStatus.completed
    contracts = sorted((d.file_name, d.status) for d in flow.state_documents if d.type is DocumentType.contract)
    assert contracts == [(f"CON-X0{n}.pdf", "extracted") for n in (1, 2, 3)]
    new = added(flow.after_again, flow.after_ready)
    assert [e.procedure_id for e in new if e.kind is TrailKind.llm_call] == []
    assert flow.after_ready["contracts"] == flow.after_fieldwork["contracts"]
    assert flow.after_ready["save_calls"] == flow.after_again["save_calls"]
    assert [e.payload.get("reason") for e in new if e.payload.get("action") == "ready"] == [READY_REASON]


# ── 财报 (验收 6) ──


def test_statements_upload_answers_the_open_request(flow: Flow) -> None:
    """REQ-financial_statements 为 open: 按请求上传 fs_v1.pdf, 不调大模型分类, classify 留痕带 request_id; 请求转
    received; 补登记 internal_original, 2026-10-22, [FS]."""
    assert flow.statements is not None and flow.statements.uploaded == ["fs_v1.pdf"]
    fs = by_name(flow.after_statements["documents"])["fs_v1.pdf"]
    assert (fs.type, fs.status, fs.reliability, fs.document_date, fs.related_ids) == (
        DocumentType.financial_statements,
        "classified",
        Reliability.internal_original,
        date(2026, 10, 22),
        ["FS"],
    )
    new = added(flow.after_ready, flow.after_statements)
    assert [e.procedure_id for e in new if e.kind is TrailKind.llm_call] == []
    [classify] = [e.payload for e in new if e.payload.get("action") == "classify"]
    assert classify["request_id"] == "REQ-financial_statements"
    assert flow.after_statements["requests"]["REQ-financial_statements"] == "received"


async def test_statements_without_an_open_request_are_classified() -> None:
    """请求不在 open (项目没有体检过, 没有这项请求) 时照常由 A3 分类为 financial_statements, 再补登记."""
    e = env()
    result = await import_stage(e.service, EID, "statements", ACTOR)
    assert result is not None and result.uploaded == ["fs_v1.pdf"]
    [fs] = e.financial.document_refs
    assert (fs.type, fs.reliability, fs.document_date) == (
        DocumentType.financial_statements,
        Reliability.internal_original,
        date(2026, 10, 22),
    )
    assert [x.procedure_id for x in e.trail.entries if x.kind is TrailKind.llm_call] == ["DOC-CLASSIFY"]


# ── 分类与清单不符 (验收 5), 之后的改动不盖掉 ──


async def test_wrong_classification_is_retyped_by_the_plan(tmp_path: Path) -> None:
    """覆盖层改三处: ridgeway_order_CRM08-12 分为 contract, CON-X02 分为 agreement, CON-X03 的合同抽取不合格.
    - 导入按清单指定类型 (理由 RETYPE_REASON, 记发起人), retyped 列出前两份: ridgeway 改回 agreement (当合同抽取时
      没有固定输出而转了人工, 改类型后不再待人工); CON-X02 改回 contract 后随即抽取, 作业号为改类型那条留痕.
    - CON-X03 抽取不合格, needs_manual 列出它, 没有合同.
    - 之后审计师另导入一版 bank_accounts, 再执行不盖掉, 也什么都不写."""
    overlay = tmp_path / "overlay"
    (overlay / "DOC-CLASSIFY").mkdir(parents=True)
    (overlay / "DOC-04").mkdir()
    for stem, kind in (("ridgeway_order_CRM08-12", "contract"), ("CON-X02", "agreement")):
        reason = f"Overlay: classified as {kind}."
        (overlay / "DOC-CLASSIFY" / f"{stem}.json").write_text(
            f'{{"document_type": "{kind}", "reason": "{reason}"}}', encoding="utf-8"
        )
    (overlay / "DOC-04" / "CON-X03.json").write_text('{"counterparty": "Overlay"}', encoding="utf-8")
    e = env(llm=LayeredMockLLM(overlay, LLM_XERO))
    result = await import_stage(e.service, EID, "fieldwork", ACTOR)
    assert result is not None
    assert (result.retyped, result.needs_manual) == (
        ["CON-X02.pdf", "ridgeway_order_CRM08-12.pdf"],
        ["CON-X03.pdf"],
    )
    con2 = by_name(e.financial.document_refs)["CON-X02.pdf"]
    [retype] = [
        x for x in e.trail.entries if x.payload.get("action") == "classify" and x.payload["document_id"] == con2.id
    ][1:]
    assert (retype.actor, retype.payload["document_type"]) == (ACTOR, "contract")
    contract = e.financial.saved_contracts["CON-X02"]
    assert (con2.status, contract.version, contract.extracted_by_run) == (
        "extracted",
        1,
        f"UPLOAD-{con2.id}-{retype.id}",
    )
    assert by_name(e.financial.document_refs)["CON-X03.pdf"].status == "needs_manual"
    assert "CON-X03" not in e.financial.saved_contracts
    order = by_name(e.financial.document_refs)["ridgeway_order_CRM08-12.pdf"]
    assert (order.type, order.status, order.reliability) == (
        DocumentType.agreement,
        "classified",
        Reliability.external_copy,
    )
    decisions = [
        x.payload | {"actor": x.actor}
        for x in e.trail.entries
        if x.payload.get("action") == "classify" and x.payload["document_id"] == order.id
    ]
    assert [(x["actor"], x["document_type"], x["reason"]) for x in decisions] == [
        ("A3", "contract", "Overlay: classified as contract."),
        (ACTOR, "agreement", RETYPE_REASON),
    ]
    assert sorted(e.financial.saved_contracts) == ["CON-X01", "CON-X02"]
    banks = [b.model_copy(update={"bank_name": "Auditor-confirmed bank"}) for b in xero_tables()["bank_accounts"]]
    async with e.service.holding(EID, ["bank_accounts"]):
        await e.service.write_tables(
            EID, {TableKind.bank_accounts: banks}, source="import", actor="U-AUDITOR", file_name="bank_confirmed.csv"
        )
    await wait_idle(e.runner, EID)
    versions = list(e.data.table_versions)
    assert await import_stage(e.service, EID, "fieldwork", ACTOR) is None
    assert e.data.table_versions == versions and versions[-1].file_name == "bank_confirmed.csv"


async def test_later_changes_by_the_auditor_are_kept() -> None:
    """导入后审计师补登记了一份, 改了另一份的类型: 再执行不盖掉 (补登记过的不再补, 最近一次类型决定不是 A3 的不改),
    什么都没写."""
    e = env()
    await import_stage(e.service, EID, "acceptance", ACTOR)
    files = by_name(e.financial.document_refs)
    edit = DocumentDetailsEdit(document_date=date(2026, 8, 26), reason="核对原件的签署日")
    await e.service.set_document_details(EID, {files["appointment.pdf"].id: edit}, "U-AUDITOR")
    await e.service.set_document_type(
        EID, files["engagement_letter.pdf"].id, DocumentType.correspondence, "附函", "U-AUDITOR"
    )
    await drain(e.service)
    before = list(e.trail.entries)
    assert await import_stage(e.service, EID, "acceptance", ACTOR) is None
    files = by_name(e.financial.document_refs)
    assert (files["appointment.pdf"].document_date, files["engagement_letter.pdf"].type) == (
        date(2026, 8, 26),
        DocumentType.correspondence,
    )
    assert e.trail.entries == before


async def test_already_imported_still_redoes_a_stale_checkup() -> None:
    """这一阶段都已导入过 (不记留痕), 但最近一次体检已不对应当前数据 (如上次在体检之前中断) 时补做体检."""
    e = env()
    await import_stage(e.service, EID, "acceptance", ACTOR)
    e.data.record(DataInput.documents)  # 数据版本变了而没体检
    stale = e.data.last_run
    assert await import_stage(e.service, EID, "acceptance", ACTOR) is None
    assert e.data.last_run is not stale and e.data.last_run is not None
    assert e.data.last_run.data_version == e.data.data_version


# ── 中途失败后再执行 (验收 7) ──


def outcome(e: Env) -> tuple[Any, ...]:
    """比对用的最终状态: 账表版本, 文件登记 (上传时间不比), 合同, 数据版本, 各类 data 留痕的条数 (demo_import 只数
    status imported 的)."""
    versions = [(v.kind, v.source, v.file_name, v.records, v.data_version) for v in e.data.table_versions]
    documents = sorted(
        (d.model_copy(update={"uploaded_at": NOW}) for d in e.financial.document_refs), key=lambda d: d.id
    )
    contracts = {k: (c.version, c.document_id, c.terms) for k, c in e.financial.saved_contracts.items()}
    imported = [x for x in e.trail.entries if x.payload.get("status") != "failed"]
    return versions, documents, contracts, e.data.data_version, actions(imported)


async def test_failure_midway_then_again_ends_like_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """外勤阶段第 10 份上传抛错 (作业中断, 已写的保留, 锁释放) 后再执行: 最终状态与一次做完相同. 中断时记一条
    status failed 的 demo_import (阶段与错误类别, 不带错误文本), 概览的 last 读得到, 这一阶段不算导入过 (需求 R270)."""
    once = env()
    await import_stage(once.service, EID, "fieldwork", ACTOR)
    broken = env()
    store = broken.runner.deps.evidence_store
    save, calls = store.save, Counter[str]()

    async def failing(eid: str, file_name: str, data: bytes, **kwargs: Any) -> DocumentRef:
        calls["save"] += 1
        if calls["save"] == 10:
            raise OSError("磁盘写满 (测试注入)")
        return await save(eid, file_name, data, **kwargs)

    monkeypatch.setattr(store, "save", failing)
    failed = await import_stage(broken.service, EID, "fieldwork", ACTOR)
    assert failed == DemoImportResult(stage="fieldwork", status="failed", error="OSError", data_version=5)
    [entry] = [x for x in broken.trail.entries if x.payload.get("status") == "failed"]
    assert "磁盘写满" not in str(entry.model_dump())
    demo = await broken.service.demo_summary(EID)
    assert (demo.stages_done, demo.last) == ([], failed)
    assert len(broken.financial.document_refs) == 9 and not broken.runner.is_running(EID)
    await import_stage(broken.service, EID, "fieldwork", ACTOR)
    assert outcome(broken) == outcome(once)


async def test_failure_after_everything_was_written_then_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """承接阶段全部写完 (补登记之后) 读数据版本时出错, 记 failed; 再导入一次已没有可写的, 仍记一条 imported
    (接着做完了), 算导入过, 不再建议. 已导入过的阶段再执行时出错 (什么都没写之前) 又不算导入过, 再建议这一阶段,
    再导入一次记 imported; 别的阶段记了 failed 不算这一阶段没做完. 防失败之后卡在 "没做完" 消不掉, 或提示再导入而
    按钮不可点 (需求 R270)."""
    e = env()
    await to_acceptance_gate(e.runner, EID)
    details, readiness = DemoImport.details, e.data.readiness
    runs, armed = Counter[str](), {"on": False}

    async def after_details(self: DemoImport, *args: Any) -> list[str]:
        found = await details(self, *args)
        runs["details"] += 1
        armed["on"] = runs["details"] in (1, 3)  # 第 1, 3 次导入在补登记之后读数据版本时出错
        return found

    async def flaky(eid: str) -> Any:
        if armed["on"]:
            armed["on"] = False
            raise ConnectionError("连接断开 (测试注入)")
        return await readiness(eid)

    monkeypatch.setattr(DemoImport, "details", after_details)
    monkeypatch.setattr(e.data, "readiness", flaky)
    got = []
    for _ in range(4):
        result = await import_stage(e.service, EID, "acceptance", ACTOR)
        demo = await e.service.demo_summary(EID)
        assert demo.last == result
        got.append(
            (result.status if result else None, result.uploaded if result else None, demo.stages_done, demo.suggested)
        )
    assert got == [
        ("failed", [], [], "acceptance"),
        ("imported", [], ["acceptance"], None),
        ("failed", [], [], "acceptance"),
        ("imported", [], ["acceptance"], None),
    ]
    assert len(e.financial.document_refs) == 2  # 两份承接文件, 只上传一次
    await DemoImport(e.service, plan(demo_source(), XERO_DATASET)).record_failure(EID, "fieldwork", ACTOR, OSError())
    assert await import_stage(e.service, EID, "acceptance", ACTOR) is None


async def test_backfill_error_is_recorded_as_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """作业之前补写留痕出错也记一条 failed (阶段与错误类别), 什么都没导入; 异常照常交后台作业记日志.
    防这次没做却看不出来."""
    e = env()

    async def broken(self: DataService, eid: str) -> None:
        raise RuntimeError("补写留痕出错 (测试注入)")

    monkeypatch.setattr(DataService, "backfill_trails", broken)
    with caplog.at_level(logging.ERROR, logger="backend.services.data_service"):
        failed = await import_stage(e.service, EID, "acceptance", ACTOR)
    assert failed == DemoImportResult(stage="acceptance", status="failed", error="RuntimeError", data_version=0)
    assert e.financial.document_refs == [] and not e.runner.is_running(EID)
    assert any("数据作业失败" in record.getMessage() for record in caplog.records)


# ── 数据管理页的入口 (需求 R270): 概览的 demo 一项随流程停的位置 ──


def test_summary_follows_where_the_flow_stops(flow: Flow) -> None:
    """停在 ACC-01 闸门建议 acceptance, 导入后不再建议; 等数据就绪建议 fieldwork; 确认就绪后待审财报的请求仍 open,
    建议 statements; 三段都导入后 stages_done 齐, 不再建议. last 为最近一次 (有写入的) 导入的结果."""
    got = {
        k: (d.available, d.stages_done, d.suggested, d.last.stage if d.last else None) for k, d in flow.demos.items()
    }
    assert got == {
        "gate": (True, [], "acceptance", None),
        "accepted": (True, ["acceptance"], None, "acceptance"),
        "awaiting": (True, ["acceptance"], "fieldwork", "acceptance"),
        "ready": (True, ["acceptance", "fieldwork"], "statements", "fieldwork"),
        "statements": (True, ["acceptance", "fieldwork", "statements"], None, "statements"),
    }
    assert (flow.demos["accepted"].last, flow.demos["ready"].last) == (flow.acceptance, flow.fieldwork)


def test_data_page_starts_the_import_over_http(flow: Flow) -> None:
    """数据管理页经 POST /data/demo-import 发起承接导入: 202, started 列出要上传的两份文件 (这一阶段不写表); 作业期间
    概览的 processing 为 2 (两份承接文件), 仍建议 acceptance (持锁的是这次导入, 不当图在跑). 防接口不按 202 返回,
    或作业期间页面看不出在导入."""
    started, during = flow.http["started"], flow.http["during"]
    assert (started["status_code"], started["status"], started["tables"], started["uploaded"]) == (
        202,
        "started",
        [],
        ["appointment.pdf", "engagement_letter.pdf"],
    )
    assert (during["processing"], during["demo"]["suggested"], during["demo"]["stages_done"]) == (2, "acceptance", [])
