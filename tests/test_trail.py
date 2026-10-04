"""R28 状态迁移留痕: 项目阶段与程序状态每变一次写一行 transition, 重放不重复 (Demo需求文档第 4.2 节, Q11 选 1)."""

from collections import Counter

import pytest

from backend.contracts.models import GateVerdict, Phase, ProcedureStatus, TrailEntry, TrailKind
from backend.orchestration import decisions
from backend.orchestration.nodes import update_procedure
from backend.orchestration.runner import GraphRunner
from backend.orchestration.trail import phase_transition, procedure_transitions, record_transitions
from tests.fakes import ENGAGEMENT, FakeOutputStore, FakeTrail, make_procedure, make_runner, wait_idle
from tests.test_decisions import Started, acc01_decision, started, stop_after_submit
from tests.test_flow import (
    FIN_APPROVE,
    RPT_APPROVE,
    RPT_REJECT,
    approve_materiality,
    review_all,
    start_numbat,
    submit_gate,
)
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
    REV-08, CSH-04, CSH-05, DOC-06 各 1 行 planned→completed; REV-03, REV-02, CSH-02 各 2 行, 经 awaiting_human)."""
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
