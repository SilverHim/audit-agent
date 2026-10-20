"""需求 R280: 补交请求标 "客户无此类资料" (理由必填), 等这项资料的程序与其下游记未执行 (无数据), 留痕带理由,
FIN-01 材料列出; 标记是定论, 只剩这些程序要的其他请求撤回. 存储用假实现 (连库的见 test_data_store_postgres)."""

from datetime import UTC, datetime

import pytest

from backend.contracts.data import DataRequest, TableKind, TableStats
from backend.contracts.errors import DataRejected
from backend.contracts.knowledge import ProcedureInputs
from backend.contracts.models import (
    DocumentType,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import fill_defaults, initial_state
from backend.orchestration.inputs import input_versions
from backend.services.data_quality import OnHand, program_requests, reconcile, table_request
from backend.services.data_service import DataService
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_TRIAL_BALANCE,
    SEEDED_STATS,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    api_client,
    make_deps,
    make_graph_nodes,
    make_runner,
    wait_idle,
)
from tests.test_data_ready import waiting_for_data
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_e2e_mock import real_agents
from tests.test_flow import REVIEWS, submit_gate
from tests.test_p2_agents_demo import replies
from tests.test_procedure_inputs import LIBRARY, REQUIRED_ONLY, runs, short_of_data, statuses

EID = ENGAGEMENT.id
NOW = datetime(2026, 10, 18, tzinfo=UTC)
REQUESTS = f"/api/v1/engagements/{EID}/data/requests"
REASON = "客户本年没有保留发运单, 签收靠电话确认"


def unavailable_url(request_id: str) -> str:
    return f"{REQUESTS}/{request_id}/unavailable"


async def without_delivery_notes() -> Started:
    """数据齐全的 Numbat 项目, 只是没有发运单: 启动 → ACC-01 → PLN-01/03 → 复核 REV-03 三条与 CSH-02 (REV-02
    不派发, 没有它的任务), 停在 await_data 等发运单."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    stats = {kind: item for kind, item in SEEDED_STATS.items() if kind is not TableKind.delivery_notes}
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagements, agents=real_agents, financial_store=financial, data=FakeDataStore(stats=stats)
    )
    s = Started(runner, store, engagements)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit_gate(s, acc01_decision(GateVerdict.approve, id="D-ACC"), ("gate_p1_risk",))
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    for decision in (d for d in REVIEWS if not d.task_id.startswith("T-J-REV-02")):
        assert await s.next_nodes() == ("await_events",), (decision.task_id, runner.run_error(EID))
        await s.submit.submit(EID, decision)
        await wait_idle(runner, EID)
    return s


# ── 验收 5 ──


async def test_no_delivery_notes_marks_rev08_not_executed_and_lists_it_at_fin01() -> None:
    s = await without_delivery_notes()
    assert await s.next_nodes() == ("await_data",)
    assert statuses(s)["REV-08"] == (ProcedureStatus.awaiting_data, ["REQ-delivery_notes"])
    assert statuses(s)["REV-02"] == (ProcedureStatus.planned, [])  # 前置 REV-08 未完成

    async with api_client(s.runner) as client:
        blank = await client.post(unavailable_url("REQ-delivery_notes"), json={"reason": "  "})
        marked = await client.post(unavailable_url("REQ-delivery_notes"), json={"reason": f" {REASON} "})
        await wait_idle(s.runner, EID)  # 标记结束时交给后台恢复, await_data 重查
        again = await client.post(unavailable_url("REQ-delivery_notes"), json={"reason": REASON})
        gate = (await client.get(f"/api/v1/engagements/{EID}/gate")).json()

    assert (blank.status_code, blank.json()["detail"]["code"]) == (422, "MISSING_FIELD")
    assert marked.status_code == 200
    body = marked.json()
    assert (body["status"], body["unavailable_reason"], body["resolved_by"]) == ("unavailable", REASON, "U-1")
    assert (again.status_code, again.json()["detail"]["code"]) == (409, "REQUEST_NOT_OPEN")
    current = statuses(s)
    assert current["REV-08"] == (ProcedureStatus.no_data, ["REQ-delivery_notes"])
    assert current["REV-02"] == (ProcedureStatus.no_data, ["REQ-delivery_notes"])  # 前置未执行, 理由沿用
    runs = {r.id for r in s.output_store.rows if isinstance(r, ProcedureRun)}
    assert not {r for r in runs if r.startswith(("RUN-REV-08", "RUN-REV-02"))}
    assert await s.next_nodes() == ("gate_fin",)  # 没有程序再等数据, 进完成阶段
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [entry] = [e for e in trail.entries if e.payload.get("action") == "request_unavailable"]
    assert (entry.actor, entry.kind, entry.summary) == ("U-1", TrailKind.data, "客户无此类资料: 发运单")
    assert entry.payload == {
        "action": "request_unavailable",
        "request_id": "REQ-delivery_notes",
        "table_kind": "delivery_notes",
        "reason": REASON,
        "used_for": ["REV-08"],
    }
    to_no_data = {
        e.payload["id"]: (e.payload["from"], e.payload["data_request_ids"])
        for e in trail.entries
        if e.kind is TrailKind.transition and e.payload.get("to") == "no_data"
    }
    assert to_no_data == {
        "REV-08": ("awaiting_data", ["REQ-delivery_notes"]),
        "REV-02": ("planned", ["REQ-delivery_notes"]),
    }
    material = gate["material"]
    assert material["kind"] == "FIN-01"
    assert [(n["procedure_id"], n["via"], [r["id"] for r in n["requests"]]) for n in material["not_executed"]] == [
        ("REV-02", ["REV-08"], ["REQ-delivery_notes"]),
        ("REV-08", [], ["REQ-delivery_notes"]),
    ]
    assert material["not_executed"][1]["requests"][0]["unavailable_reason"] == REASON


async def test_marking_while_waiting_resumes_and_withdraws_unneeded_requests() -> None:
    """只有三张必需表时标发运单: 自动恢复, REV-08 与经前置的 REV-02 记未执行; 销售发票只有它俩要, 请求撤回;
    银行流水 CSH-05, CSH-02 还要, 仍待补交 (用途去掉 REV-08); 图回到等数据."""
    s = await short_of_data()

    async with api_client(s.runner) as client:
        response = await client.post(unavailable_url("REQ-delivery_notes"), json={"reason": REASON})
        await wait_idle(s.runner, EID)
        listed = {r["id"]: r for r in (await client.get(REQUESTS)).json()}

    assert response.status_code == 200
    current = statuses(s)
    assert current["REV-08"] == (ProcedureStatus.no_data, ["REQ-delivery_notes"])
    assert current["REV-02"] == (ProcedureStatus.no_data, ["REQ-delivery_notes"])
    assert current["CSH-05"][0] is ProcedureStatus.awaiting_data
    assert (listed["REQ-delivery_notes"]["status"], listed["REQ-sales_invoices"]["status"]) == (
        "unavailable",
        "withdrawn",
    )
    assert (listed["REQ-bank_transactions"]["status"], listed["REQ-bank_transactions"]["used_for"]) == (
        "open",
        ["CSH-02", "CSH-05"],
    )
    assert await s.next_nodes() == ("await_data",)
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    withdrawn = [e.payload for e in trail.entries if e.payload.get("action") == "request_withdrawn"]
    assert withdrawn == [
        {"action": "request_withdrawn", "request_id": "REQ-sales_invoices", "table_kind": "sales_invoices"}
    ]


async def test_required_tables_and_unknown_requests_rejected() -> None:
    runner, _, data = await waiting_for_data()  # 还没有任何数据: 必需的三张表各有请求
    assert isinstance(data, FakeDataStore)
    received_at = {"status": "received", "resolved_at": NOW}
    data.data_requests["REQ-approval_log"] = table_request(TableKind.approval_log, "missing_table", {}, NOW).model_copy(
        update=received_at
    )
    async with api_client(runner) as client:
        required = await client.post(unavailable_url("REQ-accounts"), json={"reason": REASON})
        unknown = await client.post(unavailable_url("REQ-nope"), json={"reason": REASON})
        received = await client.post(unavailable_url("REQ-approval_log"), json={"reason": REASON})
        blank = await client.post(unavailable_url("REQ-approval_log"), json={"reason": "  "})
        engagements = runner.deps.engagement_store
        assert isinstance(engagements, FakeEngagementStore)
        await engagements.update(EID, locked=True)
        locked = await client.post(unavailable_url("REQ-approval_log"), json={"reason": REASON})

    assert (required.status_code, required.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert unknown.status_code == 404
    assert (received.status_code, received.json()["detail"]["code"]) == (409, "REQUEST_NOT_OPEN")
    assert (blank.status_code, blank.json()["detail"]["code"]) == (422, "MISSING_FIELD")  # 理由先于状态查
    assert (locked.status_code, locked.json()["detail"]["code"]) == (423, "ENGAGEMENT_LOCKED")
    assert {r.id: r.status for r in await data.requests(EID)}["REQ-accounts"] == "open"
    assert not runner.is_running(EID)


async def test_unavailable_and_withdrawn_trails_are_backfilled(monkeypatch: pytest.MonkeyPatch) -> None:
    """标记与撤回已提交而 request_unavailable, request_withdrawn 留痕没写成: 下次取项目锁做数据写操作时补写
    (backfill_trails), 内容与键同首次 (actor 为标记的审计师, 带理由与 used_for), 再取锁不重复."""
    s = await short_of_data()
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    write = trail.record
    failing = {"request_unavailable", "request_withdrawn"}

    async def broken(entry: TrailEntry) -> str:
        if entry.kind is TrailKind.data and entry.payload.get("action") in failing:
            raise RuntimeError("留痕写失败")
        return await write(entry)

    monkeypatch.setattr(trail, "record", broken)
    service = DataService(s.runner.deps, s.runner)
    with pytest.raises(RuntimeError):  # 标记已提交, 留痕写失败照常报错, 不交锁
        await service.mark_unavailable(EID, "REQ-delivery_notes", REASON, "U-1")
    assert not s.runner.is_running(EID) and await s.next_nodes() == ("await_data",)

    failing.discard("request_unavailable")  # 下次取锁补写标记的留痕; 交锁恢复后 REV-08, REV-02 记未执行, 撤回发票请求
    async with service.holding(EID, []):  # 撤回的留痕写失败 (体检出错只记日志)
        pass
    await wait_idle(s.runner, EID)
    requests = {r.id: r.status for r in await s.runner.deps.data.requests(EID)}
    assert (requests["REQ-delivery_notes"], requests["REQ-sales_invoices"]) == ("unavailable", "withdrawn")
    assert statuses(s)["REV-02"] == (ProcedureStatus.no_data, ["REQ-delivery_notes"])
    assert [e.payload["action"] for e in trail.entries if e.payload.get("action") == "request_withdrawn"] == []

    failing.clear()
    for _ in range(2):
        async with service.holding(EID, []):
            pass
        await wait_idle(s.runner, EID)
    entries = [e for e in trail.entries if e.payload.get("action") in {"request_unavailable", "request_withdrawn"}]
    assert sorted((e.payload["action"], e.payload["request_id"]) for e in entries) == [
        ("request_unavailable", "REQ-delivery_notes"),
        ("request_withdrawn", "REQ-sales_invoices"),
    ]
    [unavailable] = [e for e in entries if e.payload["action"] == "request_unavailable"]
    assert unavailable.actor == "U-1"
    assert unavailable.payload == {
        "action": "request_unavailable",
        "request_id": "REQ-delivery_notes",
        "table_kind": "delivery_notes",
        "reason": REASON,
        "used_for": ["REV-08"],
    }


async def test_request_changed_after_read_is_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """读过请求之后它被改了 (存储层只改仍为 open 的, 返回 None): 报 REQUEST_NOT_OPEN, 不写留痕, 锁释放."""
    s = await short_of_data()
    data = s.runner.deps.data
    assert isinstance(data, FakeDataStore)

    async def changed(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(data, "mark_unavailable", changed)
    service = DataService(s.runner.deps, s.runner)
    with pytest.raises(DataRejected, match="REQUEST_NOT_OPEN"):
        await service.mark_unavailable(EID, "REQ-delivery_notes", REASON, "U-1")
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assert not [e for e in trail.entries if e.payload.get("action") == "request_unavailable"]
    assert not s.runner.is_running(EID)


async def test_refresh_propagates_no_data_even_when_ids_declare_no_inputs() -> None:
    """refresh_inputs 的 ids 都没写 inputs, 而程序包里有 no_data 的前置: 照样往下游传播 (审核 R280-06)."""
    library = LIBRARY

    def procedure(pid: str, status: ProcedureStatus, ids: list[str] | None = None) -> Procedure:
        template = library.load(pid)
        return Procedure(
            id=pid,
            phase=template.phase,
            template_commit="x",
            autonomy_level=template.autonomy_level,
            status=status,
            data_request_ids=ids or [],
        )

    pack = [
        procedure("REV-03", ProcedureStatus.completed),
        procedure("REV-08", ProcedureStatus.no_data, ["REQ-delivery_notes"]),
        procedure("REV-02", ProcedureStatus.planned),
    ]
    store = FakeOutputStore(list(pack))
    nodes = make_graph_nodes(make_deps(FakeRegistry(), FakeTrail(), output_store=store))
    state = fill_defaults(initial_state(ENGAGEMENT, pack, 1))
    patch = await nodes.refresh_inputs(state, ["REV-03"])  # REV-03 没写 inputs
    [rev02] = [p for p in patch["procedure_pack"] if p.id == "REV-02"]
    assert (rev02.status, rev02.data_request_ids, rev02.version) == (ProcedureStatus.no_data, ["REQ-delivery_notes"], 2)


async def test_contract_marked_in_p1_keeps_doc04_and_rev03_from_running() -> None:
    """P1 等数据时卡片上已列出合同请求, 审计师先标客户无此类资料再确认数据已齐: DOC-04 记 no_data 且不执行 (审核
    R280-01: 原来会在 0 份合同上执行并 completed); 批准 PLN-01/03 后 REV-03 与经它的 REV-02 也记 no_data, 都不执行."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    data = FakeDataStore(ready=False, stats=REQUIRED_ONLY)
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagements, agents=real_agents, financial_store=financial, data=data)
    financial.document_refs = [d for d in financial.document_refs if d.type is not DocumentType.contract]
    s = Started(runner, store, engagements)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit_gate(s, acc01_decision(GateVerdict.approve, id="D-ACC"), ("p1_await_data",))
    async with api_client(runner) as client:
        listed = {r["id"]: r["status"] for r in (await client.get(REQUESTS)).json()}
        assert listed["REQ-contract"] == "open"
        marked = await client.post(unavailable_url("REQ-contract"), json={"reason": "客户本年没有书面合同"})
        assert marked.status_code == 200, marked.text
        ready = await client.post(f"/api/v1/engagements/{EID}/data/ready", json={"reason": "其余资料随后补交"})
        assert ready.status_code == 202, ready.text
        await wait_idle(runner, EID)

    assert runner.run_error(EID) is None, runner.run_error(EID)
    assert statuses(s)["DOC-04"] == (ProcedureStatus.no_data, ["REQ-contract"])
    assert not [r for r in runs(s) if r.startswith("RUN-DOC-04")]
    assert await s.next_nodes() == ("gate_p1_risk",)
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    moves = [(e.payload["from"], e.payload["to"]) for e in trail.entries if e.payload.get("id") == "DOC-04"]
    assert moves == [("planned", "no_data")]

    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    current = statuses(s)
    assert current["DOC-04"] == (ProcedureStatus.no_data, ["REQ-contract"])
    assert current["REV-03"] == (ProcedureStatus.no_data, ["REQ-contract"])
    assert current["REV-02"][0] is ProcedureStatus.no_data and "REQ-contract" in current["REV-02"][1]
    assert not [r for r in runs(s) if r.startswith(("RUN-DOC-04", "RUN-REV-03", "RUN-REV-02"))]


# ── 纯函数 ──


def test_unavailable_inputs_and_dependents_become_no_data() -> None:
    def procedure(pid: str, status: ProcedureStatus = ProcedureStatus.planned) -> Procedure:
        return Procedure(id=pid, phase=Phase.P2_Executing, template_commit="x", autonomy_level="L3", status=status)

    pack = [
        procedure("DOC-04", ProcedureStatus.awaiting_data).model_copy(
            update={"phase": Phase.P1_Planning, "data_request_ids": ["REQ-contract"]}
        ),
        procedure("REV-03"),
        procedure("REV-08"),
        procedure("REV-02"),
        procedure("CSH-04", ProcedureStatus.completed),
    ]
    have = OnHand(
        tables=frozenset({TableKind.sales_invoices, TableKind.bank_transactions}),
        unavailable=frozenset({DocumentType.contract, TableKind.delivery_notes}),
    )
    changed = {
        p.id: (p.status, p.data_request_ids, p.version)
        for p in input_versions(pack, LIBRARY, have, ["DOC-04", "REV-08", "REV-02"])
    }
    assert changed == {
        "DOC-04": (ProcedureStatus.no_data, ["REQ-contract"], 2),
        "REV-03": (ProcedureStatus.no_data, ["REQ-contract"], 2),  # 经前置 DOC-04, 不在 ids 中也传播
        "REV-08": (ProcedureStatus.no_data, ["REQ-delivery_notes"], 2),
        "REV-02": (ProcedureStatus.no_data, ["REQ-contract", "REQ-delivery_notes"], 2),  # 两个前置的并集
    }
    finished = [
        p.model_copy(update={"status": changed[p.id][0], "data_request_ids": changed[p.id][1]})
        if p.id in changed
        else p
        for p in pack
    ]
    assert input_versions(finished, LIBRARY, OnHand(), ["DOC-04", "REV-08", "REV-02"]) == []  # 终态, 不再改
    # 与程序包顺序无关: 下游排在前置之前时, 后变成 no_data 的前置也并进来 (审核 R280-06)
    reverse = [pack[0], pack[3], pack[2], pack[1], pack[4]]
    assert {
        p.id: (p.status, p.data_request_ids, p.version)
        for p in input_versions(reverse, LIBRARY, have, ["DOC-04", "REV-08", "REV-02"])
    } == changed


async def test_fin01_via_lists_only_prerequisites_that_passed_on_the_reason() -> None:
    """先标合同 (DOC-04, REV-03, REV-02 记 no_data, REV-02 的理由为合同), 之后再标发运单 (REV-08 才记 no_data):
    FIN-01 材料里 REV-02 的 via 只列传来理由的 REV-03, 不列后来才 no_data 的 REV-08 (审核 R280-10)."""
    from backend.orchestration.gates import not_executed

    def procedure(pid: str, ids: list[str]) -> Procedure:
        template = LIBRARY.load(pid)
        return Procedure(
            id=pid,
            phase=template.phase,
            template_commit="x",
            autonomy_level=template.autonomy_level,
            status=ProcedureStatus.no_data,
            data_request_ids=ids,
        )

    data = FakeDataStore()
    for kind in (TableKind.delivery_notes,):
        request = table_request(kind, "missing_table", {}, NOW, ["REV-08"]).model_copy(update={"status": "unavailable"})
        data.data_requests[request.id] = request
    deps = make_deps(FakeRegistry(), FakeTrail(), data=data)
    pack = [
        procedure("DOC-04", ["REQ-contract"]),
        procedure("REV-03", ["REQ-contract"]),
        procedure("REV-08", ["REQ-delivery_notes"]),
        procedure("REV-02", ["REQ-contract"]),
    ]
    state = fill_defaults(initial_state(ENGAGEMENT, pack, 1))
    rows = {n.procedure_id: n for n in await not_executed(deps, EID, state)}
    assert {pid: n.via for pid, n in rows.items()} == {
        "DOC-04": [],
        "REV-02": ["REV-03"],
        "REV-03": ["DOC-04"],
        "REV-08": [],
    }
    assert [r.id for r in rows["REV-08"].requests] == ["REQ-delivery_notes"]


def test_reconcile_keeps_unavailable_and_withdraws_unneeded_program_requests() -> None:
    def request(kind: TableKind, status: str = "open") -> DataRequest:
        return table_request(kind, "missing_table", {}, NOW, ["REV-08"]).model_copy(update={"status": status})

    unavailable = request(TableKind.delivery_notes, "unavailable")
    invoices = request(TableKind.sales_invoices)
    banks = request(TableKind.bank_accounts)
    approvals = table_request(TableKind.approval_log, "field_unavailable", {}, NOW)  # 体检请求
    have = OnHand(tables=frozenset({TableKind.bank_accounts}), unavailable=frozenset({TableKind.delivery_notes}))
    wanted = [*program_requests({"REV-08": ProcedureInputs(tables=[TableKind.delivery_notes])}, have, NOW)]
    assert wanted == []  # 标了客户无此类资料的不再要
    changes = reconcile([unavailable, invoices, banks, approvals], [request(TableKind.delivery_notes)], NOW, have)

    assert [r.id for r in changes.raised] == []  # unavailable 不重开
    assert {r.id: r.status for r in changes.saved} == {
        "REQ-sales_invoices": "withdrawn",  # 程序请求, 资料仍没有
        "REQ-bank_accounts": "received",  # 资料已到
        "REQ-approval_log": "received",  # 体检请求不再需要即算收到 (原规则)
    }
    assert [r.id for r in changes.withdrawn] == ["REQ-sales_invoices"]
    plain = reconcile([invoices], [], NOW)  # have 为空时一律按收到 (R15 的调用)
    assert [r.status for r in plain.saved] == ["received"]


def test_stats_marked_as_unavailable_do_not_count_as_present() -> None:
    from backend.services.data_quality import on_hand

    marked = table_request(TableKind.delivery_notes, "missing_table", {}, NOW, []).model_copy(
        update={"status": "unavailable"}
    )
    withdrawn = table_request(TableKind.sales_invoices, "missing_table", {}, NOW, []).model_copy(
        update={"status": "withdrawn"}  # 撤回的不算客户无此类资料 (只是不再需要)
    )
    have = on_hand({TableKind.accounts: TableStats(records=1)}, {DocumentType.contract: 2}, [marked, withdrawn])
    assert (have.tables, have.documents, have.unavailable) == (
        frozenset({TableKind.accounts}),
        frozenset({DocumentType.contract}),
        frozenset({TableKind.delivery_notes}),
    )
