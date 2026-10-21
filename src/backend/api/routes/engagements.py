"""POST, GET /engagements; GET /engagements/{eid}; POST /engagements/{eid}/resume; GET client-profile; GET plan;
PUT working-language; PUT currency."""

import re
from typing import Annotated, cast, get_args

from fastapi import APIRouter, Depends, Response

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps, get_query, get_runner, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.routes.clients import dataset_clients
from backend.api.schemas import (
    ClientProfileView,
    CurrencyIn,
    EngagementCreate,
    EngagementSummary,
    ProfileVersionInfo,
    WorkingLanguageIn,
)
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditException,
    ClientProfile,
    Engagement,
    GateId,
    User,
    WorkingLanguage,
)
from backend.contracts.views import EngagementOverview, PlanView
from backend.orchestration.decisions import language_trail
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.currency import currency_trail

router = APIRouter(prefix="/engagements", tags=["engagements"])


def parse_language(value: str) -> WorkingLanguage:
    """请求里的工作语言 (POST /engagements 的 working_language 与 PUT working-language 共用, 需求 R339): 须为 zh 或 en
    (区分大小写), 否则 422 INVALID_VALUE. 请求体里是字符串, 不写成 Literal: 那样会落到请求体校验的通用 422, 没有
    错误码."""
    if value not in get_args(WorkingLanguage):
        raise api_error("INVALID_VALUE", f"工作语言须为 zh 或 en, 现为 {value!r}")
    return cast(WorkingLanguage, value)


def parse_currency(value: str) -> str:
    """请求里的记账币种 (PUT currency, 需求 R366): 须为三个大写字母 (ISO 4217 代码, 不查是否在用), 否则 422
    INVALID_VALUE."""
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise api_error("INVALID_VALUE", f"记账币种须为三个大写字母的 ISO 4217 代码, 现为 {value!r}")
    return value


@router.post("", status_code=201)
async def create_engagement(
    body: EngagementCreate,
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    runner: Annotated[GraphRunner, Depends(get_runner)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> EngagementOverview:
    """建项目后启动流程, 返回时后台已在跑, running 为真. dataset 与 client_id 须给且只给一个, 否则 422.

    数据集客户 (给 dataset): 客户取数据集画像. 数据集不存在 404; 画像不合格, 补造文件与快照不匹配, 或数据集自带期间
    而请求的期间与之不同时 422, 不建项目 (载入器按数据集的期间取数, 不读项目期间).
    新建的客户 (给 client_id): 取该客户最新一版画像, 项目没有数据集. 客户不存在 404; 是数据集客户时 422 (按数据集建).
    working_language 不在 zh, en 时 422 INVALID_VALUE (parse_language, 同 PUT working-language).
    """
    if (body.dataset is None) == (body.client_id is None):
        raise api_error("INVALID_VALUE", "dataset 与 client_id 须给且只给一个")
    language = parse_language(body.working_language)
    if body.client_id is not None:
        return await create_for_client(body, body.client_id, language, user, deps, runner, query)
    assert body.dataset is not None
    try:
        profile = deps.datasets.client_profile(body.dataset)
        period = deps.datasets.period(body.dataset)
        currency = deps.datasets.currency(body.dataset)  # Xero 快照的本位币 (需求 R366); CSV 数据集为 None
    except FileNotFoundError as exc:
        raise api_error("NOT_FOUND", body.dataset) from exc
    except ValueError as exc:  # 含 SupplementMismatch 与画像校验失败 (ValidationError)
        raise api_error("INVALID_VALUE", str(exc)) from exc
    if period is not None and (body.period_start, body.period_end) != period:
        raise api_error(
            "INVALID_VALUE",
            f"期间须与数据集 {body.dataset} 相同: 数据集 {period[0]} 至 {period[1]}, "
            f"请求 {body.period_start} 至 {body.period_end}",
        )
    try:
        engagement = await deps.engagement_store.create(
            owner_id=user.id,
            period_start=body.period_start,
            period_end=body.period_end,
            dataset=body.dataset,
            profile=profile,
            working_language=language,
            currency=currency,
        )
    except ValueError as exc:
        raise api_error("INVALID_VALUE", str(exc)) from exc
    async with catch_api():
        await runner.start(engagement.id)
        return await query.overview(engagement.id)


async def create_for_client(
    body: EngagementCreate,
    client_id: str,
    language: WorkingLanguage,
    user: User,
    deps: AgentDeps,
    runner: GraphRunner,
    query: GraphStateQuery,
) -> EngagementOverview:
    """新建的客户的项目: 取最新一版画像 (同一客户下一年度建项目即带出最新一版), dataset 为 None."""
    if client_id in dataset_clients(deps):
        raise api_error("INVALID_VALUE", f"{client_id} 是数据集客户, 请按数据集建项目")
    profile = await deps.engagement_store.client_profile(client_id)
    if profile is None:
        raise api_error("NOT_FOUND", client_id)
    try:
        engagement = await deps.engagement_store.create(
            owner_id=user.id,
            period_start=body.period_start,
            period_end=body.period_end,
            dataset=None,
            profile=profile,
            working_language=language,
        )
    except ValueError as exc:
        raise api_error("INVALID_VALUE", str(exc)) from exc
    async with catch_api():
        await runner.start(engagement.id)
        return await query.overview(engagement.id)


@router.get("")
async def list_engagements(
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> list[EngagementSummary]:
    """只列出当前用户的项目. 闸门与待办数取总览, 与详情页一致."""
    owned = await deps.engagement_store.list(user.id)
    summaries: list[EngagementSummary] = []
    async with catch_api():
        for engagement in owned:
            overview = await query.overview(engagement.id)
            summaries.append(
                EngagementSummary(
                    id=engagement.id,
                    client_name=overview.client_name,
                    period_start=engagement.period_start,
                    period_end=engagement.period_end,
                    phase=overview.engagement.phase,
                    current_gate=overview.current_gate,
                    pending_tasks=overview.pending_tasks,
                    updated_at=overview.engagement.updated_at,
                )
            )
    return summaries


@router.get("/{eid}")
async def get_engagement(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> EngagementOverview:
    async with catch_api():
        return await query.overview(engagement.id)


@router.get("/{eid}/plan")
async def get_plan(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> PlanView:
    """审计计划页 (需求 R314): 当前计划, 各修订, PLN-15 决定, 所涉审计目标与特别风险, 程序表. 读检查点状态, 批准后
    照样返回; 功能 planning 关闭或还没起草计划时 current_plan_id 为空, 各列表为空. 项目不存在 404."""
    async with catch_api():
        return await query.plan(engagement.id)


@router.put("/{eid}/working-language")
async def set_working_language(
    body: WorkingLanguageIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> Engagement:
    """改项目的工作语言 (需求 R339), 返回改后的项目: 之后建的执行用新语言, 已有的执行与产出不改. 取值不在 zh, en 时
    422 INVALID_VALUE (parse_language); 项目已锁定 423 ENGAGEMENT_LOCKED (存储的 writable 一路, 同其他写入); 与当前值
    相同不改, 不留痕; 改了时写一行 decision 留痕 (decisions.language_trail, 写入人为当前用户), 留痕写失败时把语言改回
    原值再抛错, 不留下没有留痕的修改. 不取项目锁: 后台执行中照样可改, 改之后才建的执行记录取新值."""
    language = parse_language(body.language)
    async with catch_api():
        before = await deps.engagement_store.set_working_language(engagement.id, language)
        if before != language:
            try:
                await deps.trail.record(language_trail(engagement.id, user.id, before, language))
            except Exception:
                await deps.engagement_store.set_working_language(engagement.id, before)  # 补偿: 改回原值
                raise
        updated = await deps.engagement_store.get(engagement.id)
        if updated is None:
            raise api_error("NOT_FOUND", engagement.id)
    return updated


@router.put("/{eid}/currency")
async def set_currency(
    body: CurrencyIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> Engagement:
    """审计师确认或改项目的记账币种 (需求 R366, 数据管理页), 返回改后的项目: 来源记 auditor, 之后的金额显示与提示词
    输入按它; 已存的金额 (分) 不换算. 代码写法不对 422 INVALID_VALUE (parse_currency); 项目已锁定 423
    ENGAGEMENT_LOCKED; 币种与来源都不变时不改, 不留痕; 改了时写一行 data 留痕 (currency_trail, 写入人为当前用户),
    币种与留痕在同一事务中写入, 留痕失败整体回滚. 不取项目锁 (同 PUT working-language): 后台执行中照样可改."""
    currency = parse_currency(body.currency)
    async with catch_api():
        async with deps.output_store.transaction(engagement.id):
            before = await deps.engagement_store.set_currency(engagement.id, currency, "auditor")
            if (before.currency, before.currency_source) != (currency, "auditor"):
                await deps.trail.record(
                    currency_trail(engagement.id, user.id, before, currency, "auditor"), independent=False
                )
        updated = await deps.engagement_store.get(engagement.id)
        if updated is None:
            raise api_error("NOT_FOUND", engagement.id)
    return updated


@router.post("/{eid}/resume", status_code=202, response_model=None)
async def resume_engagement(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    runner: Annotated[GraphRunner, Depends(get_runner)],
) -> Response:
    """run_error 非空时重试. 项目忙返回 409 ENGAGEMENT_BUSY."""
    async with catch_api():
        await runner.resume(engagement.id)
    return Response(status_code=202)


@router.get("/{eid}/client-profile")
async def get_client_profile(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> ClientProfileView:
    """客户画像的最新版与第 1 版起的历史 (各版的出处: 谁, 何时, 来源), 版本最高的承接评分卡及其例外.

    editable 与 PUT /client-profile 的可用条件一致: 停在 ACC-01, 未锁定, 后台没在运行, 没有待恢复的错误.
    """
    eid, client_id = engagement.id, engagement.client_id
    async with catch_api():
        profile = await deps.engagement_store.client_profile(client_id)
        if profile is None:
            raise api_error("NOT_FOUND", client_id)
        records = await deps.engagement_store.profile_history(client_id)
        history: list[ClientProfile] = [record.profile for record in records]
        names: dict[str, str | None] = {}
        for user_id in {r.updated_by for r in records if r.updated_by is not None}:
            found = await deps.users.get(user_id)
            names[user_id] = found.full_name if found is not None else None
        versions = [
            ProfileVersionInfo(
                version=r.profile.version,
                source=r.source,
                updated_by=r.updated_by,
                updated_by_name=names.get(r.updated_by) if r.updated_by is not None else None,
                updated_at=r.updated_at,
            )
            for r in records
        ]
        scorecards = await deps.output_store.latest(eid, AcceptanceScorecard)
        scorecard = max(scorecards, key=lambda item: item.version, default=None)
        exceptions: list[AuditException] = []
        for exception_id in scorecard.exception_ids if scorecard else []:
            exception = await deps.output_store.get(eid, AuditException, exception_id)
            if exception is not None:
                exceptions.append(exception)
        overview = await query.overview(eid)
    editable = (
        overview.current_gate is GateId.ACC_01
        and not overview.engagement.locked
        and not overview.running
        and overview.run_error is None
    )
    return ClientProfileView(
        profile=profile,
        history=history,
        versions=versions,
        engagement_profile_version=overview.engagement.client_profile_version,
        scorecard=scorecard,
        exceptions=exceptions,
        editable=editable,
    )
