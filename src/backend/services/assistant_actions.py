"""助理操作方案的持久化与后台执行. 模型只能提案, 授权来自当前用户的指令或执行按钮."""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from backend.agents.base import AgentDeps
from backend.contracts.assistant_actions import AssistantPlan, AssistantPlanIn, AssistantReceipt, AssistantTarget
from backend.contracts.data import DataRequest
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    Decision,
    EvidenceRequest,
    Judgment,
    Model,
    Procedure,
    ProcedureRun,
    ProposedAction,
    RerunRequest,
    ReviewTask,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
)
from backend.services.deferrals import latest_entries
from backend.services.staleness import rerunnable, stale_procedures, superseded_runs
from backend.services.trail_pages import all_of_kind


class ActionExecutor(Protocol):
    async def execute_assistant(self, eid: str, plan: AssistantPlan, *, actor: str) -> AssistantReceipt: ...

    async def cancel_assistant(self, eid: str, plan: AssistantPlan, *, actor: str) -> None: ...


def fingerprint(value: Model) -> str:
    return hashlib.sha256(value.model_dump_json().encode()).hexdigest()


async def current_targets(deps: AgentDeps, eid: str, body: AssistantPlanIn) -> list[AssistantTarget]:
    if body.action == "evidence_unavailable" and body.target_kind != "evidence_request":
        raise DecisionRejected("INVALID_VALUE", "无法取得仅适用于补充证据请求, 不包括人工复核及资料暂缓事项")
    if body.action in {"defer", "resume"} and body.target_kind == "evidence_request":
        raise DecisionRejected("INVALID_VALUE", "补证请求使用上传或无法取得操作; 暂缓适用于资料请求和复核任务")
    if body.action in {"confirm_reviews", "recognize_misstatements"} and body.target_kind != "review_task":
        raise DecisionRejected("INVALID_VALUE", "复核操作仅适用于复核任务")
    if (body.action == "rerun_stale") != (body.target_kind == "procedure"):
        raise DecisionRejected("INVALID_VALUE", "重新执行仅适用于过期程序")
    rows: list[EvidenceRequest | ReviewTask | DataRequest | Procedure]
    judgments = {j.id: j for j in await deps.output_store.latest(eid, Judgment)}
    if body.target_kind == "evidence_request":
        rows = [r for r in await deps.output_store.latest(eid, EvidenceRequest) if r.status == "open"]
    elif body.target_kind == "review_task":
        rows = [t for t in await deps.output_store.latest(eid, ReviewTask) if t.status is TaskStatus.pending]
    elif body.target_kind == "procedure":
        procedures = await deps.output_store.latest(eid, Procedure)
        stale = stale_procedures(
            procedures,
            await deps.output_store.latest(eid, ProcedureRun),
            await deps.data.changes(eid),
            deps.library,
            superseded_runs(await deps.output_store.latest(eid, RerunRequest)),
        )
        allowed = set(rerunnable(procedures, stale))
        rows = [p for p in procedures if p.id in allowed]
    else:
        rows = [r for r in await deps.data.requests(eid) if r.status == "open"]
    deferrals = latest_entries(await all_of_kind(deps.trail, eid, TrailKind.deferral))
    selected = set(body.target_ids)
    targets = []
    for row in rows:
        rid: str = row.id
        if selected and rid not in selected:
            continue
        deferred = deferrals.get((body.target_kind, rid))
        is_deferred = deferred is not None and deferred.payload.get("deferred") is True
        if body.action == "resume" and not is_deferred:
            continue
        if body.action == "defer" and is_deferred:
            continue
        judgment = judgments.get(row.target_id) if isinstance(row, ReviewTask) else None
        if body.action in {"confirm_reviews", "recognize_misstatements"}:
            if not isinstance(row, ReviewTask) or row.kind is not TaskKind.normal or judgment is None:
                continue
            decision = Decision.confirm if body.action == "confirm_reviews" else Decision.misstatement
            if decision not in deps.queue.allowed(row, judgment):
                continue
            if body.action == "confirm_reviews" and judgment.proposed_action is not ProposedAction.accept:
                continue
            if body.action == "recognize_misstatements" and (
                judgment.proposed_action is not ProposedAction.misstatement or judgment.misstatement_draft is None
            ):
                continue
        digest = fingerprint(row)
        if isinstance(row, EvidenceRequest):
            task = await deps.output_store.get(eid, ReviewTask, row.task_id)
            original = judgments.get(row.judgment_id)
            if task is None or task.status is not TaskStatus.awaiting_evidence or original is None:
                continue
            digest += f":{fingerprint(task)}:{fingerprint(original)}"
        if judgment is not None and body.action in {"confirm_reviews", "recognize_misstatements"}:
            digest += f":{fingerprint(judgment)}"
        if body.action in {"defer", "resume"}:
            digest += f":{deferred.id if deferred else ''}"
        targets.append(
            AssistantTarget(
                kind=body.target_kind,
                id=rid,
                version=getattr(row, "version", None),
                fingerprint=digest,
                label=getattr(row, "description", None) or getattr(row, "title", None) or rid,
                procedure_id=getattr(row, "procedure_id", None),
                conclusion=judgment.conclusion if judgment else None,
                suggestion=judgment.proposed_action.value if judgment else None,
            )
        )
    if selected and selected != {t.id for t in targets}:
        raise DecisionRejected("VERSION_MISMATCH", "部分事项已变化或不适用于该操作, 请刷新后重新生成方案")
    if not targets:
        raise DecisionRejected("INVALID_VALUE", "当前没有符合该操作的事项")
    if len(targets) > 50:
        raise DecisionRejected("INVALID_VALUE", "单次最多处理 50 项, 请指定范围")
    return targets


@dataclass(frozen=True)
class DirectInstruction:
    body: AssistantPlanIn
    expected_count: int | None = None


def direct_instruction(text: str) -> DirectInstruction | None:
    """仅识别完整的明确指令. 含引述、问题、条件或其他内容时交给模型提案, 不执行."""
    match = re.fullmatch(
        r"(?:请)?(?:把|将)?(?:当前|这些|这)?(?:(\d+|[一二两三四五六七八九十])项)?"
        r"(?:待补(?:充)?(?:证据|资料)?请求|补(?:充)?证据请求|待补资料请求)?"
        r"(?:全部|都|一并)(?:标记|标记为|设为)(?:为)?[‘“\"「]?无法取得[’”\"」]?"
        r"(?:[，,；;]\s*原因[：:]\s*([^\n]+))?[。.!！]?",
        text.strip(),
    )
    if match is None:
        english = re.fullmatch(
            r"(?:please )?mark all(?: (\d+))? (?:current )?evidence requests (?:as )?unavailable"
            r"(?:; reason: ([^\n]+))?[.!]?",
            text.strip(),
            re.IGNORECASE,
        )
        if english is None:
            return None
        count, reason = english.groups()
        return DirectInstruction(
            AssistantPlanIn(action="evidence_unavailable", reason=reason), int(count) if count else None
        )
    count, reason = match.groups()
    number = (
        int(count)
        if count and count.isdigit()
        else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(
            count or ""
        )
    )
    return DirectInstruction(AssistantPlanIn(action="evidence_unavailable", reason=reason), number)


class AssistantActions:
    def __init__(self, deps: AgentDeps, executor: ActionExecutor) -> None:
        self.deps, self.executor = deps, executor
        self.tasks: set[asyncio.Task[AssistantReceipt]] = set()

    async def propose(self, eid: str, actor: str, body: AssistantPlanIn, instruction: str) -> AssistantPlan:
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(eid)
        if engagement.locked:
            raise DecisionRejected("ENGAGEMENT_LOCKED", eid)
        plan = AssistantPlan(
            id=f"AP-{uuid4()}",
            action=body.action,
            targets=await current_targets(self.deps, eid, body),
            reason=body.reason.strip() if body.reason else None,
            instruction=instruction,
            proposed_for=actor,
            created_at=datetime.now(UTC),
        )
        await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.assistant_action,
                actor=actor,
                summary=f"审计助理操作方案: {plan.action}, {len(plan.targets)} 项",
                idempotency_key=f"{plan.id}:proposed",
                payload={"action": "proposed", "plan": plan.model_dump(mode="json")},
            )
        )
        return plan

    async def plans(self, eid: str) -> list[AssistantPlan]:
        entries = await all_of_kind(self.deps.trail, eid, TrailKind.assistant_action)
        receipts = {str(e.payload.get("plan_id")): e.payload["receipt"] for e in entries if "receipt" in e.payload}
        statuses = {str(e.payload.get("plan_id")): e.payload.get("action") for e in entries if "plan_id" in e.payload}
        plans = []
        for entry in entries:
            if entry.payload.get("action") != "proposed":
                continue
            data = dict(entry.payload["plan"])
            receipt = receipts.get(data["id"], {})
            data.update(
                status=statuses.get(data["id"], "proposed"),
                reason=receipt.get("reason", data.get("reason")),
                completed_at=receipt.get("completed_at"),
            )
            plans.append(AssistantPlan.model_validate(data))
        return plans

    async def get(self, eid: str, plan_id: str, actor: str) -> AssistantPlan:
        plan = next((p for p in await self.plans(eid) if p.id == plan_id), None)
        if plan is None or plan.proposed_for != actor:
            raise LookupError(plan_id)
        return plan

    async def execute(self, eid: str, plan_id: str, actor: str, reason: str | None = None) -> AssistantReceipt:
        plan = await self.get(eid, plan_id, actor)
        if reason is not None:
            plan = plan.model_copy(update={"reason": reason.strip() or None})
        task = asyncio.create_task(self.executor.execute_assistant(eid, plan, actor=actor))
        self.tasks.add(task)

        def done(finished: asyncio.Task[AssistantReceipt]) -> None:
            self.tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def shutdown(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
