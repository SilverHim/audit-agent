"""复核队列, 判断呈递, 程序详情, 判断详情与例外; 重跑过期程序."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_decisions, get_deps, get_query, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import (
    IdBody,
    JudgmentDetail,
    ProcedureCatalogEntry,
    ProcedureDetail,
    RerunIn,
    ReviewRecordIn,
    ReviewTaskView,
    ReviewWithdrawalIn,
)
from backend.contracts.models import (
    AuditException,
    Decision,
    DisputeResolution,
    DraftRule,
    Engagement,
    Judgment,
    Misstatement,
    MisstatementUpdateKind,
    Phase,
    Procedure,
    ProcedureRun,
    RerunRequest,
    ReviewRecord,
    ReviewTask,
    ReviewTier,
    ReviewWithdrawal,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailKind,
    User,
)
from backend.contracts.views import EngagementOverview, TaskPresentation
from backend.orchestration.decisions import TARGET_MODELS, GraphDecisionSubmit, update_decisions, withdraw_problem
from backend.orchestration.query import GraphStateQuery
from backend.services.deferrals import is_active, latest_entries
from backend.services.features import COLLAB
from backend.services.presentation import assemble, reads_llm_calls, record_presentation
from backend.services.review_queue import review_tier, subject_amount, task_amount
from backend.services.staleness import rerunnable, stale_procedures, superseded_runs
from backend.services.trail_pages import all_of_kind

router = APIRouter(prefix="/engagements/{eid}", tags=["reviews"])


def _allowed(task: ReviewTask, deps: AgentDeps, target: Judgment | AuditException | ProcedureRun) -> list[Decision]:
    permitted = deps.queue.allowed(task, target)
    return [item for item in Decision if item in permitted]


@dataclass(frozen=True)
class TaskParts:
    """复核队列与判断呈递共用的一项: 程序名, 目标, 允许的决定, 金额, 档位, 仲裁结果与草稿规则 (同一取法)."""

    procedure_name: str
    target: Judgment | AuditException | ProcedureRun
    allowed_decisions: list[Decision]
    amount_cents: int | None
    tier: ReviewTier
    allowed_resolutions: list[DisputeResolution]
    draft_rules: dict[DisputeResolution, DraftRule]
    misstatement_update_kind: MisstatementUpdateKind | None  # 所属程序模板的错报更新类别 (需求 R163)
    update_decisions: list[Decision]  # 允许的决定中须带 misstatement_update 的 (decisions.update_decisions)


async def updated_misstatement(
    deps: AgentDeps, eid: str, target: Judgment | AuditException | ProcedureRun
) -> dict[str, Misstatement]:
    """判断携带错报更新 (FIN-03, FIN-09) 时所指错报的当前版本, 供 task_amount 取金额 (需求 R168, 与判断级降级同一
    口径); 别的目标与查不到时为空."""
    update = target.misstatement_update if isinstance(target, Judgment) else None
    if update is None:
        return {}
    found = await deps.output_store.get(eid, Misstatement, update.misstatement_id)
    return {found.id: found} if found is not None else {}


async def task_parts(
    deps: AgentDeps,
    eid: str,
    task: ReviewTask,
    exceptions: dict[str, AuditException],
    overview: EngagementOverview,
    version: int | None = None,
) -> TaskParts:
    """一条任务在队列与呈递里共用的部分. allowed_decisions 取 ReviewQueue.allowed; amount_cents 与 tier 按总览的执行
    重要性与明显微小临界值计算 (对话与队列共用同一口径); draft_rules 按项目冻结的功能集 (总览 features), 开了 collab
    才有. exceptions: 例外 ID -> 当前版本. version: 目标取哪一版, 缺省最新版 (队列), 呈递取任务绑定的版本.
    程序或目标查不到 404 NOT_FOUND."""
    try:
        template = deps.library.load(task.procedure_id)
    except KeyError as exc:
        raise api_error("NOT_FOUND", task.procedure_id) from exc
    target = await deps.output_store.get(eid, TARGET_MODELS[task.target_type], task.target_id, version)
    if not isinstance(target, Judgment | AuditException | ProcedureRun):
        raise api_error("NOT_FOUND", task.target_id)
    misstatements = await updated_misstatement(deps, eid, target)
    subject = await subject_amount(deps.financial_store, eid, target) if isinstance(target, Judgment) else None
    amount = task_amount(target, exceptions, subject, misstatements)
    allowed = _allowed(task, deps, target)
    return TaskParts(
        procedure_name=template.name,
        target=target,
        allowed_decisions=allowed,
        amount_cents=amount,
        tier=review_tier(task, target, amount, overview.performance_materiality_cents, overview.clearly_trivial_cents),
        allowed_resolutions=deps.queue.resolutions(task, target),
        draft_rules=deps.queue.draft_rules(task, target, COLLAB in overview.features),
        misstatement_update_kind=template.misstatement_update_kind,
        update_decisions=update_decisions(task.kind, allowed, template.misstatement_update_kind),
    )


@router.get("/review-tasks")
async def list_review_tasks(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
    status: TaskStatus | None = None,
    kind: TaskKind | None = None,
) -> list[ReviewTaskView]:
    """status, kind 省略时不按它过滤 (都省略为全部), 按创建时间升序; 人工队列另见 GET /manual-tasks (需求 R94).
    allowed_decisions 取 ReviewQueue.allowed (按目标的 AI 结论, 需求 R363); amount_cents 与 tier 按总览的执行重要性与
    明显微小临界值计算, 对话与队列共用同一口径. draft_rules 按项目冻结的功能集 (总览 features) 给: 开了 collab 才有.
    withdrawable_record_id: 总览 can_withdraw 为真时, 已决定的任务上最近一条没撤回且能撤回的决定 (需求 R363)."""
    filters: dict[str, TaskStatus] = {}
    if status is not None:
        filters["status"] = status
    async with catch_api():
        tasks = await deps.output_store.latest(engagement.id, ReviewTask, **filters)
        exceptions = {x.id: x for x in await deps.output_store.latest(engagement.id, AuditException)}
        overview = await query.overview(engagement.id)
        withdrawable = await withdrawable_records(deps, engagement.id) if overview.can_withdraw else {}
        views: list[ReviewTaskView] = []
        for task in sorted(tasks, key=lambda item: item.created_at):
            if kind is not None and task.kind is not kind:
                continue
            parts = await task_parts(deps, engagement.id, task, exceptions, overview)
            views.append(
                ReviewTaskView(
                    task=task,
                    procedure_name=parts.procedure_name,
                    target=parts.target,
                    allowed_decisions=parts.allowed_decisions,
                    amount_cents=parts.amount_cents,
                    tier=parts.tier,
                    allowed_resolutions=parts.allowed_resolutions,
                    draft_rules=parts.draft_rules,
                    misstatement_update_kind=parts.misstatement_update_kind,
                    update_decisions=parts.update_decisions,
                    withdrawable_record_id=withdrawable.get(task.id),
                )
            )
    return views


async def withdrawable_records(deps: AgentDeps, eid: str) -> dict[str, str]:
    """任务 ID → 该任务上能撤回的决定 ID (需求 R363): 已决定的任务上最近一条没撤回的决定 (按决定时间), 能撤回
    (decisions.withdraw_problem, 与撤回的校验同一函数) 的才列. 图的位置由调用方按总览 can_withdraw 判断."""
    store = deps.output_store
    records = await store.latest(eid, ReviewRecord)
    withdrawals = await store.latest(eid, ReviewWithdrawal)
    withdrawn = {w.record_id for w in withdrawals}
    tasks = {t.id: t for t in await store.latest(eid, ReviewTask, status=TaskStatus.decided)}
    latest: dict[str, ReviewRecord] = {}
    for record in sorted(records, key=lambda r: r.decided_at):
        if record.task_id in tasks and record.id not in withdrawn:
            latest[record.task_id] = record
    return {
        task_id: record.id
        for task_id, record in latest.items()
        if withdraw_problem(record, tasks[task_id], records, withdrawals, deps.library) is None
    }


@router.post("/review-tasks/{task_id}/presentations")
async def present_review_task(
    task_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> TaskPresentation:
    """判断呈递 (GEN-11): 审计师打开一条复核任务. 返回按任务类型组装的视图 (services.presentation.assemble; 目标取任务
    绑定的版本, 金额与档位同复核队列), 并写一行 presentation 留痕 (每次打开都记, presentation_id 为其 ID). 任务不存在
    404 NOT_FOUND, 不写留痕. 不改任务, 不写 Store, 不取项目锁: 主图运行中, 项目已锁定, 任务已决定都照常可调.
    deferred 按稍后处理的留痕 (已决定的任务为假); 只有带对象的 failed 与 unattributed 任务读 llm_call 留痕取失败
    原因 (presentation.reads_llm_calls)."""
    eid = engagement.id
    async with catch_api():
        task = await deps.output_store.get(eid, ReviewTask, task_id)
        if task is None:
            raise api_error("NOT_FOUND", task_id)
        exceptions = {x.id: x for x in await deps.output_store.latest(eid, AuditException)}
        overview = await query.overview(eid)
        parts = await task_parts(deps, eid, task, exceptions, overview, task.target_version)
        marked = latest_entries(await all_of_kind(deps.trail, eid, TrailKind.deferral)).get(("review_task", task.id))
        deferred = marked is not None and is_active(marked, {task.id: task}, None, [])
        calls = await all_of_kind(deps.trail, eid, TrailKind.llm_call) if reads_llm_calls(task) else []
        presentation_id = await record_presentation(deps.trail, eid, task, user.id, datetime.now(UTC))
    return assemble(
        task,
        parts.target,
        presentation_id=presentation_id,
        procedure_name=parts.procedure_name,
        allowed_decisions=parts.allowed_decisions,
        allowed_resolutions=parts.allowed_resolutions,
        amount_cents=parts.amount_cents,
        tier=parts.tier,
        deferred=deferred,
        llm_calls=calls,
    )


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


@router.post("/review-records/{record_id}/withdraw", status_code=202)
async def withdraw_review_record(
    record_id: str,
    body: ReviewWithdrawalIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> IdBody:
    """撤回一条复核决定, 重新选择 (需求 R363); 返回撤回 ID (WD-{uuid4}). 理由选填. 只能在执行阶段等复核或 FIN-01
    闸门处 (意见决定之前; FIN-01 已通过等 409 NOT_WITHDRAWABLE); 决定不存在 404; 不能撤回的决定 (见
    decisions.withdraw_problem) 422 INVALID_VALUE; 锁定 423, 执行中 409. 撤回后任务回到待决, 图回到等复核."""
    async with catch_api():
        withdrawal = await decisions.withdraw_review(
            engagement.id, f"WD-{uuid4()}", record_id, reason=body.reason, withdrawn_by=user.id
        )
    return IdBody(id=withdrawal.id)


@router.get("/procedures")
async def list_procedures(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[ProcedureCatalogEntry]:
    """系统完整程序库, 含未启用功能和未纳入本项目的程序, 按阶段与编号排序. 项目状态仍取总览 procedures;
    未纳入的程序不创建项目实例, 不可执行或重跑. 项目不存在或不属于当前用户时 404."""
    return [
        ProcedureCatalogEntry(
            id=t.id,
            name=t.name,
            phase=t.phase,
            owner=t.owner,
            engine=t.engine,
            autonomy_level=t.autonomy_level,
            feature=t.feature,
            depends_on=list(t.depends_on),
            standards=list(t.standards),
        )
        for t in sorted(deps.library.all(include_disabled=True), key=lambda t: (list(Phase).index(t.phase), t.id))
    ]


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
