"""判断呈递 (GEN-11, 需求 R152): 审计师打开一条复核任务即为一次呈递.

按任务类型组装呈递视图 (争议任务两方并排, 两方证据的并集, 失败原因), 每次呈递写一行 presentation 留痕 (复核停留时长的
起点, 提交复核决定时按它算, 见 dwell_seconds). 不改任务, 不写 Store, 不取项目锁, 不调大模型. 组装是纯函数; 任务,
目标, 金额与档位由调用方按复核队列的取法读好 (api/routes/reviews.task_parts).
规则见 开发接口文档.md 第 10 节 "判断呈递".
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast

from backend.contracts.models import (
    AuditException,
    Decision,
    DisputeResolution,
    EvidencePointer,
    Judgment,
    ProcedureRun,
    ReviewTask,
    ReviewTier,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import AuditTrail
from backend.contracts.views import (
    EvidenceOrigin,
    PresentedEvidence,
    PresentedFailure,
    PresentedSide,
    SideRole,
    TaskPresentation,
)
from backend.services.judgment_engine import evidence_key

PROCEDURE_ID = "GEN-11"  # 判断呈递与决定收集 (规格书 GEN-11); 不建程序文件, 只用作留痕的程序编号


def generator_side(judgment: Judgment) -> PresentedSide:
    """生成方: 判断的结论, 推理链, 反方论证, 置信度, 建议动作与错报草稿."""
    return PresentedSide(
        conclusion=judgment.conclusion,
        reasoning_chain=judgment.reasoning_chain,
        proposed_action=judgment.proposed_action,
        misstatement_draft=judgment.misstatement_draft,
        counter_argument=judgment.counter_argument,
        confidence=judgment.confidence,
    )


def reviewer_side(judgment: Judgment) -> PresentedSide:
    """复核方 (A7): 第一轮的独立结论与推理链, 建议动作与错报草稿 (collab 之前的判断为空), 不一致的理由类型与理由."""
    return PresentedSide(
        conclusion=judgment.critic_conclusion,
        reasoning_chain=judgment.critic_reasoning_chain,
        proposed_action=judgment.critic_proposed_action,
        misstatement_draft=judgment.critic_misstatement_draft,
        reason_type=judgment.critic_reason_type,
        reason=judgment.critic_reason,
    )


def sides_of(task: ReviewTask, target: Judgment | AuditException | ProcedureRun) -> dict[SideRole, PresentedSide]:
    """目标是判断时给生成方; 只有 dispute 任务另给复核方 (两方并排只用于要裁决的分歧; 其余任务 A7 的结论照旧在
    target 的 critic_ 字段里). 目标是例外或执行记录时为空."""
    if not isinstance(target, Judgment):
        return {}
    sides: dict[SideRole, PresentedSide] = {"generator": generator_side(target)}
    if task.kind is TaskKind.dispute:
        sides["reviewer"] = reviewer_side(target)
    return sides


def generator_pointers(judgment: Judgment) -> list[EvidencePointer]:
    """生成方引用的证据: 结论证据, 再推理链各步的, 按出现顺序 (含重复)."""
    return [*judgment.evidence, *(p for step in judgment.reasoning_chain for p in step.evidence)]


def reviewer_pointers(judgment: Judgment) -> list[EvidencePointer]:
    """复核方引用的证据: 第一轮推理链各步的, 按出现顺序 (含重复)."""
    return [p for step in judgment.critic_reasoning_chain for p in step.evidence]


def evidence_union(
    generator: Sequence[EvidencePointer], reviewer: Sequence[EvidencePointer]
) -> list[PresentedEvidence]:
    """两方证据的并集: 按 evidence_key 去重 (文件按来源与页, 同一页的不同 locator 算一项; 客户画像按来源与字段; 其余
    按来源), 保留先出现的指针; 生成方的在前, 只有复核方引用的接在后面; origin 为两方都引用 (both) 或只有一方. 纯函数.
    例: BA-01 两方各 6 个指针 (回函第 1 页两个 locator) → 5 项, 都为 both."""
    first: dict[tuple[str, str, int | str | None], EvidencePointer] = {}
    roles: dict[tuple[str, str, int | str | None], set[SideRole]] = {}
    sources: tuple[tuple[SideRole, Sequence[EvidencePointer]], ...] = (("generator", generator), ("reviewer", reviewer))
    for role, pointers in sources:
        for pointer in pointers:
            key = evidence_key(pointer)
            first.setdefault(key, pointer)
            roles.setdefault(key, set()).add(role)
    return [
        PresentedEvidence(
            pointer=pointer, origin="both" if len(roles[key]) == 2 else cast(EvidenceOrigin, next(iter(roles[key])))
        )
        for key, pointer in first.items()
    ]


def last_llm_call(llm_calls: Sequence[TrailEntry], task: ReviewTask) -> TrailEntry | None:
    """带对象的失败任务对应的最后一次大模型调用: 本次执行 (run_id), 本程序, fixture_key 为任务的 subject_id 的 llm_call
    留痕中最后一行 (按写入顺序). 同一执行里别的对象与独立复核 (GEN-09, 键为 {对象}-r1) 的行不算."""
    return next(
        (
            entry
            for entry in reversed(llm_calls)
            if entry.kind is TrailKind.llm_call
            and entry.run_id == task.run_id
            and entry.procedure_id == task.procedure_id
            and entry.payload.get("fixture_key") == task.subject_id
        ),
        None,
    )


def failure_of(
    task: ReviewTask, target: Judgment | AuditException | ProcedureRun, llm_calls: Sequence[TrailEntry] = ()
) -> PresentedFailure | None:
    """失败类与无归因类任务的失败原因 (规格书 UC-04 第 2 步: 无归因类给规则结果, 失败类给失败原因); 其余任务为空.

    - unattributed (目标为例外): 规则编号, 金额与明细.
    - failed, 不带 subject_id (整次执行失败, A1 建的): 执行记录的 failure_reason.
    - failed, 带 subject_id (个别对象的大模型输出没通过校验, 执行记录为成功终态): 取 last_llm_call 那行的
      level_failed, errors 与 attempt; 找不到时只有 subject_id.
    llm_calls: 项目的 llm_call 留痕, 按写入顺序; 只有带 subject_id 的 failed 任务用到. 纯函数.
    """
    if task.kind is TaskKind.unattributed and isinstance(target, AuditException):
        return PresentedFailure(
            source="exception", rule_id=target.rule_id, amount_cents=target.amount_cents, detail=target.detail
        )
    if task.kind is not TaskKind.failed:
        return None
    if task.subject_id is None:
        return PresentedFailure(
            source="run", reason=target.failure_reason if isinstance(target, ProcedureRun) else None
        )
    call = last_llm_call(llm_calls, task)
    if call is None:
        return PresentedFailure(source="llm_call", subject_id=task.subject_id)
    payload = call.payload
    return PresentedFailure.model_validate(
        {
            "source": "llm_call",
            "subject_id": task.subject_id,
            "level_failed": payload.get("level_failed"),
            "errors": list(payload.get("errors") or []),
            "attempts": payload.get("attempt"),
            "trail_id": call.id,
        }
    )


def assemble(
    task: ReviewTask,
    target: Judgment | AuditException | ProcedureRun,
    *,
    presentation_id: str,
    procedure_name: str,
    allowed_decisions: Sequence[Decision],
    allowed_resolutions: Sequence[DisputeResolution],
    amount_cents: int | None,
    tier: ReviewTier,
    deferred: bool,
    llm_calls: Sequence[TrailEntry] = (),
) -> TaskPresentation:
    """组装呈递视图. target 为任务绑定的那一版; 程序名, 允许的决定, 仲裁结果, 金额与档位由调用方按复核队列的取法给出.
    evidence 为 sides 中各方证据的并集 (没有 sides 为空). 纯函数."""
    sides = sides_of(task, target)
    evidence: list[PresentedEvidence] = []
    if isinstance(target, Judgment):
        generator = generator_pointers(target) if "generator" in sides else []
        reviewer = reviewer_pointers(target) if "reviewer" in sides else []
        evidence = evidence_union(generator, reviewer)
    return TaskPresentation(
        presentation_id=presentation_id,
        task=task,
        procedure_name=procedure_name,
        target=target,
        allowed_decisions=list(allowed_decisions),
        allowed_resolutions=list(allowed_resolutions),
        amount_cents=amount_cents,
        tier=tier,
        deferred=deferred,
        sides=sides,
        evidence=evidence,
        failure=failure_of(task, target, llm_calls),
    )


def presentation_entry(eid: str, task: ReviewTask, actor: str, now: datetime) -> TrailEntry:
    """一次呈递的留痕: kind presentation, actor 为打开任务的用户, 程序 GEN-11, run_id 为任务的, payload 记任务与目标的
    版本 (target_version 为任务绑定的版本), created_at 为呈递时间. 不带 idempotency_key: 每次打开都记. 纯函数."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.presentation,
        actor=actor,
        procedure_id=PROCEDURE_ID,
        run_id=task.run_id,
        summary=f"呈递复核任务 {task.id}",
        payload={
            "task_id": task.id,
            "task_version": task.version,
            "target_type": task.target_type.value,
            "target_id": task.target_id,
            "target_version": task.target_version,
        },
        created_at=now,
    )


async def record_presentation(trail: AuditTrail, eid: str, task: ReviewTask, actor: str, now: datetime) -> str:
    """写一次呈递的留痕, 返回留痕 ID (即 presentation_id). now 为呈递时间, 由调用方给 (接口取当前时间, 测试可注入)."""
    return await trail.record(presentation_entry(eid, task, actor, now))


def dwell_seconds(
    entries: Sequence[TrailEntry], task_id: str, reviewer: str, decided_at: datetime
) -> tuple[int | None, str | None]:
    """复核停留时长 (需求 R153): entries 中 kind presentation, payload.task_id 为 task_id, actor 为 reviewer, 呈递时间
    不晚于 decided_at 的留痕里取最近一条, 返回 (decided_at 减呈递时间的秒数, 向下取整; 该留痕 ID); 没有时为
    (None, None). 别人的呈递与决定之后的呈递不算. 纯函数."""
    shown = [
        e
        for e in entries
        if e.kind is TrailKind.presentation
        and e.payload.get("task_id") == task_id
        and e.actor == reviewer
        and e.created_at is not None
        and e.created_at <= decided_at
    ]
    if not shown:
        return None, None
    last = sorted(shown, key=lambda e: e.created_at or decided_at)[-1]  # 稳定排序: 同一时刻的取后写的
    assert last.created_at is not None
    return (decided_at - last.created_at) // timedelta(seconds=1), last.id
