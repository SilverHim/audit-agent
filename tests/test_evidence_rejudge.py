"""需求 R369 (Issue #65 验收 4): 补充证据到达后只重判该对象.

用复核决定的测试台 (tests.test_review_decisions.waiting_on_cutoff) 把图停在 P2 等复核: 对 SI-2026-0612 的判断要求补充
证据 → 证据上传登记 → 自动重判只重判这一个对象, 新判断 (run_no + 1) 与新复核任务, 旧判断标 superseded; 另一对象
SI-2026-0618 的判断不动; 新任务照常可以决定. 功能关闭时不允许要求补充证据; 同一请求重判两次不重复.
"""

from dataclasses import replace
from typing import Any

import pytest

from backend.agents.judging import in_scope
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    Decision,
    DocumentType,
    EvidenceRequest,
    Judgment,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    Reliability,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    TaskStatus,
)
from backend.contracts.state import fill_defaults
from backend.services.evidence_followup import link_uploaded_documents
from backend.services.features import MORE_EVIDENCE
from backend.services.staleness import current_results
from tests.fakes import ENGAGEMENT as FAKE_ENGAGEMENT
from tests.fakes import NOW, NUMBAT_FEATURES, FakeRegistry, FakeTrail, make_ctx, make_deps, wait_idle
from tests.test_review_decisions import EID, record, waiting_on_cutoff

shared_loop = pytest.mark.asyncio(loop_scope="module")


def evidence_decision(task: ReviewTask, decision_id: str = "D-MORE-EVIDENCE") -> ReviewRecord:
    """要求补充证据的决定, 绑定任务当前版本."""
    return ReviewRecord(
        id=decision_id,
        task_id=task.id,
        target_version=task.target_version,
        decision=Decision.more_evidence,
        reason="Need the bank statement.",
        reviewed_by="U-1",
        decided_at=NOW,
    )


async def pending_task(s) -> ReviewTask:
    tasks = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    [task] = [t for t in tasks if t.target_id == "J-REV-02-1-SI-2026-0612"]
    return task


async def upload_and_link(s, request: EvidenceRequest) -> EvidenceRequest:
    document = await s.runner.deps.evidence_store.save(
        EID,
        "bank-statement.pdf",
        b"%PDF-1.4 synthetic statement",
        type=DocumentType.bank_statement,
        reliability=Reliability.external_copy,
    )
    await link_uploaded_documents(s.runner.deps, EID, request.id, [document], actor="U-1")
    received = await s.output_store.get(EID, EvidenceRequest, request.id)
    assert received is not None and received.status == "received"
    return received


@pytest.mark.parametrize("late", [False, True])
async def test_evidence_upload_rejudges_the_subject_and_supersedes_the_old_judgment(late: bool) -> None:
    """要求补充证据 → 上传证据 → 自动重判: 新判断 J-REV-02-2-SI-2026-0612 与新任务, 旧判断 v2 superseded 且
    superseded_by 指向新判断, 当前结果里只剩新判断; 请求记重判后的判断 ID; 新任务照常可决定.
    (测试台 REV-02 是无状态脚本 Agent, 不按 rerun_subject_ids 过滤, 故这里不对"只重判一个对象"下断言, 见
    test_in_scope_keeps_only_rerun_subjects.)"""
    s = await waiting_on_cutoff(features=NUMBAT_FEATURES | {MORE_EVIDENCE})
    task = await pending_task(s)
    judgment = await s.output_store.get(EID, Judgment, task.target_id)
    assert judgment is not None

    await s.submit.submit(EID, evidence_decision(task))
    await wait_idle(s.runner, EID)
    [request] = await s.output_store.latest(EID, EvidenceRequest)
    stored_task = await s.output_store.get(EID, ReviewTask, task.id)
    assert stored_task is not None and stored_task.status is TaskStatus.awaiting_evidence
    assert request.status == "open" and request.subject_id == "SI-2026-0612"

    if late:
        for other in await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending):
            await s.submit.submit(EID, evidence_decision(other, "D-OTHER-EVIDENCE"))
            await wait_idle(s.runner, EID)
        assert await s.next_nodes() == ("gate_fin",)
    await upload_and_link(s, request)
    assert (await s.output_store.get(EID, ReviewTask, task.id)).status is TaskStatus.pending

    updated = await s.submit.rejudge_after_evidence(EID, request.id, actor="U-1")
    await wait_idle(s.runner, EID)

    assert updated.rejudged_judgment_id == "J-REV-02-2-SI-2026-0612"
    old = await s.output_store.get(EID, Judgment, judgment.id)
    new = await s.output_store.get(EID, Judgment, "J-REV-02-2-SI-2026-0612")
    assert old is not None and new is not None
    assert (old.version, old.review_status, old.superseded_by) == (
        judgment.version + 1,
        ReviewStatus.superseded,
        new.id,
    )
    assert new.subject_id == "SI-2026-0612" and new.run_id == "RUN-REV-02-2"
    # 当前结果 (FIN-04, FIN-01, 下游取数) 只认新判断, 被取代的旧判断不再计入
    snapshot = await s.runner.app.aget_state({"configurable": {"thread_id": EID}})
    current = current_results(fill_defaults(snapshot.values))
    assert [j.id for j in current.judgments if j.subject_id == "SI-2026-0612"] == [new.id]
    tasks = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    [new_task] = [t for t in tasks if t.target_id == new.id]
    assert await s.next_nodes() == ("await_events",)
    procedure = await s.output_store.get(EID, Procedure, "REV-02")
    assert procedure is not None and procedure.status is ProcedureStatus.awaiting_human

    await s.submit.submit(
        EID,
        record(
            new_task.id,
            Decision.override,
            id="D-NEW",
            target_version=new_task.target_version,
            override_conclusion="With the bank statement, the conversion entry is supported.",
        ),
    )
    await wait_idle(s.runner, EID)
    decided = await s.output_store.get(EID, ReviewTask, new_task.id)
    assert decided is not None and decided.status is TaskStatus.decided


@pytest.mark.parametrize("recovery", ["startup", "after_intake"])
async def test_rejudge_twice_does_not_duplicate(recovery: str) -> None:
    """同一请求重判两次: 第二次原样返回, 不再重跑 (判断只有一份新的)."""
    s = await waiting_on_cutoff(features=NUMBAT_FEATURES | {MORE_EVIDENCE})
    task = await pending_task(s)
    await s.submit.submit(EID, evidence_decision(task))
    await wait_idle(s.runner, EID)
    [request] = await s.output_store.latest(EID, EvidenceRequest)
    await upload_and_link(s, request)
    # 请求持久化后重启也会补做, 不依赖上传 HTTP 请求的有限重试窗口.
    document_id = (await s.output_store.get(EID, EvidenceRequest, request.id)).document_ids[0]
    await s.runner.deps.evidence_store.set_status(EID, document_id, "classified")
    if recovery == "startup":
        await s.runner.recover()
    else:
        s.runner.acquire(EID)
        await s.runner.proceed(EID)
    await wait_idle(s.runner, EID)
    first = await s.output_store.get(EID, EvidenceRequest, request.id)
    assert first and first.rejudged_judgment_id
    second = await s.submit.rejudge_after_evidence(EID, request.id, actor="U-1")
    await wait_idle(s.runner, EID)
    assert second.rejudged_judgment_id == first.rejudged_judgment_id
    executions = [r for r in await s.output_store.latest(EID, ProcedureRun) if r.procedure_id == "REV-02"]
    assert len(executions) == 2  # 第 1 次执行与只重判一个对象的第 2 次


async def test_more_evidence_decision_requires_the_feature() -> None:
    """功能 more_evidence 关闭时, 对普通复核任务提交 more_evidence 被拒 (DECISION_NOT_ALLOWED)."""
    s = await waiting_on_cutoff()
    task = await pending_task(s)
    with pytest.raises(DecisionRejected) as caught:
        await s.submit.submit(EID, evidence_decision(task))
    assert caught.value.code == "DECISION_NOT_ALLOWED"
    assert await s.output_store.latest(EID, EvidenceRequest) == []


def test_in_scope_keeps_only_rerun_subjects() -> None:
    """判断公共流程只重判 rerun_subject_ids 中的对象 (单对象重判的过滤层); 为空时整条重判."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    ctx = make_ctx(deps, "REV-02", FAKE_ENGAGEMENT)
    only = replace(ctx, procedure=ctx.procedure.model_copy(update={"rerun_subject_ids": ["SI-2026-0612"]}))
    assert in_scope(only, "SI-2026-0612") is True
    assert in_scope(only, "SI-2026-0618") is False
    assert in_scope(ctx, "SI-2026-0618") is True


@pytest.mark.parametrize("received", [False, True])
async def test_recover_evidence_decision_after_checkpoint_write_failed(
    monkeypatch: pytest.MonkeyPatch, received: bool
) -> None:
    """Store 已提交而检查点写入失败后, 恢复重判的旧判断或无法取得的待决任务, 不丢掉补证状态."""
    s = await waiting_on_cutoff(features=NUMBAT_FEATURES | {MORE_EVIDENCE})
    task = await pending_task(s)
    await s.submit.submit(EID, evidence_decision(task))
    await wait_idle(s.runner, EID)
    [request] = await s.output_store.latest(EID, EvidenceRequest)
    if received:
        await upload_and_link(s, request)

    async def fail_checkpoint(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("checkpoint interrupted")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(s.runner.app, "aupdate_state", fail_checkpoint)
        with pytest.raises(RuntimeError, match="checkpoint interrupted"):
            if received:
                await s.submit.rejudge_after_evidence(EID, request.id, actor="U-1")
            else:
                await s.submit.mark_evidence_unavailable(EID, request.id, None, actor="U-1")
    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None
    state = fill_defaults((await s.runner.app.aget_state(s.runner.config(EID))).values)
    if received:
        old = next(j for j in state["judgments"] if j.id == request.judgment_id)
        assert old.superseded_by and old.review_status is ReviewStatus.superseded
    else:
        restored = next(t for t in state["review_tasks"] if t.id == task.id)
        assert restored.status is TaskStatus.pending
