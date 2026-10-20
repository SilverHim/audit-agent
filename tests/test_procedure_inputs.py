"""需求 R272: 程序缺数据时逐个等数据并提补交请求, 不在空数据上直接成功; 数据写操作结束后自动接着跑.

场景: Numbat 项目只接入就绪必需的三张表 (科目表, 两期试算表, 总账), 没有合同, 银行账户, 银行流水, 销售发票与发运单.
Agent 读的财务数据仍是 Numbat 的 (假财务数据存储), 有没有资料按假数据管理存储的行数与证据文件数判断, 资料到了
各程序的结果应与数据齐全的 Numbat 全流程 (R55) 相同. 存储用假实现.
"""

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.types import Command

from backend.agents.concierge_chat.tools import NoArgs, ToolEnv, list_open_items
from backend.contracts.data import ImportCommit, TableKind, TableStats
from backend.contracts.errors import DecisionRejected, RuleError
from backend.contracts.knowledge import ProcedureInputs
from backend.contracts.models import (
    AuditException,
    Decision,
    DocumentRef,
    DocumentType,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewRecord,
    ReviewTask,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailKind,
)
from backend.contracts.state import EngagementState, fill_defaults, initial_state
from backend.core.config import get_settings
from backend.main import recover
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.graph import after_fan_in
from backend.orchestration.inputs import input_problems, input_versions
from backend.orchestration.nodes import INPUTS_CHANGED, GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import (
    GraphRunner,
    awaiting_inputs,
    fixed_procedures,
    review_waits,
    waiting_payloads,
)
from backend.orchestration.waits import WaitKind, accepts, inputs_payload, kind_of
from backend.services.data_quality import (
    REQUEST_ORDER,
    OnHand,
    merge_requests,
    missing_inputs,
    program_requests,
    table_request,
)
from backend.services.data_service import DataService
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_TRIAL_BALANCE,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    api_client,
    make_deps,
    make_graph_nodes,
    make_runner,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_e2e_mock import real_agents, real_storage
from tests.test_flow import submit_gate
from tests.test_p2_agents_demo import replies
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
IMPORTS = f"/api/v1/engagements/{EID}/data/imports"
NOW = datetime(2026, 10, 18, tzinfo=UTC)
REQUIRED_ONLY = {  # 只接入了就绪必需的三张表 (Numbat 的行数)
    TableKind.accounts: TableStats(records=23),
    TableKind.trial_balance: TableStats(records=46, current=23, prior=23),
    TableKind.journal_entries: TableStats(records=2686),
}
WAITING = {  # PLN-01/03 通过后等数据的程序 → 还缺的资料的补交请求
    "DOC-04": ["REQ-contract"],
    "CSH-02": ["REQ-bank_accounts", "REQ-bank_transactions"],
    "CSH-04": ["REQ-bank_accounts"],
    "CSH-05": ["REQ-bank_accounts", "REQ-bank_transactions"],
    "REV-02": ["REQ-sales_invoices"],
    "REV-08": ["REQ-sales_invoices", "REQ-delivery_notes", "REQ-bank_transactions"],
}
LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)


class Short(Started):
    """只有三张必需表的 Numbat 项目: 另持数据管理存储, 财务数据存储与拿掉的合同登记."""

    data: FakeDataStore
    financial: FakeFinancialStore
    contracts: list[DocumentRef]


async def short_of_data() -> Short:
    """启动 → ACC-01 approve (P1 中 DOC-04 等合同, P1 照常走) → PLN-01/03 approve, 等后台停下.
    数据集载入视同已确认就绪 (P1 不等确认); 合同的登记先拿掉, 补跑 DOC-04 时再放回."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    data = FakeDataStore(stats=REQUIRED_ONLY)
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagements, agents=real_agents, financial_store=financial, data=data)
    s = Short(runner, store, engagements)
    s.data, s.financial = data, financial
    s.contracts = [d for d in financial.document_refs if d.type is DocumentType.contract]
    assert len(s.contracts) == 3  # 种子登记的 Numbat 合同
    financial.document_refs = [d for d in financial.document_refs if d.type is not DocumentType.contract]
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit_gate(s, acc01_decision(GateVerdict.approve, id="D-ACC"), ("gate_p1_risk",))
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    return s


def statuses(s: Started) -> dict[str, tuple[ProcedureStatus, list[str]]]:
    latest = {}
    for row in s.output_store.rows:
        if isinstance(row, Procedure) and (row.id not in latest or row.version > latest[row.id].version):
            latest[row.id] = row
    return {pid: (p.status, p.data_request_ids) for pid, p in latest.items()}


def runs(s: Started) -> list[str]:
    return sorted({r.id for r in s.output_store.rows if isinstance(r, ProcedureRun)})


async def import_csv(client: httpx.AsyncClient, name: str) -> None:
    """经导入接口上传 data/demo 的原文件并按系统建议提交."""
    preview = (await client.post(IMPORTS, files={"files": (name, (DEMO / name).read_bytes(), "text/csv")})).json()
    body = {
        "kind": preview["suggested_kind"],
        "sheet": preview["sheet"],
        "date_format": preview["suggested_date_format"],
        "mapping": preview["suggested_mapping"],
    }
    committed = await client.post(f"{IMPORTS}/{preview['import_id']}/commit", json=body)
    assert committed.status_code == 202, committed.text


# ── 验收 1: 缺数据的程序等数据, 不执行, 流程不进 completion ──


async def test_programs_short_of_data_wait_without_running() -> None:
    s = await short_of_data()

    current = statuses(s)
    assert {pid: v for pid, v in current.items() if v[0] is ProcedureStatus.awaiting_data} == {
        pid: (ProcedureStatus.awaiting_data, ids) for pid, ids in WAITING.items()
    }
    assert current["REV-03"] == (ProcedureStatus.planned, [])  # 前置 DOC-04 未完成, 不派发
    assert current["DOC-06"] == (ProcedureStatus.planned, [])  # 前置 CSH-04 未完成
    # 没有一条在空数据上执行: DOC-04 与 P2 程序都没有执行记录
    assert runs(s) == ["RUN-ACC-04-1", "RUN-PLN-01-1", "RUN-PLN-07-1"]
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("await_data",) and awaiting_inputs(snapshot)
    [payload] = waiting_payloads(snapshot)
    assert kind_of(payload) is WaitKind.inputs and sorted(payload["procedure_ids"]) == sorted(WAITING)
    assert review_waits(snapshot) == []
    overview = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).overview(EID)
    assert (overview.awaiting_review, overview.awaiting_data, overview.current_gate, overview.running) == (
        False,
        False,
        None,
        False,
    )
    assert overview.engagement.phase is Phase.P2_Executing  # 没进 completion
    assert {p.id: p.status for p in overview.procedures if p.status is ProcedureStatus.awaiting_data}.keys() == set(
        WAITING
    )
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    waits = {
        e.payload["id"]: e.payload["data_request_ids"]
        for e in trail.entries
        if e.kind is TrailKind.transition and e.payload.get("to") == "awaiting_data"
    }
    assert waits == WAITING


# ── 验收 2: A3 的补交请求 ──


async def test_requests_list_programs_waiting_for_each_item() -> None:
    s = await short_of_data()

    async with api_client(s.runner) as client:
        listed = (await client.get(f"/api/v1/engagements/{EID}/data/requests")).json()
    by_id = {r["id"]: r for r in listed}
    assert {rid: (r["kind"], r["reason_code"], r["used_for"], r["status"]) for rid, r in by_id.items()} == {
        "REQ-contract": ("document", "not_provided", ["DOC-04"], "open"),
        "REQ-sales_invoices": ("table", "missing_table", ["REV-02", "REV-08"], "open"),
        "REQ-bank_accounts": ("table", "missing_table", ["CSH-02", "CSH-04", "CSH-05"], "open"),
        "REQ-bank_transactions": ("table", "missing_table", ["CSH-02", "CSH-05", "REV-08"], "open"),
        "REQ-delivery_notes": ("table", "missing_table", ["REV-08"], "open"),
    }
    # 合同在 P1 等数据时就提出, 四张表在装配后提出, 同时提出的按表种类顺序; 不再要报表草稿 (Issue #46)
    assert [r["id"] for r in listed] == [
        "REQ-contract",
        "REQ-sales_invoices",
        "REQ-bank_accounts",
        "REQ-bank_transactions",
        "REQ-delivery_notes",
    ]
    assert by_id["REQ-bank_accounts"]["title"] == "银行账户" and by_id["REQ-contract"]["title"] == "合同"
    query = GraphStateQuery(s.runner.app, s.runner.deps, s.runner)
    found = await list_open_items(ToolEnv(EID, s.runner.deps, query, 6000), NoArgs())
    items = [i for i in found.data["items"] if i["type"] == "data_request"]
    assert [(i["request_id"], i["used_for"]) for i in items] == [(r["id"], r["used_for"]) for r in listed]


# ── 验收 3: 补了银行账户与流水, 自动接着跑 ──


async def test_bank_files_resume_cash_programs() -> None:
    s = await short_of_data()

    async with api_client(s.runner) as client:
        await import_csv(client, "bank_accounts.csv")
        await wait_idle(s.runner, EID)  # 导入提交后交给后台恢复, 由 await_data 重查
        after_accounts = statuses(s)
        mid_runs = runs(s)
        await import_csv(client, "bank_transactions.csv")
        await wait_idle(s.runner, EID)
        listed = {r["id"]: r["status"] for r in (await client.get(f"/api/v1/engagements/{EID}/data/requests")).json()}

    assert after_accounts["CSH-04"][0] is ProcedureStatus.completed
    assert after_accounts["DOC-06"][0] is ProcedureStatus.completed
    assert after_accounts["CSH-05"] == (ProcedureStatus.awaiting_data, ["REQ-bank_transactions"])
    assert {"RUN-CSH-04-1", "RUN-DOC-06-1"} <= set(mid_runs) and "RUN-CSH-05-1" not in mid_runs
    assert (listed["REQ-bank_accounts"], listed["REQ-bank_transactions"]) == ("received", "received")
    assert listed["REQ-sales_invoices"] == "open"
    current = statuses(s)
    assert current["CSH-05"][0] is ProcedureStatus.completed
    assert current["CSH-02"][0] is ProcedureStatus.awaiting_human  # 函证差异一律复核 (always_review)
    assert current["REV-08"] == (ProcedureStatus.awaiting_data, ["REQ-sales_invoices", "REQ-delivery_notes"])
    assert current["REV-02"] == (ProcedureStatus.awaiting_data, ["REQ-sales_invoices"])
    assert current["REV-03"] == (ProcedureStatus.planned, [])
    by_run = {r.id: r for r in s.output_store.rows if isinstance(r, ProcedureRun)}
    doc06 = max((r for r in by_run.values() if r.procedure_id == "DOC-06"), key=lambda r: r.version)
    assert doc06.ended_at is not None and by_run["RUN-CSH-02-1"].started_at >= doc06.ended_at
    # 例外与数据齐全的 Numbat 全流程 (R55, test_e2e_mock.assert_results) 中对应程序的相同
    exceptions = {x.id: (x.source_ids, x.amount_cents) for x in await s.output_store.latest(EID, AuditException)}
    assert exceptions == {
        "X-PLN-07-1-A01-1": (["1100"], 52500000),
        "X-CSH-05-1-B01-1": (["BA-01"], -5000000),
        "X-CSH-02-1-D01-1": (["BA-01"], -5000000),
    }
    assert "RUN-REV-08-1" not in by_run and "RUN-REV-03-1" not in by_run
    assert await s.next_nodes() == ("await_events",)  # 等 CSH-02 的复核; 之后仍有程序等数据, 不进 completion


# ── DOC-04 等到合同后补跑, REV-03 随之派发; 恢复与空恢复 ──


async def test_contracts_arriving_rerun_doc04_in_p1_ingest() -> None:
    s = await short_of_data()
    service = DataService(s.runner.deps, s.runner)

    async def receive_contracts() -> None:  # 相当于文件处理作业: 合同登记 (A3 分完类), 做完体检后作业结束
        s.financial.document_refs = [*s.financial.document_refs, *s.contracts]
        await service.checkup(EID)

    await service.launch(EID, ["contracts"], receive_contracts)
    for task in list(service.tasks):
        await task  # 作业结束时锁交给后台恢复
    await wait_idle(s.runner, EID)

    current = statuses(s)
    assert current["DOC-04"] == (ProcedureStatus.completed, [])
    assert {"RUN-DOC-04-1", "RUN-REV-03-1"} <= set(runs(s))
    assert current["REV-03"][0] is not ProcedureStatus.planned  # 已派发执行
    assert (await s.runner.app.aget_state(GraphRunner.config(EID))).values["phase"] is Phase.P2_Executing
    async with api_client(s.runner) as client:
        listed = {r["id"]: r["status"] for r in (await client.get(f"/api/v1/engagements/{EID}/data/requests")).json()}
    assert listed["REQ-contract"] == "received"


async def test_recover_rechecks_and_empty_resume_waits_again() -> None:
    """资料已到而交给后台恢复前中断: recover 以 INPUTS_CHANGED 继续; 没有变化的恢复停回 await_data, 不出新版本."""
    s = await short_of_data()
    before = statuses(s)
    versions = len(s.output_store.rows)

    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()  # 什么都没变
    await wait_idle(restarted, EID)
    assert await s.next_nodes() == ("await_data",) and restarted.run_error(EID) is None
    assert statuses(s) == before and len(s.output_store.rows) == versions

    s.data.table_stats[TableKind.bank_accounts] = TableStats(records=2)  # 写入成功而没来得及恢复
    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)
    assert statuses(s)["CSH-04"][0] is ProcedureStatus.completed and restarted.run_error(EID) is None


async def test_full_data_flow_writes_no_extra_versions() -> None:
    """验收 4 的补充: 资料齐全时 (缺省的假数据管理存储) 重查没有变化, 不出新版本, 没有 awaiting_data 的留痕."""
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagements)
    s = Started(runner, store, engagements)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)

    assert all(p.status is not ProcedureStatus.awaiting_data for p in await store.latest(EID, Procedure))
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assert not [e for e in trail.entries if e.payload.get("to") == "awaiting_data"]


# ── 契约, 写法校验与纯函数 ──


def test_inputs_are_declared_on_programs_that_wait() -> None:
    declared = {t.id: t.inputs for t in LIBRARY.all(include_disabled=True) if t.inputs.tables or t.inputs.documents}
    assert {pid: ([k.value for k in i.tables], [k.value for k in i.documents]) for pid, i in declared.items()} == {
        "CSH-02": (["bank_accounts", "bank_transactions"], []),
        "CSH-04": (["bank_accounts"], []),
        "CSH-05": (["bank_accounts", "bank_transactions"], []),
        "DOC-04": ([], ["contract"]),
        "REV-02": (["sales_invoices"], []),
        "REV-08": (["sales_invoices", "delivery_notes", "bank_transactions"], []),
    }
    assert input_problems(LIBRARY, ("DOC-04",)) == []


def library_with(tmp_path: Path, **extra: str) -> ProcedureLibrary:
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    base = "name: 测试\ncycle: Cash\nowner: A6\nengine: rule\nautonomy_level: L3\n"
    for name, text in extra.items():
        (root / "procedures" / f"{name}.yaml").write_text(f"id: {name}\n{base}{text}", encoding="utf-8")
    return ProcedureLibrary(root)


def test_inputs_problems_rejected_when_library_loads(tmp_path: Path) -> None:
    """只看模板就能判定的 inputs 写法问题在程序库加载时报出, 消息带程序编号 (审核 R272-07)."""
    with pytest.raises(ValueError) as wrong:
        library_with(
            tmp_path / "wrong",
            **{
                "X-REQ": "phase: P2_Executing\ninputs: {tables: [trial_balance, bank_accounts]}\n",
                "X-OTHER": "phase: P2_Executing\ninputs: {documents: [other]}\n",
            },
        )
    assert str(wrong.value) == (
        "程序模板的 inputs 不对: X-OTHER: inputs 不能写文件类型 other; "
        "X-REQ: inputs 不能写就绪必需的表 trial_balance (由 P1 等数据保证)"
    )
    # 取值须为 TableKind: 字段校验不过时也带程序编号与文件名
    with pytest.raises(ValueError, match=r"程序模板 X-BAD \(X-BAD\.yaml\) 写法不对: (?s:.*)inputs\.tables\.0"):
        library_with(tmp_path / "bad", **{"X-BAD": "phase: P2_Executing\ninputs: {tables: [ledger]}\n"})


def test_inputs_outside_p2_rejected_when_nodes_are_built(tmp_path: Path) -> None:
    """P2 以外的程序写 inputs 须能在执行阶段补跑 (p1_ingest), 要看图节点, 在 GraphNodes 构造时查."""
    library = library_with(tmp_path, **{"X-P3": "phase: P3_Completion\ninputs: {documents: [financial_statements]}\n"})
    assert input_problems(library, ("DOC-04",)) == [
        "X-P3: P2 以外的程序只有 DOC-04 能写 inputs (等到数据后才有地方执行)"
    ]
    with pytest.raises(ValueError, match="X-P3: P2 以外的程序只有 DOC-04 能写 inputs"):
        GraphNodes(make_deps(FakeRegistry(), FakeTrail(), library=library), 2)


def test_input_versions_follow_data_on_hand() -> None:
    pack = [
        Procedure(id="CSH-05", phase=Phase.P2_Executing, template_commit="x", autonomy_level="L4"),
        Procedure(id="DOC-06", phase=Phase.P2_Executing, template_commit="x", autonomy_level="L3"),
    ]
    nothing = OnHand()
    [waiting] = input_versions(pack, LIBRARY, nothing, ["CSH-05", "DOC-06"])  # DOC-06 没有 inputs, 不动
    assert (waiting.status, waiting.data_request_ids, waiting.version) == (
        ProcedureStatus.awaiting_data,
        ["REQ-bank_accounts", "REQ-bank_transactions"],
        2,
    )
    assert input_versions([waiting], LIBRARY, nothing, ["CSH-05"]) == []  # 不变不出版本
    half = OnHand(tables=frozenset({TableKind.bank_accounts}))
    [less] = input_versions([waiting], LIBRARY, half, ["CSH-05"])
    assert (less.status, less.data_request_ids) == (ProcedureStatus.awaiting_data, ["REQ-bank_transactions"])
    full = OnHand(tables=frozenset({TableKind.bank_accounts, TableKind.bank_transactions}))
    [ready] = input_versions([less], LIBRARY, full, ["CSH-05"])
    assert (ready.status, ready.data_request_ids, ready.version) == (ProcedureStatus.planned, [], 4)
    done = ready.model_copy(update={"status": ProcedureStatus.completed})
    assert input_versions([done], LIBRARY, nothing, ["CSH-05"]) == []  # 已执行完的不再按资料改


def test_every_document_type_but_other_has_a_request_title() -> None:
    """模板 inputs 可写 other 以外的任一种文件类型, 提请求时都要有标题."""
    from backend.services.data_quality import DOCUMENT_TITLES

    assert set(DOCUMENT_TITLES) == set(DocumentType) - {DocumentType.other}


def test_program_requests_and_merge() -> None:
    needs = {
        "REV-08": ProcedureInputs(tables=[TableKind.sales_invoices, TableKind.delivery_notes]),
        "REV-02": ProcedureInputs(tables=[TableKind.sales_invoices]),
        "DOC-04": ProcedureInputs(documents=[DocumentType.contract]),
    }
    have = OnHand(tables=frozenset({TableKind.delivery_notes}))
    assert missing_inputs(needs["REV-08"], have) == [TableKind.sales_invoices]
    requests = program_requests(needs, have, NOW)
    assert [(r.id, r.kind, r.reason_code, r.used_for, r.title) for r in requests] == [
        ("REQ-sales_invoices", "table", "missing_table", ["REV-02", "REV-08"], "销售发票"),
        ("REQ-contract", "document", "not_provided", ["DOC-04"], "合同"),
    ]
    approval = table_request(TableKind.approval_log, "field_unavailable", {}, NOW)
    assert approval.used_for == ["GEN-05"]  # 体检请求的用途不变 (USED_FOR)
    extra = table_request(TableKind.approval_log, "missing_table", {}, NOW, ["X-USE"])
    [merged] = merge_requests([approval], [extra])
    assert (merged.reason_code, merged.used_for) == ("field_unavailable", ["GEN-05", "X-USE"])
    # 体检请求的相对顺序不变: 科目表, 试算表, 总账, 审批日志
    old = ["REQ-accounts", "REQ-trial_balance", "REQ-journal_entries", "REQ-approval_log"]
    assert sorted(old, key=REQUEST_ORDER.index) == old


def test_inputs_wait_takes_no_decisions_and_old_procedures_load() -> None:
    payload = inputs_payload(["CSH-04"])
    assert kind_of(payload) is WaitKind.inputs
    procedure = Procedure(id="CSH-04", phase=Phase.P2_Executing, template_commit="x", autonomy_level="L4")
    task = ReviewTask(
        id="T-1",
        kind=TaskKind.normal,
        procedure_id="CSH-04",
        target_type=TargetType.ProcedureRun,
        target_id="RUN-CSH-04-1",
        target_version=1,
        run_id="RUN-CSH-04-1",
        created_at=NOW,
    )
    assert not accepts(payload, task, [procedure])
    # 旧检查点与 Store 里没有 data_request_ids 的程序照常读回; 新状态经检查点往返不变
    old = Procedure.model_validate(
        {"id": "CSH-04", "phase": "P2_Executing", "template_commit": "x", "autonomy_level": "L4", "version": 2}
    )
    assert old.data_request_ids == []
    serde = checkpoint_serde()
    waiting = old.model_copy(update={"status": ProcedureStatus.awaiting_data, "data_request_ids": ["REQ-x"]})
    assert serde.loads_typed(serde.dumps_typed(waiting)) == waiting
    assert serde.loads_typed(serde.dumps_typed(ProcedureStatus.no_data)) is ProcedureStatus.no_data


# ── 自动继续与交锁 (审核 R272-01, R272-03): 等复核时补数据, 只剩 DOC-04 在等, 交锁, 忙, 失败与取消 ──


async def test_data_arriving_during_review_runs_after_decision() -> None:
    """图停在等复核 (CSH-02) 时补了销售发票与发运单 (不在 inputs 等待, 不触发恢复): 复核决定后经 fan_in 到
    await_data, 先重查就放行, REV-08 随即执行, 不停在 await_data 等下一次写入 (await_data 先停再查时失败)."""
    s = await short_of_data()
    async with api_client(s.runner) as client:
        await import_csv(client, "bank_accounts.csv")
        await wait_idle(s.runner, EID)
        await import_csv(client, "bank_transactions.csv")
        await wait_idle(s.runner, EID)
        assert await s.next_nodes() == ("await_events",)
        await import_csv(client, "sales_invoices.csv")
        await import_csv(client, "delivery_notes.csv")
        assert await s.next_nodes() == ("await_events",)  # 不在 inputs 等待, 没有恢复
    assert statuses(s)["REV-08"][0] is ProcedureStatus.awaiting_data  # 资料到了, 还没重查
    [task] = [
        r
        for r in s.output_store.rows
        if isinstance(r, ReviewTask) and r.procedure_id == "CSH-02" and r.status is TaskStatus.pending
    ]
    await s.submit.submit(EID, record(task.id, id="D-CSH02", target_version=task.target_version))
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None, s.runner.run_error(EID)
    current = statuses(s)
    assert current["REV-08"][0] is ProcedureStatus.completed, current["REV-08"]
    assert current["REV-02"] == (ProcedureStatus.planned, [])  # 资料到了, 仍等前置 REV-03 (DOC-04 等合同)
    assert current["DOC-04"] == (ProcedureStatus.awaiting_data, ["REQ-contract"])
    assert await s.next_nodes() == ("await_data",)


def pack_state(nodes: GraphNodes, status_of: dict[str, ProcedureStatus]) -> EngagementState:
    """执行阶段的状态: 固定集与 Numbat 的七条 P2 程序, 状态取 status_of, 没写的为 completed."""
    library = nodes.deps.library
    pack = [
        p.model_copy(update={"status": status_of.get(p.id, ProcedureStatus.completed)})
        for p in fixed_procedures(library)
    ]
    for pid in ("CSH-04", "DOC-06", "CSH-05", "CSH-02", "REV-08", "REV-03", "REV-02"):
        template = library.load(pid)
        status = status_of.get(pid, ProcedureStatus.completed)
        pack.append(
            Procedure(
                id=pid, phase=template.phase, template_commit="x", autonomy_level=template.autonomy_level, status=status
            )
        )
    state = fill_defaults(initial_state(ENGAGEMENT, pack, 1))
    state["phase"] = Phase.P2_Executing
    return state


def test_after_fan_in_waits_when_only_doc04_waits() -> None:
    """只有 P2 以外的 DOC-04 在等合同 (REV-03 因它 planned, 其余完成): 去 await_data, 不报 ScheduleStuck
    (客户给齐了账表却没给合同; 等数据只看 P2 时失败)."""
    nodes = make_graph_nodes(make_deps(FakeRegistry(), FakeTrail()))
    state = pack_state(nodes, {"DOC-04": ProcedureStatus.awaiting_data, "REV-03": ProcedureStatus.planned})
    assert after_fan_in(nodes)(state) == "await_data"


async def test_job_end_hands_lock_to_graph() -> None:
    """后台作业结束时图在等数据: 锁交给图的后台推进, 作业的完成回调不再释放, 推进期间再写数据报 ENGAGEMENT_BUSY;
    推进停下后锁释放 (完成回调照样释放时失败)."""
    s = await short_of_data()
    service = DataService(s.runner.deps, s.runner)

    async def job() -> None:
        s.data.table_stats[TableKind.bank_accounts] = TableStats(records=2)

    await service.launch(EID, ["bank_accounts"], job)
    [task] = list(service.tasks)
    await task
    await asyncio.sleep(0)  # 让作业的完成回调跑完
    assert EID in s.runner.tasks  # 图在后台推进
    assert s.runner.is_running(EID)
    with pytest.raises(DecisionRejected, match="ENGAGEMENT_BUSY"):
        async with service.holding(EID, ()):
            pass
    await wait_idle(s.runner, EID)
    assert not s.runner.is_running(EID)
    assert statuses(s)["CSH-04"][0] is ProcedureStatus.completed


async def test_holding_hands_lock_to_graph_and_busy_meanwhile() -> None:
    """数据写操作 (holding) 结束时图在等数据: 锁在后台推进手里, 这期间再写报 ENGAGEMENT_BUSY; 停下后释放."""
    s = await short_of_data()
    service = DataService(s.runner.deps, s.runner)
    async with service.holding(EID, {"bank_accounts"}):
        s.data.table_stats[TableKind.bank_accounts] = TableStats(records=2)
    assert s.runner.is_running(EID) and EID in s.runner.tasks
    with pytest.raises(DecisionRejected, match="ENGAGEMENT_BUSY"):
        async with service.holding(EID, ()):
            pass
    await wait_idle(s.runner, EID)
    assert not s.runner.is_running(EID)
    assert statuses(s)["CSH-04"][0] is ProcedureStatus.completed


async def test_job_failure_still_hands_lock_to_graph() -> None:
    """后台作业抛异常 (写入了一部分数据) 也算结束: 图在等数据时交锁恢复, 重查."""
    s = await short_of_data()
    service = DataService(s.runner.deps, s.runner)

    async def job() -> None:
        s.data.table_stats[TableKind.bank_accounts] = TableStats(records=2)
        raise RuntimeError("同步中途失败")

    await service.launch(EID, ["bank_accounts"], job)
    for task in list(service.tasks):
        await task
    await wait_idle(s.runner, EID)
    assert statuses(s)["CSH-04"][0] is ProcedureStatus.completed
    assert not s.runner.is_running(EID) and s.runner.run_error(EID) is None


async def test_checkpoint_read_failure_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据写入后读检查点出错: 只记日志并释放锁 (不当作已交出), 数据已提交, 图仍停在 await_data; 之后能再写,
    这次交锁恢复 (读检查点失败仍当已交出时锁泄漏, 失败)."""
    s = await short_of_data()
    service = DataService(s.runner.deps, s.runner)
    real = s.runner.app.aget_state
    broken = {"once": True}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        if broken["once"]:
            broken["once"] = False
            raise RuntimeError("checkpoint down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(s.runner.app, "aget_state", flaky)
    async with service.holding(EID, {"bank_accounts"}):
        s.data.table_stats[TableKind.bank_accounts] = TableStats(records=2)
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == ("await_data",)
    async with service.holding(EID, ()):  # 锁已释放, 能再写
        pass
    await wait_idle(s.runner, EID)
    assert statuses(s)["CSH-04"][0] is ProcedureStatus.completed


async def test_cancel_while_handing_over_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """holding 成功后在交锁前读检查点时被取消 (请求取消, 应用退出): 锁释放, 不会一直 ENGAGEMENT_BUSY; 之后能再写."""
    s = await short_of_data()
    service = DataService(s.runner.deps, s.runner)
    entered = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    async def write() -> None:
        async with service.holding(EID, {"bank_accounts"}):
            s.data.table_stats[TableKind.bank_accounts] = TableStats(records=2)

    monkeypatch.setattr(s.runner.app, "aget_state", hang)
    task = asyncio.create_task(write())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    assert not s.runner.is_running(EID) and service.processing(EID) == []
    assert await s.next_nodes() == ("await_data",)
    async with service.holding(EID, ()):
        pass
    await wait_idle(s.runner, EID)
    assert statuses(s)["CSH-04"][0] is ProcedureStatus.completed


# ── 启动顺序 (审核 R272-04): 先补做没处理完的上传文件, 做完再带动图 ──


async def test_startup_processes_files_before_resuming_waiting_graph() -> None:
    """停在 await_data 等合同的项目有一份没处理完的上传文件: 启动恢复 (main.recover) 先由 DataService.recover 补做
    (不因图被拉起而跳过), 做完锁交给 runner.proceed, 以 INPUTS_CHANGED 恢复一次; runner.recover 不再拉起这个项目."""
    s = await short_of_data()
    upload = s.contracts[0].model_copy(
        update={
            "id": "DOC-UPLOAD-1",
            "type": DocumentType.other,
            "status": "received",
            "channel": "auditor_upload",
            "uploaded_by": "U-1",
        }
    )
    s.financial.document_refs = [*s.financial.document_refs, upload]
    restarted = GraphRunner(s.runner.app, s.runner.deps)
    launched: list[tuple[object, str]] = []  # (恢复值, 当时上传文件的状态)
    launch = restarted.launch

    def spy(eid: str, graph_input: Any) -> None:
        status = next(d.status for d in s.financial.document_refs if d.id == upload.id)
        launched.append((graph_input.resume if isinstance(graph_input, Command) else graph_input, status))
        launch(eid, graph_input)

    restarted.launch = spy  # type: ignore[method-assign]
    service = DataService(s.runner.deps, restarted)
    await recover(restarted, service)
    assert service.processing(EID) == [frozenset({upload.id})]  # 补做的作业持锁
    assert restarted.run_error(EID) is None  # runner.recover 跳过了它, 没去抢锁 (抢不到会记 ENGAGEMENT_BUSY)
    for task in list(service.tasks):
        await task
    await wait_idle(restarted, EID)

    handled = next(d for d in s.financial.document_refs if d.id == upload.id)
    assert handled.status != "received"  # 补做了 (这里的大模型分不出类型, 转人工)
    assert launched == [(INPUTS_CHANGED, handled.status)]  # 文件处理完才恢复, 只拉起一次
    assert restarted.run_error(EID) is None and not restarted.is_running(EID)
    assert await s.next_nodes() == ("await_data",)


# ── 等人时资料变了 (审核 R272-05): rerun 也先查资料; 节点重放照当时的判断, 不撞 Store 已有的版本 ──


def flaky_doc04(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOC-04 第一次执行失败 (之后照常)."""
    from backend.agents import ingest

    calls = {"n": 0}
    original = ingest.IngestAgent.extract_contracts

    async def flaky(self: Any, ctx: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuleError("DOC-04: 模拟失败")
        return await original(self, ctx)

    monkeypatch.setattr(ingest.IngestAgent, "extract_contracts", flaky)


async def rerun_failed_doc04(s: Started) -> None:
    [task] = [
        r
        for r in s.output_store.rows
        if isinstance(r, ReviewTask)
        and r.procedure_id == "DOC-04"
        and r.status is TaskStatus.pending
        and r.kind is TaskKind.failed
    ]
    await s.submit.submit(EID, record(task.id, Decision.rerun, id="D-RERUN", target_version=task.target_version))
    await wait_idle(s.runner, EID)


async def doc04_consistent(s: Started) -> Procedure:
    """DOC-04 在检查点与 Store 中是同一版本, 返回它."""
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    [in_state] = [p for p in fill_defaults(snapshot.values)["procedure_pack"] if p.id == "DOC-04"]
    stored = await s.output_store.get(EID, Procedure, "DOC-04")
    assert stored == in_state, (stored, in_state)
    return in_state


async def contracts_arrive(s: Short) -> None:
    """后台作业登记合同 (A3 分完类) 并体检后结束, 图在等数据时交锁恢复."""
    service = DataService(s.runner.deps, s.runner)

    async def receive_contracts() -> None:
        s.financial.document_refs = [*s.financial.document_refs, *s.contracts]
        await service.checkup(EID)

    await service.launch(EID, ["contracts"], receive_contracts)
    for task in list(service.tasks):
        await task
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None, s.runner.run_error(EID)


async def request_status(s: Started, request_id: str) -> str:
    return {r.id: r.status for r in await s.runner.deps.data.requests(EID)}[request_id]


async def assert_doc04_waits_after_rerun(s: Started) -> None:
    """rerun 时合同已没了: 不在空数据上重跑 (没有第 2 次执行), DOC-04 转等合同, 检查点与 Store 版本一致,
    合同请求重新打开, REV-03 不派发."""
    assert s.runner.run_error(EID) is None, s.runner.run_error(EID)
    doc04 = await doc04_consistent(s)
    assert (doc04.status, doc04.data_request_ids) == (ProcedureStatus.awaiting_data, ["REQ-contract"])
    assert "RUN-DOC-04-1" in runs(s) and "RUN-DOC-04-2" not in runs(s)
    assert statuses(s).get("REV-03", (ProcedureStatus.planned, [])) == (ProcedureStatus.planned, [])  # P1 时还没装配
    assert await request_status(s, "REQ-contract") == "open"
    rerun = await s.output_store.get(EID, ReviewRecord, "D-RERUN")
    assert rerun is not None and rerun.decision is Decision.rerun


async def assert_doc04_reruns_when_contracts_back(s: Short) -> None:
    """补回合同后自动继续: p1_ingest 补跑 DOC-04 (第 2 次执行), REV-03 随之派发, 合同请求收到."""
    await contracts_arrive(s)
    doc04 = await doc04_consistent(s)
    assert doc04.status is ProcedureStatus.completed
    assert "RUN-DOC-04-2" in runs(s)
    assert statuses(s)["REV-03"][0] is not ProcedureStatus.planned  # 已派发
    assert await request_status(s, "REQ-contract") == "received"


async def test_contracts_gone_while_late_doc04_waits_then_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    """合同到了, p1_ingest 补跑 DOC-04 失败, 在节点内等人 (等待取到的不是 INPUTS_CHANGED); 这时合同又没了 (如改了
    类型), 再提交 rerun: 重放照当时的判断取回第 1 次执行 (不按现在的资料另出版本, 不撞 Store 已有的版本), 读回 rerun
    后重跑前再查资料, 合同没了就转等合同; 补回合同后自动继续."""
    flaky_doc04(monkeypatch)
    s = await short_of_data()
    await contracts_arrive(s)
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    [payload] = waiting_payloads(snapshot)
    assert (kind_of(payload), payload["procedure_id"]) == (WaitKind.human, "DOC-04")
    assert [t.name for t in snapshot.tasks] == ["p1_ingest"]

    s.financial.document_refs = [d for d in s.financial.document_refs if d not in s.contracts]  # 合同没了
    await rerun_failed_doc04(s)
    await assert_doc04_waits_after_rerun(s)
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    [payload] = waiting_payloads(snapshot)
    assert kind_of(payload) is WaitKind.inputs and "DOC-04" in payload["procedure_ids"]

    await assert_doc04_reruns_when_contracts_back(s)


async def test_contracts_gone_while_p1_doc04_waits_then_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 第一次执行 DOC-04 失败, 等人期间合同没了, 再提交 rerun: 同上, DOC-04 转等合同, P1 照常到 PLN-01/03 闸门;
    批准后进执行阶段, 补回合同后自动补跑."""
    flaky_doc04(monkeypatch)
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    data = FakeDataStore(stats=REQUIRED_ONLY)
    runner, store = make_runner(engagement_store=engagements, agents=real_agents, financial_store=financial, data=data)
    s = Short(runner, store, engagements)
    s.data, s.financial = data, financial
    s.contracts = [d for d in financial.document_refs if d.type is DocumentType.contract]
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("p1_ingest",)  # DOC-04 失败, 节点内等人

    financial.document_refs = [d for d in financial.document_refs if d.type is not DocumentType.contract]
    await rerun_failed_doc04(s)
    await assert_doc04_waits_after_rerun(s)
    assert await s.next_nodes() == ("gate_p1_risk",)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("await_data",)
    await assert_doc04_reruns_when_contracts_back(s)


def test_input_versions_check_reruns_and_keep_first_pass_decision_on_replay() -> None:
    """要求重跑的 (executing) 同样按资料查: 缺了转等数据, 不缺不变. replayed 中的程序照当时的判断 (资料齐了),
    不按现在的资料重查."""
    pack = [Procedure(id="DOC-04", phase=Phase.P1_Planning, template_commit="x", autonomy_level="L3")]
    assert input_versions(pack, LIBRARY, OnHand(), ["DOC-04"]) != []  # 不重放: 没有合同, 等数据
    assert input_versions(pack, LIBRARY, OnHand(), ["DOC-04"], replayed=["DOC-04"]) == []
    waiting = pack[0].model_copy(update={"status": ProcedureStatus.awaiting_data, "data_request_ids": ["REQ-contract"]})
    [planned] = input_versions([waiting], LIBRARY, OnHand(), ["DOC-04"], replayed=["DOC-04"])
    assert (planned.status, planned.data_request_ids, planned.version) == (ProcedureStatus.planned, [], 2)
    rerun = pack[0].model_copy(update={"status": ProcedureStatus.executing})
    [short] = input_versions([rerun], LIBRARY, OnHand(), ["DOC-04"])
    assert (short.status, short.data_request_ids) == (ProcedureStatus.awaiting_data, ["REQ-contract"])
    contracts = OnHand(documents=frozenset({DocumentType.contract}))
    assert input_versions([rerun], LIBRARY, contracts, ["DOC-04"]) == []  # 资料齐了, 照常重跑
    assert input_versions([rerun], LIBRARY, OnHand(), ["DOC-04"], replayed=["DOC-04"]) == []


# ── 以 Store 为准 (审核 R272-08) 与启动带动图出错 (R272-09) ──


async def before_pln() -> Short:
    """同 short_of_data, 停在 PLN-01/03 闸门 (还没批准)."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    data = FakeDataStore(stats=dict(REQUIRED_ONLY))
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagements, agents=real_agents, financial_store=financial, data=data)
    s = Short(runner, store, engagements)
    s.data, s.financial = data, financial
    s.contracts = [d for d in financial.document_refs if d.type is DocumentType.contract]
    financial.document_refs = [d for d in financial.document_refs if d.type is not DocumentType.contract]
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit_gate(s, acc01_decision(GateVerdict.approve, id="D-ACC"), ("gate_p1_risk",))
    return s


async def test_schedule_replay_takes_store_versions_after_node_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLN-01/03 通过后 schedule 把 CSH-04 记 v2 awaiting_data (已提交), 随后写留痕失败, 节点出错 (锁释放). 审计师这时
    导入了银行账户, 再 resume: schedule 重做时以 Store 为准, 取已写的 v2 照当时的判断, 不按现在的资料重算 (否则
    CSH-04 按 v1 派发, fan_in 的 v2 撞上 Store 已有的等数据版本, 之后又以 rerun 跑第 2 次); 补写那条留痕; 之后
    await_data 重查, CSH-04 只执行一次, 检查点与 Store 版本一致."""
    from backend.orchestration import nodes as nodes_module

    s = await before_pln()
    real = nodes_module.record_transitions
    fired = {"done": False}

    async def flaky(trail: Any, entries: Any) -> Any:
        if not fired["done"] and any(
            e.payload.get("id") == "CSH-04" and e.payload.get("to") == "awaiting_data" for e in entries
        ):
            fired["done"] = True
            raise RuntimeError("trail down")
        return await real(trail, entries)

    monkeypatch.setattr(nodes_module, "record_transitions", flaky)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)
    assert fired["done"] and s.runner.run_error(EID) == "RuntimeError: trail down"
    assert statuses(s)["CSH-04"] == (ProcedureStatus.awaiting_data, ["REQ-bank_accounts"])  # 已提交

    service = DataService(s.runner.deps, s.runner)
    async with service.holding(EID, {"bank_accounts"}):  # 节点出错停着, 锁空闲, 能写; 不在等数据, 不恢复
        s.data.table_stats[TableKind.bank_accounts] = TableStats(records=2)
    await wait_idle(s.runner, EID)
    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None, s.runner.run_error(EID)
    assert [r for r in runs(s) if r.startswith("RUN-CSH-04")] == ["RUN-CSH-04-1"]
    run = await s.output_store.get(EID, ProcedureRun, "RUN-CSH-04-1")
    assert run is not None and run.trigger.value == "initial"
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    [in_state] = [p for p in fill_defaults(snapshot.values)["procedure_pack"] if p.id == "CSH-04"]
    assert await s.output_store.get(EID, Procedure, "CSH-04") == in_state
    assert in_state.status is ProcedureStatus.completed
    history = sorted(
        (p.version, p.status) for p in s.output_store.rows if isinstance(p, Procedure) and p.id == "CSH-04"
    )
    assert history == [
        (1, ProcedureStatus.planned),
        (2, ProcedureStatus.awaiting_data),
        (3, ProcedureStatus.planned),
        (4, ProcedureStatus.completed),
    ]
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    moves = [(e.payload["from"], e.payload["to"]) for e in trail.entries if e.payload.get("id") == "CSH-04"]
    assert moves == [("planned", "awaiting_data"), ("awaiting_data", "planned"), ("planned", "completed")]


async def test_startup_proceed_failure_records_run_error_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动补做的作业做完, 锁交给 runner.proceed; 它读检查点出错时记 run_error, 锁释放, 作业任务没有未取回的异常;
    之后手动重试 (resume) 照常."""
    s = await short_of_data()
    upload = s.contracts[0].model_copy(
        update={
            "id": "DOC-UPLOAD-1",
            "type": DocumentType.other,
            "status": "received",
            "channel": "auditor_upload",
            "uploaded_by": "U-1",
        }
    )
    s.financial.document_refs = [*s.financial.document_refs, upload]
    restarted = GraphRunner(s.runner.app, s.runner.deps)
    service = DataService(s.runner.deps, restarted)
    real = restarted.app.aget_state
    calls = {"n": 0}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("checkpoint down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(restarted.app, "aget_state", flaky)
    assert await service.recover() == {EID}
    tasks = list(service.tasks)
    for task in tasks:
        await task
    assert all(t.exception() is None for t in tasks)
    assert restarted.run_error(EID) == "RuntimeError: checkpoint down"
    assert not restarted.is_running(EID)
    monkeypatch.undo()
    await restarted.resume(EID)
    await wait_idle(restarted, EID)
    assert restarted.run_error(EID) is None and await s.next_nodes() == ("await_data",)


# ── 真实存储 (审核 R272-02): 验收 1, 3 的主干与新字段经 Postgres Store 与检查点往返 ──


async def commit_demo_file(service: DataService, eid: str, name: str, actor: str) -> None:
    """经 DataService 导入 data/demo 的原文件并按系统建议提交."""
    preview = await service.start_import(eid, name, (DEMO / name).read_bytes(), actor=actor)
    assert preview.suggested_kind is not None, name
    body = ImportCommit(
        kind=preview.suggested_kind,
        sheet=preview.sheet,
        date_format=preview.suggested_date_format,
        mapping=preview.suggested_mapping,
    )
    await service.commit_import(eid, preview.import_id, body, actor)


@pytest.mark.slow
async def test_short_of_data_on_postgres(tmp_path: Path) -> None:
    """新项目不载入数据集, 经导入接入三张必需表并确认已齐: DOC-04 等合同, PLN-01/03 通过后五条 P2 程序等数据,
    图停在 await_data, 检查点 (AuditPostgresSaver) 与 Store 读回 awaiting_data 与 data_request_ids; 再导入银行两表,
    图自动继续, 例外与 R55 相同, 请求转 received."""
    async with real_storage(tmp_path, load_dataset=False) as run:
        s, deps, eid, owner = run.s, run.deps, run.eid, run.owner
        runner = s.runner
        service = DataService(deps, runner)
        try:
            await runner.start(eid)
            await wait_idle(runner, eid)
            await s.submit.submit(eid, acc01_decision(GateVerdict.approve, id="D-ACC", decided_by=owner))
            await wait_idle(runner, eid)
            assert (await runner.app.aget_state(GraphRunner.config(eid))).next == ("p1_await_data",)
            for name in ("accounts.csv", "trial_balance.csv", "journal_entries.csv"):
                await commit_demo_file(service, eid, name, owner)
            await service.confirm_ready(eid, owner, reason="其余资料随后补交")
            await wait_idle(runner, eid)
            assert runner.run_error(eid) is None, runner.run_error(eid)
            assert (await runner.app.aget_state(GraphRunner.config(eid))).next == ("gate_p1_risk",)
            await s.submit.submit(eid, pln_decision(GateVerdict.approve, decided_by=owner))
            await wait_idle(runner, eid)
            assert runner.run_error(eid) is None, runner.run_error(eid)

            latest = {p.id: p for p in await deps.output_store.latest(eid, Procedure)}
            waiting = {
                pid: p.data_request_ids for pid, p in latest.items() if p.status is ProcedureStatus.awaiting_data
            }
            assert waiting == WAITING
            assert sorted(r.id for r in await deps.output_store.latest(eid, ProcedureRun)) == [
                "RUN-ACC-04-1",
                "RUN-PLN-01-1",
                "RUN-PLN-07-1",
            ]
            snapshot = await runner.app.aget_state(GraphRunner.config(eid))
            assert snapshot.next == ("await_data",) and awaiting_inputs(snapshot)
            pack = {p.id: p for p in fill_defaults(snapshot.values)["procedure_pack"]}
            assert {
                pid: p.data_request_ids for pid, p in pack.items() if p.status is ProcedureStatus.awaiting_data
            } == WAITING
            [payload] = waiting_payloads(snapshot)
            assert kind_of(payload) is WaitKind.inputs and sorted(payload["procedure_ids"]) == sorted(WAITING)
            requests = {r.id: (r.status, r.used_for) for r in await deps.data.requests(eid)}
            assert requests["REQ-bank_accounts"] == ("open", ["CSH-02", "CSH-04", "CSH-05"])
            assert requests["REQ-contract"] == ("open", ["DOC-04"])

            await commit_demo_file(service, eid, "bank_accounts.csv", owner)
            await wait_idle(runner, eid)
            assert runner.run_error(eid) is None, runner.run_error(eid)
            mid = {p.id: p.status for p in await deps.output_store.latest(eid, Procedure)}
            assert (mid["CSH-04"], mid["DOC-06"], mid["CSH-05"]) == (
                ProcedureStatus.completed,
                ProcedureStatus.completed,
                ProcedureStatus.awaiting_data,
            )

            await commit_demo_file(service, eid, "bank_transactions.csv", owner)
            await wait_idle(runner, eid)
            assert runner.run_error(eid) is None, runner.run_error(eid)
            final = {p.id: p for p in await deps.output_store.latest(eid, Procedure)}
            assert final["CSH-05"].status is ProcedureStatus.completed
            assert final["CSH-02"].status is ProcedureStatus.awaiting_human
            assert final["REV-08"].data_request_ids == ["REQ-sales_invoices", "REQ-delivery_notes"]
            exceptions = {
                x.id: (x.source_ids, x.amount_cents) for x in await deps.output_store.latest(eid, AuditException)
            }
            assert exceptions == {
                "X-PLN-07-1-A01-1": (["1100"], 52500000),
                "X-CSH-05-1-B01-1": (["BA-01"], -5000000),
                "X-CSH-02-1-D01-1": (["BA-01"], -5000000),
            }
            requests = {r.id: r.status for r in await deps.data.requests(eid)}
            assert (requests["REQ-bank_accounts"], requests["REQ-bank_transactions"]) == ("received", "received")
            assert (await runner.app.aget_state(GraphRunner.config(eid))).next == ("await_events",)
        finally:
            await service.shutdown()
