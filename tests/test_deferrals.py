"""稍后处理 (R1): 有效标记的规则, 以及 GET, POST /engagements/{eid}/deferrals.

复核任务的标记用 Numbat 跑到复核阶段 (Xero 没有发运单, 合同与回函, 跑不出 P2 判断与任务);
闸门的标记用 Xero Demo Company 快照的项目, 停在 ACC-01 (本机没有快照时跳过).
"""

from datetime import UTC, datetime, timedelta

import httpx

from backend.contracts.models import GateDecision, GateId, GateVerdict, ReviewTask, TrailEntry, TrailKind
from backend.services.deferrals import active_deferrals
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    FakeEngagementStore,
    FakeTrail,
    api_client,
    make_runner,
    wait_idle,
    xero_engagement,
    xero_snapshot,
)
from tests.test_decisions import Started, acc01_decision, started
from tests.test_flow import approve_materiality, start_numbat
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
TASK = "T-J-REV-03-1-CON-001-v1"  # Numbat 复核阶段的第一条待决任务
URL = f"/api/v1/engagements/{EID}/deferrals"


def entry(target_type: str, target_id: str, deferred: bool, minutes: int) -> TrailEntry:
    return TrailEntry(
        id=f"TR-{target_id}-{minutes}",
        engagement_id=EID,
        kind=TrailKind.deferral,
        actor="U-1",
        summary="",
        payload={"target_type": target_type, "target_id": target_id, "deferred": deferred},
        created_at=NOW + timedelta(minutes=minutes),
    )


def task(task_id: str, decided: bool = False) -> ReviewTask:
    extra = {"status": "decided", "version": 2} if decided else {}
    return ReviewTask.model_validate(
        {
            "id": task_id,
            "kind": "normal",
            "procedure_id": "REV-03",
            "target_type": "Judgment",
            "target_id": "J",
            "target_version": 1,
            "run_id": "RUN-REV-03-1",
            "created_at": NOW,
            **extra,
        }
    )


def gate_decision(minutes: int) -> GateDecision:
    return acc01_decision(GateVerdict.insufficient_info, decided_at=NOW + timedelta(minutes=minutes))


# ── 有效标记的规则 (纯函数) ──


def test_latest_entry_per_target_decides_and_order_follows_mark_time() -> None:
    entries = [
        entry("review_task", "T-2", True, 1),
        entry("review_task", "T-1", True, 2),
        entry("review_task", "T-2", False, 3),  # T-2 最后一条是取消
        entry("review_task", "T-2", True, 4),  # 又标记了
        entry("review_task", "T-3", True, 5),
        entry("review_task", "T-3", False, 6),
    ]
    tasks = {t: task(t) for t in ("T-1", "T-2", "T-3")}
    active = active_deferrals(entries, tasks, None, [])
    assert [(e.payload["target_id"], e.id) for e in active] == [("T-1", "TR-T-1-2"), ("T-2", "TR-T-2-4")]


def test_decided_or_missing_task_is_not_active() -> None:
    entries = [entry("review_task", "T-1", True, 1), entry("review_task", "T-9", True, 2)]
    assert active_deferrals(entries, {"T-1": task("T-1", decided=True)}, None, []) == []


def test_gate_mark_needs_current_gate_and_no_later_decision() -> None:
    entries = [entry("gate", "ACC-01", True, 5)]
    assert active_deferrals(entries, {}, GateId.PLN_01_03, []) == []  # 已不在这道闸门
    assert active_deferrals(entries, {}, GateId.ACC_01, [gate_decision(4)]) == entries  # 决定早于标记
    assert active_deferrals(entries, {}, GateId.ACC_01, [gate_decision(6)]) == []  # 标记之后作出了决定


# ── 接口: 复核任务 (Numbat 跑到复核阶段) ──


async def reviewing() -> Started:
    """Numbat 项目过了 ACC-01 与 PLN-01/03, 停在复核阶段, 6 条待决任务."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    return s


def deferral_entries(s: Started) -> list[TrailEntry]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.deferral]


async def mark(client: httpx.AsyncClient, target_type: str, target_id: str, deferred: bool = True) -> httpx.Response:
    return await client.post(URL, json={"target_type": target_type, "target_id": target_id, "deferred": deferred})


async def test_mark_then_cancel_task() -> None:
    s = await reviewing()
    async with api_client(s.runner) as client:
        marked = await mark(client, "review_task", TASK)
        assert marked.status_code == 202
        listed = (await client.get(URL)).json()
        assert [(d["target_type"], d["target_id"], d["deferred_by"]) for d in listed] == [("review_task", TASK, "U-1")]

        cancelled = await mark(client, "review_task", TASK, deferred=False)
        assert cancelled.status_code == 202 and cancelled.json()["id"] != marked.json()["id"]
        assert (await client.get(URL)).json() == []

    entries = deferral_entries(s)
    assert [(e.actor, e.summary, e.payload) for e in entries] == [
        ("U-1", f"稍后处理 {TASK}", {"target_type": "review_task", "target_id": TASK, "deferred": True}),
        ("U-1", f"取消稍后处理 {TASK}", {"target_type": "review_task", "target_id": TASK, "deferred": False}),
    ]
    assert [e.id for e in entries] == [marked.json()["id"], cancelled.json()["id"]]


async def test_repeated_mark_is_idempotent() -> None:
    s = await reviewing()
    async with api_client(s.runner) as client:
        first = await mark(client, "review_task", TASK)
        second = await mark(client, "review_task", TASK)
    assert second.status_code == 202 and second.json() == first.json()
    assert len(deferral_entries(s)) == 1


async def test_decided_task_drops_out_and_cannot_be_marked() -> None:
    s = await reviewing()
    async with api_client(s.runner) as client:
        await mark(client, "review_task", TASK)
        await s.submit.submit(EID, record(TASK, id="D-CON-001"))
        await wait_idle(s.runner, EID)
        assert (await client.get(URL)).json() == []  # 决定后自动消失, 不用补"取消"

        again = await mark(client, "review_task", TASK)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "TASK_NOT_PENDING"
    assert len(deferral_entries(s)) == 1


async def test_rejects_unknown_task_wrong_gate_and_unmarked_cancel() -> None:
    s = await reviewing()
    async with api_client(s.runner) as client:
        unknown = await mark(client, "review_task", "T-NOPE")
        wrong_gate = await mark(client, "gate", "ACC-01")  # 停在复核阶段, 不在闸门
        never_marked = await mark(client, "review_task", TASK, deferred=False)
    assert (unknown.status_code, unknown.json()["detail"]["code"]) == (404, "NOT_FOUND")
    assert (wrong_gate.status_code, wrong_gate.json()["detail"]["code"]) == (409, "NOT_AT_GATE")
    assert (never_marked.status_code, never_marked.json()["detail"]["code"]) == (404, "NOT_FOUND")
    assert deferral_entries(s) == []


async def test_locked_engagement_rejects_marks() -> None:
    s = await started()
    await s.engagement_store.update(EID, locked=True)
    async with api_client(s.runner) as client:
        response = await mark(client, "gate", "ACC-01")
    assert (response.status_code, response.json()["detail"]["code"]) == (423, "ENGAGEMENT_LOCKED")


# ── 接口: 闸门 (Xero 项目停在 ACC-01) ──


async def test_xero_gate_mark_ends_with_gate_decision() -> None:
    """Xero 项目停在 ACC-01 (登记查询查不到它的 ABN, 评分卡带例外): 标记闸门后可读回;
    审计师作出"信息不足"决定后闸门原地重新呈报, 旧标记随那次决定失效, 可以重新标记."""
    profile, engagement = xero_engagement(xero_snapshot())
    engagement_store = FakeEngagementStore([profile], [engagement])
    runner, store = make_runner(engagement_store=engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    s = Started(runner, store, engagement_store)

    async with api_client(runner) as client:
        first = await mark(client, "gate", "ACC-01")
        assert [d["target_id"] for d in (await client.get(URL)).json()] == ["ACC-01"]

        await s.submit.submit(EID, acc01_decision(GateVerdict.insufficient_info, decided_at=datetime.now(UTC)))
        await wait_idle(runner, EID)
        assert await s.next_nodes() == ("gate_acc01",)
        assert (await client.get(URL)).json() == []

        second = await mark(client, "gate", "ACC-01")
        assert second.json()["id"] != first.json()["id"]
        assert [d["target_id"] for d in (await client.get(URL)).json()] == ["ACC-01"]
