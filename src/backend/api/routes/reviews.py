"""复核队列, 程序详情, 判断详情与例外; 重跑过期程序."""

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Query

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_decisions, get_deps, get_query, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import IdBody, JudgmentDetail, ProcedureDetail, RerunIn, ReviewRecordIn, ReviewTaskView
from backend.contracts.models import (
    AuditException,
    Decision,
    Engagement,
    Judgment,
    Procedure,
    ProcedureRun,
    RerunRequest,
    ReviewRecord,
    ReviewTask,
    SourceType,
    TargetType,
    TaskStatus,
    User,
)
from backend.orchestration.decisions import TARGET_MODELS, GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery
from backend.services.features import COLLAB
from backend.services.review_queue import review_tier, row_amount, subject_row, task_amount
from backend.services.staleness import rerunnable, stale_procedures, superseded_runs

router = APIRouter(prefix="/engagements/{eid}", tags=["reviews"])


def _allowed(task: ReviewTask, deps: AgentDeps) -> list[Decision]:
    permitted = deps.queue.allowed(task)
    return [item for item in Decision if item in permitted]


async def subject_amount(deps: AgentDeps, eid: str, target: Judgment | AuditException | ProcedureRun) -> int | None:
    """判断对象本身是交易行时该行的金额 (见 review_queue.subject_row), 否则或查不到该行为 None."""
    pointer = subject_row(target) if isinstance(target, Judgment) else None
    if pointer is None:
        return None
    row = await deps.financial_store.record(eid, cast(SourceType, pointer.source_type), pointer.source_id)
    return row_amount(pointer.source_type, row) if row is not None else None


@router.get("/review-tasks")
async def list_review_tasks(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
    status: TaskStatus | None = None,
) -> list[ReviewTaskView]:
    """status 省略时返回全部, 按创建时间升序. allowed_decisions 取 ReviewQueue.allowed;
    amount_cents 与 tier 按总览的执行重要性与明显微小临界值计算, 对话与队列共用同一口径.
    draft_rules 按项目冻结的功能集 (总览 features) 给: 开了 collab 才有."""
    filters: dict[str, TaskStatus] = {}
    if status is not None:
        filters["status"] = status
    async with catch_api():
        tasks = await deps.output_store.latest(engagement.id, ReviewTask, **filters)
        exceptions = {x.id: x for x in await deps.output_store.latest(engagement.id, AuditException)}
        overview = await query.overview(engagement.id)
        views: list[ReviewTaskView] = []
        for task in sorted(tasks, key=lambda item: item.created_at):
            try:
                template = deps.library.load(task.procedure_id)
            except KeyError as exc:
                raise api_error("NOT_FOUND", task.procedure_id) from exc
            target = await deps.output_store.get(engagement.id, TARGET_MODELS[task.target_type], task.target_id)
            if not isinstance(target, Judgment | AuditException | ProcedureRun):
                raise api_error("NOT_FOUND", task.target_id)
            amount = task_amount(target, exceptions, await subject_amount(deps, engagement.id, target))
            views.append(
                ReviewTaskView(
                    task=task,
                    procedure_name=template.name,
                    target=target,
                    allowed_decisions=_allowed(task, deps),
                    amount_cents=amount,
                    tier=review_tier(
                        task,
                        target,
                        amount,
                        overview.performance_materiality_cents,
                        overview.clearly_trivial_cents,
                    ),
                    allowed_resolutions=deps.queue.resolutions(task, target),
                    draft_rules=deps.queue.draft_rules(task, target, COLLAB in overview.features),
                )
            )
    return views


@router.post("/review-records", status_code=202)
async def submit_review_record(
    body: ReviewRecordIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> IdBody:
    record = ReviewRecord.model_validate(
        {**body.model_dump(), "id": f"D-{uuid4()}", "reviewed_by": user.id, "decided_at": datetime.now(UTC)}
    )
    async with catch_api():
        await decisions.submit(engagement.id, record)
    return IdBody(id=record.id)


@router.get("/procedures/{pid}")
async def get_procedure(
    pid: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> ProcedureDetail:
    async with catch_api():
        procedure = await deps.output_store.get(engagement.id, Procedure, pid)
        if procedure is None:
            raise api_error("NOT_FOUND", pid)
        try:
            template = deps.library.load(pid)
        except KeyError as exc:
            raise api_error("NOT_FOUND", pid) from exc
        runs = await deps.output_store.latest(engagement.id, ProcedureRun, procedure_id=pid)
        exceptions = await deps.output_store.latest(engagement.id, AuditException, procedure_id=pid)
        judgments = await deps.output_store.latest(engagement.id, Judgment, procedure_id=pid)
        # 过期与可重跑同总览 (以 Store 为准); 被取代的执行取 Store 中的重跑请求
        pack = await deps.output_store.latest(engagement.id, Procedure)
        all_runs = await deps.output_store.latest(engagement.id, ProcedureRun)
        superseded = superseded_runs(await deps.output_store.latest(engagement.id, RerunRequest))
        stale = stale_procedures(pack, all_runs, await deps.data.changes(engagement.id), deps.library, superseded)
    return ProcedureDetail(
        procedure=procedure,
        name=template.name,
        depends_on=list(template.depends_on),
        standards=list(template.standards),
        runs=sorted(runs, key=lambda run: run.run_no),
        exceptions=exceptions,
        judgments=judgments,
        stale=pid in stale,
        rerunnable=pid in rerunnable(pack, stale),
        superseded_run_ids=sorted(r.id for r in runs if r.id in superseded),
    )


@router.post("/procedures/rerun", status_code=202)
async def rerun_procedures(
    body: RerunIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> IdBody:
    """用新数据重跑过期的执行阶段程序 (单条或全部, 需求 R274); 返回重跑请求 ID. 只能在等复核, FIN-01 或 RPT-01
    闸门处 (409 NOT_RERUNNABLE); 所列程序不是可重跑的过期程序 422 INVALID_VALUE; 锁定 423, 执行中 409."""
    async with catch_api():
        request = await decisions.rerun_stale(engagement.id, f"RR-{uuid4()}", body.procedure_ids, requested_by=user.id)
    return IdBody(id=request.id)


@router.get("/judgments/{jid}")
async def get_judgment(
    jid: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    version: Annotated[int | None, Query()] = None,
) -> JudgmentDetail:
    """version 省略时取最新版. review_records 是指向这条判断的复核决定."""
    async with catch_api():
        judgment = await deps.output_store.get(engagement.id, Judgment, jid, version)
        if judgment is None:
            raise api_error("NOT_FOUND", jid)
        versions = await deps.output_store.history(engagement.id, Judgment, jid)
        tasks = await deps.output_store.latest(
            engagement.id, ReviewTask, target_id=jid, target_type=TargetType.Judgment
        )
        records: list[ReviewRecord] = []
        for task in tasks:
            records.extend(await deps.output_store.latest(engagement.id, ReviewRecord, task_id=task.id))
    return JudgmentDetail(judgment=judgment, versions=versions, review_records=records)


@router.get("/exceptions")
async def list_exceptions(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    procedure_id: str | None = None,
) -> list[AuditException]:
    filters: dict[str, str] = {}
    if procedure_id is not None:
        filters["procedure_id"] = procedure_id
    async with catch_api():
        return await deps.output_store.latest(engagement.id, AuditException, **filters)
