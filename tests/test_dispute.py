"""争议仲裁 (R9): 独立复核与 A6 不一致时建 dispute 任务, 只能仲裁; 三种裁决的效果, 附错报草稿, 字段规则.

用 Numbat 演示 (真实 Agent 与 mock 输出): A6 的固定输出照常, 复核模型换成 tests/fixtures/llm_variants 下的变体,
BA-01 (L3, 建议关闭) 被判为不一致. Xero 没有函证回函, 跑不出这条判断.
"""

from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AuditException,
    CriticVerdict,
    Decision,
    DisputeResolution,
    ExceptionDisposition,
    Judgment,
    Misstatement,
    MisstatementStatus,
    NeitherAction,
    Procedure,
    ProcedureStatus,
    ProposedAction,
    ReviewStatus,
    ReviewTask,
    TaskKind,
    TaskStatus,
    TrailKind,
)
from backend.orchestration.decisions import review_effects
from backend.services.judgment_engine import critic_chain_ok
from tests.fakes import ENGAGEMENT, NUMBAT_CUTOFF_DRAFT, api_client, wait_idle
from tests.test_decisions import Started, assert_rejected
from tests.test_e2e_mock import start_demo
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
VARIANTS = Path(__file__).resolve().parent / "fixtures" / "llm_variants"
TASK = "T-J-CSH-02-1-BA-01-v1"
JID = "J-CSH-02-1-BA-01"


async def disputed(variant: str = "dispute") -> Started:
    """Numbat 跑到复核阶段, 复核模型取变体: BA-01 判为不一致, 建争议任务."""
    s = await start_demo(review_llm=MockLLMClient(VARIANTS / variant))
    await wait_idle(s.runner, EID)
    return s


def resolve(resolution: DisputeResolution, **fields: Any) -> Any:
    return record(
        TASK, Decision.dispute_resolution, id="D-ARB", dispute_resolution=resolution, reason="仲裁意见", **fields
    )


async def submitted(s: Started, decision: Any) -> None:
    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)


async def latest(s: Started, kind: type[Any], oid: str) -> Any:
    return await s.output_store.get(EID, kind, oid)


async def test_inconsistent_creates_dispute_task_only_resolution_allowed() -> None:
    s = await disputed()
    j = await latest(s, Judgment, JID)
    assert (j.critic_verdict, j.critic_reason_type) == (CriticVerdict.inconsistent, "different_conclusion")
    assert j.confidence == 0.5625  # 不一致不计复核分项, 置信度同只有 A6 时
    assert critic_chain_ok(j.critic_reasoning_chain) and j.critic_conclusion is not None
    task = await latest(s, ReviewTask, TASK)
    assert (task.kind, task.status) == (TaskKind.dispute, TaskStatus.pending)
    assert (await latest(s, Procedure, "CSH-02")).status is ProcedureStatus.awaiting_human  # 未裁决的争议不算已处置

    async with api_client(s.runner) as client:
        views = (await client.get(f"/api/v1/engagements/{EID}/review-tasks?status=pending")).json()
    [view] = [v for v in views if v["task"]["id"] == TASK]
    assert (view["allowed_decisions"], view["tier"]) == (["dispute_resolution"], "full_review")
    assert view["allowed_resolutions"] == ["a6_upheld", "a7_upheld", "neither"]
    assert view["target"]["critic_verdict"] == "inconsistent"

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, record(TASK, Decision.confirm, id="D-CONFIRM"))
    assert e.value.code == "DECISION_NOT_ALLOWED"  # 确认会绕过仲裁


async def test_a6_upheld_confirms_original() -> None:
    s = await disputed()
    await submitted(s, resolve(DisputeResolution.a6_upheld))
    j = await latest(s, Judgment, JID)
    assert (j.version, j.review_status, j.superseded_by) == (2, ReviewStatus.confirmed, None)
    exceptions = [x for x in await s.output_store.latest(EID, AuditException) if x.attributed_by is not None]
    assert {x.attributed_by for x in exceptions} == {JID}
    assert (await latest(s, ReviewTask, TASK)).status is TaskStatus.decided
    assert (await latest(s, Procedure, "CSH-02")).status is ProcedureStatus.completed
    trail = [e for e in s.runner.deps.trail.entries if e.kind is TrailKind.decision]  # type: ignore[attr-defined]
    assert trail[-1].payload["dispute_resolution"] == "a6_upheld"


async def test_a7_upheld_builds_new_version_from_critic_and_records_misstatement() -> None:
    s = await disputed()
    before = await latest(s, Judgment, JID)
    await submitted(s, resolve(DisputeResolution.a7_upheld, misstatement_draft=NUMBAT_CUTOFF_DRAFT))

    old = await latest(s, Judgment, JID)
    assert (old.version, old.review_status, old.superseded_by) == (2, ReviewStatus.overridden, f"{JID}-H1")
    new = await latest(s, Judgment, f"{JID}-H1")
    assert (new.conclusion, new.reasoning_chain) == (before.critic_conclusion, before.critic_reasoning_chain)
    assert (new.review_status, new.performed_by, new.trail_ref) == (
        ReviewStatus.confirmed,
        "U-1",
        before.critic_trail_ref,
    )
    assert new.critic_verdict is CriticVerdict.not_applicable and new.exception_ids == before.exception_ids
    assert {x.attributed_by for x in await s.output_store.latest(EID, AuditException) if x.id in new.exception_ids} == {
        new.id
    }
    misstatement = await latest(s, Misstatement, "M-D-ARB")
    assert (misstatement.judgment_id, misstatement.amount_cents) == (new.id, NUMBAT_CUTOFF_DRAFT.amount_cents)


@pytest.mark.parametrize(
    ("lifecycle", "status"), [(False, MisstatementStatus.uncorrected), (True, MisstatementStatus.identified)]
)
def test_attached_misstatement_status_follows_the_feature(lifecycle: bool, status: MisstatementStatus) -> None:
    """R156 验收 5 (C5f): 仲裁附错报草稿时建的错报, 初始状态同 misstatement 决定, 按功能 misstatement (经
    review_effects, 纯函数): 关闭为 uncorrected (同上一用例实际走的), 打开为 identified; 来源为采用的判断."""
    target = Judgment.model_construct(id=JID, version=1, exception_ids=[], proposed_action=ProposedAction.misstatement)
    task = ReviewTask.model_construct(id=TASK, kind=TaskKind.dispute)
    decision = resolve(DisputeResolution.a6_upheld, misstatement_draft=NUMBAT_CUTOFF_DRAFT)
    effects = review_effects(decision, task, target, [], set(), lifecycle=lifecycle)
    [misstatement] = [e for e in effects if isinstance(e, Misstatement)]
    assert misstatement == Misstatement(
        id="M-D-ARB",
        judgment_id=JID,
        exception_id=None,
        review_record_id="D-ARB",
        status=status,
        **NUMBAT_CUTOFF_DRAFT.model_dump(),
    )


async def test_attached_draft_account_code_outside_the_chart_is_rejected() -> None:
    """R156 (第 1 轮审核 R156-04): 附错报草稿的仲裁同样查科目代码: 草稿的 account_code 不在本项目科目表 (start_demo
    的科目表为空) → INVALID_VALUE, 什么都不写."""
    s = await disputed()
    draft = NUMBAT_CUTOFF_DRAFT.model_copy(update={"account_code": "999"})
    decision = resolve(DisputeResolution.a7_upheld, misstatement_draft=draft)
    await assert_rejected(s, decision, "INVALID_VALUE", "科目代码 999 不在本项目科目表中")


async def test_neither_needs_human_conclusion() -> None:
    s = await disputed()
    for fields, missing in (
        ({}, "neither_action, override_conclusion"),
        ({"neither_action": NeitherAction.human_conclusion}, "override_conclusion"),
    ):
        with pytest.raises(DecisionRejected) as e:
            await s.submit.submit(EID, resolve(DisputeResolution.neither, **fields))
        assert (e.value.code, e.value.message) == ("MISSING_FIELD", missing)
    await submitted(
        s,
        resolve(
            DisputeResolution.neither, neither_action=NeitherAction.human_conclusion, override_conclusion="人的结论"
        ),
    )
    human = await latest(s, Judgment, f"{JID}-H1")
    assert (human.conclusion, human.reasoning_chain) == ("人的结论", [])
    assert (await latest(s, Judgment, JID)).review_status is ReviewStatus.overridden


async def test_resolution_field_rules() -> None:
    s = await disputed()
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, resolve(DisputeResolution.a6_upheld, override_conclusion="x"))
    assert e.value.code == "FIELD_NOT_ALLOWED"
    with pytest.raises(DecisionRejected) as e:
        no_reason = record(
            TASK, Decision.dispute_resolution, id="D-NO-REASON", dispute_resolution=DisputeResolution.a6_upheld
        )
        await s.submit.submit(EID, no_reason.model_copy(update={"reason": None}))
    assert (e.value.code, e.value.message) == ("MISSING_FIELD", "reason")
    other = "T-J-REV-03-1-CON-001-v1"
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, record(other, id="D-OTHER", dispute_resolution=DisputeResolution.a6_upheld))
    assert e.value.code == "FIELD_NOT_ALLOWED"  # 别的决定不能带仲裁字段


async def test_invalid_critic_chain_rules_out_a7_upheld() -> None:
    """独立复核的推理链没通过 8.6 校验 (inference 没引用前序步骤): 列表里没有 a7_upheld, 选了拒绝."""
    s = await disputed("dispute_bad_chain")
    j = await latest(s, Judgment, JID)
    assert j.critic_verdict is CriticVerdict.inconsistent and not critic_chain_ok(j.critic_reasoning_chain)
    async with api_client(s.runner) as client:
        views = (await client.get(f"/api/v1/engagements/{EID}/review-tasks?status=pending")).json()
    assert [v["allowed_resolutions"] for v in views if v["task"]["id"] == TASK] == [["a6_upheld", "neither"]]
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, resolve(DisputeResolution.a7_upheld))
    assert e.value.code == "INVALID_VALUE"
    assert (await latest(s, ReviewTask, TASK)).status is TaskStatus.pending


async def test_dispute_trail_records_review_model() -> None:
    s = await disputed()
    calls = [e for e in s.runner.deps.trail.entries if e.kind is TrailKind.llm_call and e.procedure_id == "GEN-09"]  # type: ignore[attr-defined]
    assert [(e.actor, e.model, e.payload["fixture_key"]) for e in calls] == [
        ("A7", "mock", "BA-01-r1"),
        ("A7", "mock", "BA-01-r2"),
    ]
    assert {x.disposition for x in await s.output_store.latest(EID, AuditException) if "BA-01" in x.source_ids} == {
        ExceptionDisposition.open
    }
