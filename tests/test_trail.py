"""R28 状态迁移留痕: 项目阶段与程序状态每变一次写一行 transition, 重放不重复 (Demo需求文档第 4.2 节, Q11 选 1)."""

from collections import Counter
from dataclasses import replace
from datetime import date
from typing import Any, cast

import pytest
from langgraph.types import Command

from backend.agents.base import RunContext
from backend.contracts.knowledge import LocalizedText
from backend.contracts.models import (
    AgentId,
    AuditException,
    GateVerdict,
    Phase,
    PopulationItem,
    ProcedureRun,
    ProcedureStatus,
    RulePopulation,
    RunPopulation,
    RunStatus,
    TaskStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration import decisions
from backend.orchestration.nodes import GraphNodes, update_procedure
from backend.orchestration.runner import GraphRunner
from backend.orchestration.trail import (
    phase_transition,
    procedure_transitions,
    record_transitions,
    run_finished,
    run_started,
)
from backend.services.run_summary import run_result
from tests.fakes import (
    ENGAGEMENT,
    KNOWLEDGE,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    FakeOutputStore,
    FakeTrail,
    ScriptA4,
    finished,
    knowledge_library,
    make_procedure,
    make_run,
    make_runner,
    preset_rerun,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, started, stop_after_submit
from tests.test_fieldwork import assembled, dispatch, fail_rev08
from tests.test_flow import (
    FIN_APPROVE,
    RPT_APPROVE,
    RPT_REJECT,
    approve_materiality,
    review_all,
    start_numbat,
    submit_gate,
)
from tests.test_merge import exception, judgment, task
from tests.test_procedure_inputs import runs, short_of_data
from tests.test_rerun import failed_at_p0, pending_task
from tests.test_rerun_decision import at_review, flaky_rev08, rerun

EID = ENGAGEMENT.id


def transitions(trail: object) -> list[tuple[str, str, str, str]]:
    """transition 留痕的 (对象, ID, 原状态, 新状态), 按写入顺序."""
    assert isinstance(trail, FakeTrail)
    return [
        (e.payload["object"], e.payload["id"], e.payload["from"], e.payload["to"])
        for e in trail.entries
        if e.kind is TrailKind.transition
    ]


def started_transitions(s: Started) -> list[tuple[str, str, str, str]]:
    return transitions(s.runner.deps.trail)


# ── 验收 1: 留痕的内容与防重复 ──


def test_phase_transition_entry() -> None:
    """阶段变化: actor A1, 键以新阶段结尾, payload 无版本号; 阶段没变不写."""
    [entry] = phase_transition(EID, Phase.P0_Acceptance, Phase.P1_Planning, "ACC-01")

    assert (entry.kind, entry.actor, entry.procedure_id) == (TrailKind.transition, "A1", "ACC-01")
    assert entry.idempotency_key == f"{EID}:transition:engagement:{EID}:P1_Planning"
    assert entry.payload == {
        "object": "engagement",
        "id": EID,
        "from": "P0_Acceptance",
        "to": "P1_Planning",
        "version": None,
    }
    assert phase_transition(EID, Phase.P1_Planning, Phase.P1_Planning) == []


async def test_procedure_transitions_compare_with_previous_version() -> None:
    """程序新版本与 Store 中上一版比: 状态变了写一行 (键带新版本号); 首版与状态未变的新版本不写; 不是程序的跳过."""
    v1 = make_procedure("ACC-04")
    v2 = update_procedure([v1], "ACC-04", status=ProcedureStatus.awaiting_human)[0]
    v3 = update_procedure([v2], "ACC-04", rerun_subject_ids=["X"])[0]  # 状态未变
    v4 = update_procedure([v3], "ACC-04", status=ProcedureStatus.completed)[0]
    store = FakeOutputStore([v1, v2, v3, v4])

    entries = await procedure_transitions(store, EID, [v1, v2, v3, v4, acc01_decision(GateVerdict.approve)])

    assert [(e.idempotency_key, e.procedure_id, e.payload) for e in entries] == [
        (
            f"{EID}:transition:procedure:ACC-04:v2",
            "ACC-04",
            {"object": "procedure", "id": "ACC-04", "from": "planned", "to": "awaiting_human", "version": 2},
        ),
        (
            f"{EID}:transition:procedure:ACC-04:v4",
            "ACC-04",
            {"object": "procedure", "id": "ACC-04", "from": "awaiting_human", "to": "completed", "version": 4},
        ),
    ]


async def test_record_transitions_skips_existing_keys() -> None:
    """同键已有留痕时跳过, 不重复写."""
    trail = FakeTrail()
    entries: list[TrailEntry] = phase_transition(EID, Phase.P0_Acceptance, Phase.P1_Planning)

    await record_transitions(trail, entries)
    await record_transitions(trail, entries)

    assert len(trail.entries) == 1


# ── 验收 3, 4: 承接阶段 ──


async def test_acc01_approve_writes_three_transitions() -> None:
    """验收 3: 启动后 ACC-04 planned→completed; ACC-01 approve 后另有 ACC-01 planned→completed 与项目 P0→P1."""
    s = await started()
    assert started_transitions(s) == [("procedure", "ACC-04", "planned", "completed")]

    stop_after_submit(s)

    await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert started_transitions(s) == [
        ("procedure", "ACC-04", "planned", "completed"),
        ("procedure", "ACC-01", "planned", "completed"),
        ("engagement", EID, "P0_Acceptance", "P1_Planning"),
    ]


async def test_acc01_reject_writes_declined_once() -> None:
    """拒绝承接: ACC-01 planned→completed 与项目 P0→Declined 各一行; declined 终点补写同键, 不重复."""
    s = await started()

    await s.submit.submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ()
    assert started_transitions(s)[1:] == [
        ("procedure", "ACC-01", "planned", "completed"),
        ("engagement", EID, "P0_Acceptance", "Declined"),
    ]


async def test_declined_writes_phase_when_submit_did_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """DecisionSubmit 提交后没写成 transition (如进程在此中断): declined 终点补写进入 Declined 的阶段留痕."""

    async def lost(trail: object, entries: object) -> None:
        return None

    monkeypatch.setattr(decisions, "record_transitions", lost)
    s = await started()

    await s.submit.submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)

    assert started_transitions(s)[1:] == [("engagement", EID, "P0_Acceptance", "Declined")]


async def test_rewind_and_replay_p0_intel_adds_no_transition() -> None:
    """验收 4: 回退到 p0_intel 之前的检查点重放, transition 行数不变."""
    runner, _ = make_runner()
    config = GraphRunner.config(EID)
    await runner.start(EID)
    await wait_idle(runner, EID)
    count = len(transitions(runner.deps.trail))
    before = [s async for s in runner.app.aget_state_history(config) if s.next == ("p0_intel",)]

    await runner.app.ainvoke(None, before[0].config)

    assert (await runner.app.aget_state(config)).next == ("gate_acc01",)
    assert count == 1 and len(transitions(runner.deps.trail)) == count


# ── 验收 2: 复核决定 ──


async def test_rerun_decision_transitions() -> None:
    """P2 程序失败后 rerun: 汇合时 REV-08 planned→awaiting_human, rerun 决定 awaiting_human→executing,
    重跑汇合后 executing→completed."""
    s = await at_review(flaky_rev08(1))

    await s.submit.submit(EID, rerun("T-RUN-REV-08-1-v1", "D-R1"))
    await wait_idle(s.runner, EID)

    assert [(b, a) for obj, pid, b, a in started_transitions(s) if pid == "REV-08"] == [
        ("planned", "awaiting_human"),
        ("awaiting_human", "executing"),
        ("executing", "completed"),
    ]


# ── 验收 5: 全流程 ──


async def test_m2_flow_transitions() -> None:
    """验收 5: Numbat 全流程到归档 (Q6 选不写派发版本): 项目阶段 5 行, 程序 20 行 (固定集 10 条各 1 行;
    REV-08, CSH-04, CSH-05, DOC-06 各 1 行 planned→completed; REV-03, REV-02, CSH-02 各 2 行, 经 awaiting_human).
    同一次走通另核 run 留痕 (需求 R325, 审核 R325-06: 不另跑一遍全流程)."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    await review_all(s)
    await submit_gate(s, FIN_APPROVE, ("gate_rpt01",))
    await submit_gate(s, RPT_REJECT, ("gate_rpt01",))
    await submit_gate(s, RPT_APPROVE, ())

    rows = started_transitions(s)
    assert [(before, after) for obj, _, before, after in rows if obj == "engagement"] == [
        ("P0_Acceptance", "P1_Planning"),
        ("P1_Planning", "P2_Executing"),
        ("P2_Executing", "P3_Completion"),
        ("P3_Completion", "P4_Reporting"),
        ("P4_Reporting", "Archived"),
    ]
    procedures = [(pid, before, after) for obj, pid, before, after in rows if obj == "procedure"]
    fixed = ["ACC-04", "ACC-01", "DOC-04", "PLN-07", "PLN-01", "PLN-03", "FIN-04", "FIN-01", "RPT-03", "RPT-01"]
    once = ["REV-08", "CSH-04", "CSH-05", "DOC-06"]
    reviewed = ["REV-03", "REV-02", "CSH-02"]
    assert Counter(pid for pid, _, _ in procedures) == Counter(
        {**dict.fromkeys(fixed + once, 1), **dict.fromkeys(reviewed, 2)}
    )
    for pid in fixed + once:
        assert [(b, a) for p, b, a in procedures if p == pid] == [("planned", "completed")], pid
    for pid in reviewed:
        assert [(b, a) for p, b, a in procedures if p == pid] == [
            ("planned", "awaiting_human"),
            ("awaiting_human", "completed"),
        ], pid
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    keys = [e.idempotency_key for e in trail.entries if e.kind is TrailKind.transition]
    assert len(keys) == len(set(keys)) == 25
    # 需求 R325 验收 3, 7: Store 中每次执行恰有开始与结束各一行 run 留痕, 开始在前, 结束的 status 同执行记录, 键不重复
    entries = runs_of(trail)
    stored = {r.id: r for r in await s.output_store.latest(EID, ProcedureRun)}
    assert len(entries) == 2 * len(stored) and len({e.idempotency_key for e in entries}) == len(entries)
    for run_id, run in stored.items():
        actions = [e.payload for e in entries if e.run_id == run_id]
        assert [p["action"] for p in actions] == ["started", "finished"], run_id
        assert actions[1]["status"] == run.status.value, run_id


# ── 需求 R325: 执行进度留痕 (每次执行开始, 结束各一行 run 留痕) ──

LIBRARY = knowledge_library(KNOWLEDGE)
WINDOW = {"window_days": 20, "period_end": date(2026, 9, 30)}


def runs_of(trail: object) -> list[TrailEntry]:
    """run 留痕, 按写入顺序."""
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.run]


def run_keys(trail: object) -> list[str | None]:
    return [e.idempotency_key for e in runs_of(trail)]


def base_payload(action: str, run: ProcedureRun, phase: str, scope: dict[str, str] | None) -> dict[str, Any]:
    return {
        "action": action,
        "procedure_id": run.procedure_id,
        "run_id": run.id,
        "run_no": run.run_no,
        "trigger": run.trigger.value,
        "phase": phase,
        "scope": scope,
    }


REV08_SCOPE = {
    "zh": "期末前 20 天 (2026-09-10 至 2026-09-30) 入账的销售发票",
    "en": "Sales invoices posted in the 20 days before period end (2026-09-10 to 2026-09-30)",
}


@pytest.mark.parametrize(
    ("procedure_id", "params", "run_no", "failure", "scope", "summary"),
    [
        ("REV-08", WINDOW, 1, None, REV08_SCOPE, "REV-08 凭证三单匹配 完成"),
        # 参数快照经 Store 读回后日期为文本, 照样渲染; 重跑的 trigger 为 rerun
        ("REV-08", WINDOW | {"period_end": "2026-09-30"}, 2, None, REV08_SCOPE, "REV-08 凭证三单匹配 完成"),
        ("REV-03", {}, 1, "RuleError: boom", None, "REV-03 收入合同条款评价 执行失败: RuleError: boom"),  # 没写 scope
    ],
)
def test_run_entries_carry_scope_status_and_result(
    procedure_id: str,
    params: dict[str, Any],
    run_no: int,
    failure: str | None,
    scope: dict[str, str] | None,
    summary: str,
) -> None:
    """验收 1, 2: 开始与结束各一行, actor A1, 键 {eid}:run:{run_id}:started 与 :finished; payload 带程序, 执行, 第几次,
    起因, 程序所属阶段与按参数快照渲染的范围; 结束另带执行状态 (status, 同 ProcedureRun), 失败原因 (同执行记录) 与
    结果摘要."""
    template = LIBRARY.load(procedure_id)
    if scope is None:  # 现有程序都写了 scope (需求 R327), 没写的情形把模板的 scope 去掉来构造
        template = template.model_copy(update={"scope": None})
    run = make_run(procedure_id, run_no, params)
    started = run_started(EID, template, run)
    assert (started.kind, started.actor, started.procedure_id, started.run_id) == (
        TrailKind.run,
        AgentId.A1,
        procedure_id,
        run.id,
    )
    assert started.idempotency_key == f"{EID}:run:{run.id}:started"
    assert started.summary == f"开始执行 {procedure_id} {template.name}"
    assert started.payload == base_payload("started", run, "P2_Executing", scope)
    status = RunStatus.failed if failure else RunStatus.succeeded
    ended = finished(run, status).model_copy(update={"failure_reason": failure})
    result = None if failure else {"exceptions": 1}
    entry = run_finished(EID, template, ended, result)
    assert (entry.idempotency_key, entry.summary) == (f"{EID}:run:{run.id}:finished", summary)
    assert entry.payload == base_payload("finished", run, "P2_Executing", scope) | {
        "status": status.value,
        "failure_reason": failure,
        "result": result,
    }


def xid(n: int, run_id: str = "RUN-REV-08-1") -> AuditException:
    return exception(f"X-REV-08-1-R01-{n}", run_id=run_id)


def items(*pairs: tuple[str, int]) -> list[PopulationItem]:
    return [PopulationItem(source_id=i, amount_cents=a, fields={}) for i, a in pairs]


def population(*rules: RulePopulation) -> RunPopulation:
    return RunPopulation(id="POP-RUN-REV-08-1", run_id="RUN-REV-08-1", procedure_id="REV-08", rules=list(rules))


R01 = RulePopulation(
    rule_id="R01", source_type="sales_invoice", items=items(("A", 100), ("B", 200)), count=2, amount_cents=300
)
R02 = RulePopulation(
    rule_id="R02", source_type="sales_invoice", items=items(("B", 200), ("C", 300)), count=2, amount_cents=500
)
OWN, OTHER = "RUN-REV-03-1", "RUN-REV-03-2"
PENDING = task(run_id=OWN)
DECIDED = PENDING.model_copy(update={"id": "T-decided", "status": TaskStatus.decided})


@pytest.mark.parametrize(
    ("procedure_id", "run_id", "patch", "pop", "expected"),
    [
        (  # 规则程序: 各规则的总体, 去重后 3 项 600, 只数本次执行的例外
            "REV-08",
            "RUN-REV-08-1",
            {"exceptions": [xid(1), xid(2, "RUN-REV-08-0")]},
            population(R01, R02),
            {
                "populations": [
                    {"rule_id": "R01", "count": 2, "amount_cents": 300},
                    {"rule_id": "R02", "count": 2, "amount_cents": 500},
                ],
                "items": 3,
                "amount_cents": 600,
                "exceptions": 1,
            },
        ),
        (  # 有规则未记录: 去重数与合计不给
            "REV-08",
            "RUN-REV-08-1",
            {"exceptions": []},
            population(R01, RulePopulation(rule_id="R02")),
            {
                "populations": [
                    {"rule_id": "R01", "count": 2, "amount_cents": 300},
                    {"rule_id": "R02", "count": None, "amount_cents": None},
                ],
                "items": None,
                "amount_cents": None,
                "exceptions": 0,
            },
        ),
        ("REV-08", "RUN-REV-08-1", {"exceptions": [xid(1)]}, None, {"exceptions": 1}),  # 没有总体 (不经规则引擎)
        (  # 判断程序: 本次执行的判断数与待决任务数
            "REV-03",
            OWN,
            {
                "judgments": [judgment("J-1", run_id=OWN), judgment("J-2", run_id=OWN), judgment("J-3", run_id=OTHER)],
                "review_tasks": [PENDING, DECIDED, PENDING.model_copy(update={"id": "T-other", "run_id": OTHER})],
            },
            None,
            {"exceptions": 0, "judgments": 2, "pending_reviews": 1},
        ),
        (  # 抽取程序: 交回的文件数, 抽取失败的 failed 任务待决
            "DOC-04",
            "RUN-DOC-04-1",
            {"documents": NUMBAT_CONTRACTS, "review_tasks": [PENDING.model_copy(update={"run_id": "RUN-DOC-04-1"})]},
            None,
            {"pending_reviews": 1, "documents": 3},
        ),
    ],
)
def test_run_result_by_program_kind(
    procedure_id: str, run_id: str, patch: StatePatch, pop: RunPopulation | None, expected: dict[str, Any]
) -> None:
    """验收 1: 结果摘要按程序种类只给有的项 (规则程序给总体与例外, 判断程序给判断与待复核, 抽取程序给文件), 只数
    run_id 为本次执行的对象 (重放取回的片段带着别的执行的对象)."""
    run_no = int(run_id.rsplit("-", 1)[1])
    run = finished(make_run(procedure_id, run_no))
    assert run_result(LIBRARY.load(procedure_id), run, patch, pop) == expected


def test_failed_run_has_no_result() -> None:
    """验收 2: 执行失败没有结果摘要 (原因见 failure_reason)."""
    run = finished(make_run("REV-08"), RunStatus.failed)
    assert run_result(LIBRARY.load("REV-08"), run, {"exceptions": []}, population(R01)) is None


class StoreAwareTrail(FakeTrail):
    """假留痕, 另记下每行 run 留痕写入时: 其执行记录在不在 Store 中, 有没有进行中的 Store 事务. 假存储在同一任务里
    看得到事务内未提交的写入, 所以 "已在 Store" 区分不了事务内与提交后, 另看事务 (审核 R325-02)."""

    def __init__(self) -> None:
        super().__init__()
        self.store: FakeOutputStore | None = None
        # 防重复键 -> (写入时执行记录已在 Store, 写入时不在事务里)
        self.at_write: dict[str, tuple[bool, bool]] = {}

    async def record(self, entry: TrailEntry) -> str:
        if entry.kind is TrailKind.run and self.store is not None and entry.run_id is not None:
            stored = await self.store.get(EID, ProcedureRun, entry.run_id) is not None
            self.at_write[cast(str, entry.idempotency_key)] = (stored, self.store.active_txn() is None)
        return await super().record(entry)


async def assembled_with_trail(
    script: Any = None,
) -> tuple[GraphNodes, FakeOutputStore, EngagementState, StoreAwareTrail]:
    """Numbat 装配后的项目 (tests/test_fieldwork.assembled), 留痕换成 StoreAwareTrail."""
    nodes, store, state = await (assembled(script) if script is not None else assembled())
    trail = StoreAwareTrail()
    trail.store = store
    nodes.deps = replace(nodes.deps, trail=trail)
    return nodes, store, state, trail


async def test_attempt_writes_started_before_the_agent_and_finished_after_commit() -> None:
    """验收 1 (P2 以外): 开始留痕在调 Agent 之前写 (Agent 执行时已有, 执行记录还不在 Store), 结束留痕在终态提交之后
    写, 先于程序状态的 transition."""
    seen: list[list[str | None]] = []

    def pln07(ctx: RunContext) -> StatePatch:
        seen.append(run_keys(trail))
        return {"procedure_runs": [finished(ctx.run)]}

    nodes, _, state, trail = await assembled_with_trail()
    nodes.agents["PLN-07"] = ScriptA4(nodes.deps, pln07)
    engagement = ENGAGEMENT.model_copy(update={"client_profile_version": NUMBAT_PROFILE.version})
    await nodes.attempt(state, engagement, "PLN-07")

    started, ended = f"{EID}:run:RUN-PLN-07-1:started", f"{EID}:run:RUN-PLN-07-1:finished"
    assert seen == [[started]]
    assert run_keys(trail) == [started, ended]
    assert trail.at_write == {started: (False, True), ended: (True, True)}
    # PLN-07 planned → completed 的 transition 在结束留痕之后
    order = [(e.kind, e.payload.get("id")) for e in trail.entries]
    assert order == [(TrailKind.run, None), (TrailKind.run, None), (TrailKind.transition, "PLN-07")]


async def test_fieldwork_writes_started_and_fan_in_finished_once() -> None:
    """验收 1, 3 (P2): fieldwork 里每条程序调 Agent 之前写开始 (此时不写 Store); fan_in 提交之后按暂存项顺序写结束;
    fan_in 重做与 fieldwork 重放都同键跳过; 结束留痕没写成 (删掉) 的, 重放后补写, 内容与第一次相同."""
    nodes, _, state, trail = await assembled_with_trail()
    staged = await nodes.fieldwork(dispatch(state, "REV-03", "REV-08"))
    state["staged_results"] = staged["staged_results"]
    keys = [f"{EID}:run:RUN-{pid}-1:{a}" for pid in ("REV-03", "REV-08") for a in ("started", "finished")]
    assert run_keys(trail) == [keys[0], keys[2]]
    await nodes.fan_in(state)
    assert run_keys(trail) == [keys[0], keys[2], keys[1], keys[3]]
    before, after = (False, True), (True, True)  # 开始: 还不在 Store; 结束: 已提交, 不在事务里
    assert trail.at_write == {keys[0]: before, keys[2]: before, keys[1]: after, keys[3]: after}
    first = runs_of(trail)

    await nodes.fan_in(state)  # Store 已写, 检查点没跟上, 带着原暂存项重做
    replayed = await nodes.fieldwork(dispatch(state, "REV-03", "REV-08"))  # 回退检查点后重放
    assert runs_of(trail) == first
    trail.entries[:] = [e for e in trail.entries if e.idempotency_key != keys[3]]  # REV-08 的结束留痕没写成
    await nodes.fan_in(state | {"staged_results": replayed["staged_results"]})
    [again] = [e for e in runs_of(trail) if e.idempotency_key == keys[3]]
    assert again.payload == next(e for e in first if e.idempotency_key == keys[3]).payload
    assert len(runs_of(trail)) == 4


async def test_attempt_replay_after_rerun_writes_each_key_once() -> None:
    """验收 3 (P2 以外, 审核 R325-01): ACC-04 第 1 次失败, 节点内等待; rerun 决定恢复后节点从头执行, 第 1 次按重放取
    Store (attempt 再走一遍, 两行同键跳过), 再执行第 2 次: run 留痕的键恰为两次执行的开始与结束各一行."""
    runner, store, _ = await failed_at_p0()
    await preset_rerun(store, await pending_task(store))
    await runner.app.ainvoke(Command(resume="D-1"), GraphRunner.config(EID))
    assert run_keys(runner.deps.trail) == [
        f"{EID}:run:RUN-ACC-04-{n}:{action}" for n in (1, 2) for action in ("started", "finished")
    ]


@pytest.mark.parametrize("change", ["window_days", "extra_days"], ids=["param_changed", "scope_param_added"])
async def test_backfilled_entries_take_the_run_in_store(change: str, caplog: pytest.LogCaptureFixture) -> None:
    """验收 3 (审核 R325-05): 开始与结束留痕都没写成而执行在 Store 中已是终态 (如升级前跑过的执行) 时, 重放按 Store 中的
    那次补写. 期间程序库改了参数 (REV-08 的 window_days 各档加 7 天): 补写的与第一次相同, 开始与结束是同一范围.
    程序库给 scope 加了快照里没有的参数 (extra_days): 渲染不出, 两行的 scope 记为空, 记一行 warning (执行 ID 与缺的
    参数), 节点不报错, 其余字段同第一次."""
    nodes, _, state, trail = await assembled_with_trail()
    state["staged_results"] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]
    await nodes.fan_in(state)
    keys = [f"{EID}:run:RUN-REV-08-1:{a}" for a in ("started", "finished")]
    first = [next(e for e in trail.entries if e.idempotency_key == key) for key in keys]
    trail.entries[:] = [e for e in trail.entries if e.idempotency_key not in keys]
    original = nodes.deps.library.load
    template = original("REV-08")
    assert template.scope is not None
    if change == "window_days":
        window = {"by_risk": {k: v + 7 for k, v in template.params["window_days"]["by_risk"].items()}}
        update: dict[str, Any] = {"params": {**template.params, "window_days": window}}
    else:
        scope = LocalizedText(
            zh=template.scope.zh + ", 另加 {{ extra_days }} 天", en=template.scope.en + " + {{ extra_days }}"
        )
        update = {"params": {**template.params, "extra_days": 3}, "scope": scope}
    changed = template.model_copy(update=update)
    nodes.deps.library.load = lambda pid: changed if pid == "REV-08" else original(pid)  # type: ignore[method-assign]

    replayed = await nodes.fieldwork(dispatch(state, "REV-08"))
    await nodes.fan_in(state | {"staged_results": replayed["staged_results"]})

    again = [next(e for e in trail.entries if e.idempotency_key == key) for key in keys]
    if change == "window_days":
        assert [e.payload for e in again] == [e.payload for e in first]
        assert again[0].payload["scope"] == again[1].payload["scope"]
    else:
        assert [e.payload for e in again] == [e.payload | {"scope": None} for e in first]
        assert [r.getMessage() for r in caplog.records if "RUN-REV-08-1" in r.getMessage()] == [
            "执行 RUN-REV-08-1 的范围说明渲染不出, scope 记为空: 'extra_days' is undefined"
        ] * 2


async def test_failed_run_finishes_once_with_its_reason() -> None:
    """验收 2, 3: REV-08 抛 RuleError: 结束留痕 status 为 failed, failure_reason 同执行记录, 没有结果; fan_in 重做
    不重复写 (find_success 不看 payload 的 status, 失败执行的结束留痕照样算写成了)."""
    nodes, store, state, trail = await assembled_with_trail(fail_rev08)
    state["staged_results"] = (await nodes.fieldwork(dispatch(state, "REV-08")))["staged_results"]
    await nodes.fan_in(state)
    await nodes.fan_in(state)
    run = await store.get(EID, ProcedureRun, "RUN-REV-08-1")
    assert run is not None and run.status is RunStatus.failed
    [_, end] = runs_of(trail)
    assert end.summary == f"REV-08 凭证三单匹配 执行失败: {run.failure_reason}"
    assert (end.payload["status"], end.payload["failure_reason"], end.payload["result"]) == (
        "failed",
        run.failure_reason,
        None,
    )


async def test_programs_waiting_for_data_write_no_run_entries() -> None:
    """验收 2: 程序等数据 (R272) 时不建执行, 不写 run 留痕 (等待沿用程序的 transition 留痕); 执行过的三条各一对."""
    s = await short_of_data()
    entries = runs_of(s.runner.deps.trail)
    assert sorted((e.run_id, e.payload["action"]) for e in entries) == sorted(
        (run, action) for run in runs(s) for action in ("started", "finished")
    )
