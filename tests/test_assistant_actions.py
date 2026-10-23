"""授权批次必须固定范围、整体校验、可重试, 并只启动一次流程恢复."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from backend.contracts.assistant_actions import AssistantPlanIn
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    EvidenceRequest,
    Procedure,
    RerunRequest,
    ReviewTask,
    ScopeLimitation,
    TaskStatus,
    TrailKind,
)
from backend.services.assistant_actions import AssistantActions, direct_instruction
from backend.services.features import MORE_EVIDENCE
from tests.fakes import NUMBAT_FEATURES, wait_idle
from tests.test_evidence_rejudge import evidence_decision
from tests.test_review_decisions import EID, waiting_on_cutoff


@pytest.mark.parametrize(
    "instruction,count",
    [
        ("全部标记为无法取得", None),
        ("Please mark all 2 current evidence requests unavailable", 2),
        ("请将当前两项待补资料请求全部标记为无法取得", 2),
        ("把当前补充证据请求全部标记为无法取得，原因：银行确认无法提供", None),
    ],
)
def test_complete_direct_instruction(instruction: str, count: int | None) -> None:
    parsed = direct_instruction(instruction)
    assert parsed is not None and parsed.expected_count == count
    assert parsed.body.action == "evidence_unavailable"


@pytest.mark.parametrize(
    "instruction",
    [
        "不要全部标记为无法取得",
        "如果全部标记为无法取得会怎样",
        "如何全部标记为无法取得？",
        "文件中说：全部标记为无法取得",
        "“全部标记为无法取得”",
        "全部处理",
        "全部标记为无法取得？",
    ],
)
def test_other_text_does_not_authorize(instruction: str) -> None:
    assert direct_instruction(instruction) is None


async def setup_batch():
    s = await waiting_on_cutoff(features=NUMBAT_FEATURES | {MORE_EVIDENCE})
    tasks = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    assert len(tasks) == 2
    for i, task in enumerate(tasks):
        await s.submit.submit(EID, evidence_decision(task, f"D-BATCH-{i}"))
        await wait_idle(s.runner, EID)
    actions = AssistantActions(s.runner.deps, s.submit)
    plan = await actions.propose(EID, "U-1", AssistantPlanIn(action="evidence_unavailable"), "全部标记为无法取得")
    return s, actions, plan


async def test_authorized_batch_rewinds_once_and_replay_returns_same_receipt() -> None:
    s, actions, plan = await setup_batch()
    old = await s.output_store.get(EID, Procedure, "REV-02")
    with patch.object(s.runner, "launch", wraps=s.runner.launch) as launch:
        receipt = await actions.execute(EID, plan.id, "U-1", "The requested records cannot be supplied.")
        await wait_idle(s.runner, EID)
        again = await actions.execute(EID, plan.id, "U-1", "A retry must not replace the original reason.")
        assert launch.call_count == 1
    assert again == receipt and set(receipt.target_ids) == {t.id for t in plan.targets}
    requests = await s.output_store.latest(EID, EvidenceRequest)
    assert all(r.status == "unavailable" for r in requests)
    assert len(await s.output_store.latest(EID, ScopeLimitation)) == 2
    reruns = [r for r in await s.output_store.latest(EID, RerunRequest) if r.id.endswith(plan.id)]
    assert len(reruns) == 1 and set(reruns[0].evidence_request_ids) == set(receipt.target_ids)
    updated = await s.output_store.get(EID, Procedure, "REV-02")
    assert updated.version == old.version + 1
    assert [p.status for p in await actions.plans(EID)] == ["completed"]


async def test_stale_target_rejects_whole_batch_without_touching_other_request() -> None:
    s, actions, plan = await setup_batch()
    changed = await s.output_store.get(EID, EvidenceRequest, plan.targets[0].id)
    await s.output_store.put(
        EID,
        [changed.model_copy(update={"version": changed.version + 1, "description": "Revised request"})],
        actor="U-1",
    )
    with pytest.raises(DecisionRejected, match="事项已变化"):
        await actions.execute(EID, plan.id, "U-1")
    assert all(r.status == "open" for r in await s.output_store.latest(EID, EvidenceRequest))
    assert not await s.output_store.latest(EID, ScopeLimitation)


async def test_cancelled_or_wrong_user_cannot_execute() -> None:
    s, actions, plan = await setup_batch()
    with pytest.raises(LookupError):
        await actions.execute(EID, plan.id, "someone-else")
    await actions.executor.cancel_assistant(EID, plan, actor="U-1")
    with pytest.raises(DecisionRejected, match="已取消"):
        await actions.execute(EID, plan.id, "U-1")
    assert not await s.output_store.latest(EID, ScopeLimitation)


async def test_disconnected_waiter_does_not_cancel_authorized_execution() -> None:
    s, actions, plan = await setup_batch()
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = actions.executor.execute_assistant

    async def blocked(*args, **kwargs):
        entered.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    with patch.object(actions.executor, "execute_assistant", side_effect=blocked):
        waiter = asyncio.create_task(actions.execute(EID, plan.id, "U-1"))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        proceed.set()
        await actions.shutdown()
    await wait_idle(s.runner, EID)
    assert (await actions.plans(EID))[0].status == "completed"
    receipts = [
        e
        for e in await s.runner.deps.trail.list(EID, TrailKind.assistant_action)
        if e.payload.get("action") == "completed"
    ]
    assert len(receipts) == 1


async def test_completion_batch_consumes_all_decisions_with_one_continuation() -> None:
    from backend.contracts.models import Misstatement, ReviewRecord
    from backend.orchestration.runner import GraphRunner, resume_required
    from tests.test_misstatement_updates import at_fin03

    s = await at_fin03()
    actions = AssistantActions(s.runner.deps, s.submit)
    plan = await actions.propose(
        EID, "U-1", AssistantPlanIn(action="confirm_reviews", target_kind="review_task"), "确认所列分类结论"
    )
    assert len(plan.targets) == 2
    with patch.object(s.runner, "launch", wraps=s.runner.launch) as launch:
        receipt = await actions.execute(EID, plan.id, "U-1")
        await wait_idle(s.runner, EID)
        assert launch.call_count == 1
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("communicate",)
    assert len(snapshot.values["review_records"]) == 2
    assert all(m.version == 2 for m in await s.output_store.latest(EID, Misstatement))
    engagement = await s.engagement_store.get(EID)
    assert await resume_required(s.runner.deps, engagement, snapshot) is None
    assert len(await s.output_store.latest(EID, ReviewRecord)) == 2
    assert await actions.execute(EID, plan.id, "U-1") == receipt


async def test_completion_batch_recovers_after_committed_store_before_launch() -> None:
    from backend.orchestration.runner import GraphRunner
    from tests.test_misstatement_updates import at_fin03

    s = await at_fin03()
    actions = AssistantActions(s.runner.deps, s.submit)
    plan = await actions.propose(
        EID, "U-1", AssistantPlanIn(action="confirm_reviews", target_kind="review_task"), "确认所列分类结论"
    )
    # 模拟保存批次后进程中断, Store 与检查点独立持久化.
    with patch.object(s.runner, "launch", side_effect=lambda eid, command: s.runner.release(eid)):
        await actions.execute(EID, plan.id, "U-1")
    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("communicate",)
    assert len(snapshot.values["review_records"]) == 2
    assert s.runner.run_error(EID) is None


async def test_batch_failure_rolls_back_earlier_decisions_and_receipt() -> None:
    from backend.contracts.models import ReviewRecord
    from tests.test_misstatement_updates import at_fin03

    s = await at_fin03()
    actions = AssistantActions(s.runner.deps, s.submit)
    plan = await actions.propose(
        EID, "U-1", AssistantPlanIn(action="confirm_reviews", target_kind="review_task"), "确认所列分类结论"
    )
    original = s.submit._apply_review
    count = 0

    async def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("Injected second-item failure")
        return await original(*args, **kwargs)

    with (
        patch.object(s.submit, "_apply_review", side_effect=fail_second),
        pytest.raises(RuntimeError, match="second-item"),
    ):
        await actions.execute(EID, plan.id, "U-1")
    assert await s.output_store.latest(EID, ReviewRecord) == []
    assert len(await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)) == 2
    assert (await actions.plans(EID))[0].status == "proposed"


@pytest.mark.parametrize("node_error", [False, True])
async def test_completion_evidence_batch_reopens_locally_without_phase_rewind(node_error: bool) -> None:
    from dataclasses import replace

    from backend.contracts.models import Decision, ReviewRecord
    from backend.orchestration.runner import GraphRunner, resume_required
    from tests.test_misstatement_updates import at_fin03

    s = await at_fin03()
    s.runner.deps = replace(s.runner.deps, features=s.runner.deps.features | {MORE_EVIDENCE})
    s.submit.deps = s.runner.deps
    tasks = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    for index, task in enumerate(tasks):
        await s.submit.submit(
            EID,
            ReviewRecord(
                id=f"D-LOCAL-{index}",
                task_id=task.id,
                target_version=task.target_version,
                decision=Decision.more_evidence,
                reviewed_by="U-1",
                decided_at=datetime.now(UTC),
            ),
        )
        await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None
    actions = AssistantActions(s.runner.deps, s.submit)
    plan = await actions.propose(EID, "U-1", AssistantPlanIn(action="evidence_unavailable"), "全部标记为无法取得")
    if node_error:
        snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
        broken = snapshot._replace(tasks=tuple(task._replace(error="Interrupted node") for task in snapshot.tasks))
        with (
            patch.object(s.submit.app, "aget_state", return_value=broken),
            pytest.raises(DecisionRejected, match="请在相关程序"),
        ):
            await actions.execute(EID, plan.id, "U-1")
        assert all(r.status == "open" for r in await s.output_store.latest(EID, EvidenceRequest))
        return
    with patch.object(s.runner, "launch", wraps=s.runner.launch) as launch:
        await actions.execute(EID, plan.id, "U-1")
        await wait_idle(s.runner, EID)
        assert launch.call_count == 0  # 等待仍然有效, 无需以空输入调用图而清除中断.
    pending = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    assert len(pending) == 2
    assert await s.output_store.latest(EID, RerunRequest) == []
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert any(task.name == "misstatement_triage" and task.interrupts for task in snapshot.tasks)
    assert s.runner.run_error(EID) is None
    assert await resume_required(s.runner.deps, await s.engagement_store.get(EID), snapshot) is None
    await s.submit.submit(
        EID,
        ReviewRecord(
            id="D-AFTER-UNAVAILABLE",
            task_id=pending[0].id,
            target_version=pending[0].target_version,
            decision=Decision.confirm,
            reviewed_by="U-1",
            decided_at=datetime.now(UTC),
        ),
    )
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None
    assert len(await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)) == 1


async def test_action_api_is_owned_and_cancelled_plan_cannot_execute() -> None:
    from tests.fakes import api_client

    s = await waiting_on_cutoff()
    owner = (await s.engagement_store.get(EID)).owner_id
    base = f"/api/v1/engagements/{EID}"
    async with api_client(s.runner, owner) as client:
        prepared = await client.post(
            base + "/assistant-actions",
            json={
                "action": "confirm_reviews",
                "target_kind": "review_task",
            },
        )
        assert prepared.status_code == 200, prepared.text
        plan_id = prepared.json()["id"]
        assert (await client.post(base + f"/assistant-actions/{plan_id}/cancel")).status_code == 204
        rejected = await client.post(base + f"/assistant-actions/{plan_id}/execute", json={})
        assert rejected.status_code == 422
        assert (await client.get(base + "/execution/stream?after=unknown")).status_code == 404
    other = (await s.runner.deps.users.get(owner)).model_copy(update={"id": "unrelated-user"})
    s.runner.deps.users.users[other.id] = other
    async with api_client(s.runner, other.id) as client:
        assert (await client.get(base + "/assistant-actions")).status_code == 404
        assert (await client.get(base + "/execution/stream")).status_code == 404


async def test_chat_count_mismatch_only_proposes_then_exact_instruction_executes() -> None:
    from backend.contracts.conversation import ConciergeQuestion
    from tests.chat_fakes import ScriptedChatModel, make_chat, project_state

    s, actions, _ = await setup_batch()
    model = ScriptedChatModel([])
    chat = make_chat(model, s.runner.deps, project_state("zh"))
    chat.actions = actions
    await chat.ask(EID, "U-1", ConciergeQuestion(question="请将当前三项待补资料请求全部标记为无法取得", lang="zh"))
    assert all(r.status == "open" for r in await s.output_store.latest(EID, EvidenceRequest))
    assert all(p.status == "proposed" for p in await actions.plans(EID))
    result = await chat.ask(
        EID, "U-1", ConciergeQuestion(question="请将当前两项待补资料请求全部标记为无法取得", lang="zh")
    )
    await wait_idle(s.runner, EID)
    assert "2" in result.answer
    assert all(r.status == "unavailable" for r in await s.output_store.latest(EID, EvidenceRequest))
    assert model.requests == []
    await chat.shutdown()


async def test_repeated_and_concurrent_proposals_reuse_pending_scope() -> None:
    s, actions, plan = await setup_batch()
    body = AssistantPlanIn(action="evidence_unavailable", target_ids=[t.id for t in reversed(plan.targets)])
    repeated = await asyncio.gather(*(actions.propose(EID, "U-1", body, "再次点击") for _ in range(3)))
    assert {p.id for p in repeated} == {plan.id}
    assert len(await actions.plans(EID)) == 1
    changed = await s.output_store.get(EID, EvidenceRequest, plan.targets[0].id)
    await s.output_store.put(EID, [changed.model_copy(update={"version": changed.version + 1})], actor="U-1")
    newer = await actions.propose(EID, "U-1", body, "事项更新后重新生成")
    assert newer.id != plan.id
    await actions.cancel(EID, newer.id, "U-1")
    regenerated = await actions.propose(EID, "U-1", body, "取消后重新生成")
    assert regenerated.id != newer.id


async def test_cancel_closes_legacy_duplicate_proposals() -> None:
    from backend.contracts.models import TrailEntry

    s, actions, plan = await setup_batch()
    duplicate = plan.model_copy(update={"id": "AP-LEGACY-DUPLICATE"})
    await s.runner.deps.trail.record(
        TrailEntry(
            engagement_id=EID,
            actor="U-1",
            kind=TrailKind.assistant_action,
            summary="旧版重复方案",
            idempotency_key=f"{duplicate.id}:proposed",
            payload={"action": "proposed", "plan": duplicate.model_dump(mode="json")},
        )
    )
    await actions.cancel(EID, plan.id, "U-1")
    assert [p.status for p in await actions.plans(EID)] == ["cancelled", "cancelled"]
