"""应用入口与组装根: 注入 Store, 适配器与检查点, 编译主图, 挂路由 (C 负责, A 提供图与编排入口)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from backend.adapters.abn_lookup import AbnLookupClient, CachedAbnLookup
from backend.adapters.confirmation_mock import DatasetConfirmationChannel
from backend.adapters.llm import SwitchableLLMClient, llm_version, make_llm_client
from backend.adapters.registry import CompositeRegistrySource, UploadedExtracts
from backend.adapters.registry_mock import DatasetRegistrySource
from backend.agents.base import AgentDeps
from backend.agents.concierge_chat.service import ChatLimits, ConciergeChat, make_chat_source
from backend.agents.registry import with_manual_owners
from backend.api.errors import validation_error
from backend.api.routes import api_router
from backend.api.routes.integrations import install_log_filter
from backend.core.config import Settings, get_settings
from backend.orchestration.checkpoint import open_checkpointer
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.persistence.data_store import PostgresDataStore
from backend.persistence.db import make_engine
from backend.persistence.stores import (
    CsvDatasetLoader,
    FileEvidenceStore,
    PostgresAuditOutputStore,
    PostgresEngagementStore,
    PostgresFinancialDataStore,
    PostgresLlmSettingsStore,
    PostgresUserStore,
    RoutingDatasetLoader,
    XeroDatasetLoader,
)
from backend.persistence.xero_connections import PostgresXeroConnectionStore
from backend.services.activities import ActivityBoard
from backend.services.assistant_actions import AssistantActions
from backend.services.audit_trail import PostgresAuditTrail
from backend.services.autonomy_policy import AutonomyPolicy
from backend.services.currency import engagement_currency
from backend.services.data_service import DataService
from backend.services.judgment_engine import JudgmentEngine, OutputValidator
from backend.services.llm_settings import LlmSettingsService
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from backend.services.rule_engine import SqlRuleEngine
from backend.services.xero_link import XeroLink


def build_deps(settings: Settings) -> AgentDeps:
    engine = make_engine(settings)
    financial_store = PostgresFinancialDataStore(engine)
    evidence_store = FileEvidenceStore(settings.EVIDENCE_ROOT, engine)
    features = settings.features  # 功能开关; 有未登记的名字或依赖不满足时在这里抛 ValueError
    library = ProcedureLibrary(settings.KNOWLEDGE_ROOT, features)
    trail = PostgresAuditTrail(engine)
    engagement_store = PostgresEngagementStore(engine)
    # 主模型与复核模型各一个可切换客户端, 先按 .env 建; lifespan 里 LlmSettingsService.load 再换成设置页保存的配置.
    # mock 模式按项目数据集选 fixture 目录 (Xero 读补造目录的 llm/), 所以要项目存储
    model_name = llm_version(settings)["model"]
    llm = SwitchableLLMClient(make_llm_client(settings, engagement_store), model_name)
    review_llm = SwitchableLLMClient(make_llm_client(settings, engagement_store), model_name)
    datasets = RoutingDatasetLoader(
        CsvDatasetLoader(settings.DATASET_ROOT, engine, evidence_store),
        {
            XeroDatasetLoader.PREFIX: XeroDatasetLoader(
                settings.DATASET_ROOT / "external" / "xero" / "api",
                settings.KNOWLEDGE_ROOT / "planning",
                engine,
                evidence_store,
                supplements=settings.DATASET_ROOT / "xero_demo",
            )
        },
    )
    # ABN 实时核查 (需求 R289): GUID 只在配置里, 建客户端时登记进 HTTP 日志的脱敏; 新客户的 ACC-04 经组合实现实查,
    # 表单的在线核对另包一层短时缓存
    abn_lookup = AbnLookupClient(settings.ABN_LOOKUP_GUID, settings.ABN_LOOKUP_URL)
    data = PostgresDataStore(settings.EVIDENCE_ROOT, engine)
    deps = AgentDeps(
        financial_store=financial_store,
        output_store=PostgresAuditOutputStore(engine),
        evidence_store=evidence_store,
        datasets=datasets,
        engagement_store=engagement_store,
        llm=llm,
        rules=SqlRuleEngine(financial_store),
        judgments=JudgmentEngine(
            llm,
            OutputValidator(financial_store, library, trail),
            trail,
            library,
            currency=engagement_currency(engagement_store),
        ),
        queue=ReviewQueue(library),
        library=library,
        trail=trail,
        activities=ActivityBoard(trail=trail),
        # 新客户的 ASIC 取上传的 ASIC 摘录的最新抽取结果 (需求 R290)
        registry=CompositeRegistrySource(
            engagement_store,
            DatasetRegistrySource(engagement_store, datasets.supplement_dir),
            abn_lookup,
            UploadedExtracts(financial_store, data, evidence_store),
        ),
        confirmations=DatasetConfirmationChannel(
            engagement_store, datasets.supplement_dir, settings.EVIDENCE_ROOT / ".confirmation_receipts"
        ),
        users=PostgresUserStore(engine),
        data=data,
        abn_lookup=CachedAbnLookup(abn_lookup),
        autonomy=AutonomyPolicy.load(library),
        review_llm=review_llm,
        features=features,
        fixed_language="en" if settings.LLM_MODE == "mock" else None,  # mock 的固定输出都是英文 (需求 R343)
    )
    return with_manual_owners(deps)  # 人工程序的 owner 最后找, 各处共用 (需求 R93)


async def recover_archives(runner: GraphRunner) -> None:
    """启动时补做签字后没做完的底稿归档 (需求 R337): 失败只记日志, 下次启动或读档案页再补."""
    from backend.services.workpapers import finalize

    for engagement in await runner.deps.engagement_store.list():
        if engagement.locked:
            try:
                await finalize(runner.deps, engagement.id)
            except Exception:
                logging.getLogger(__name__).exception("底稿归档补做失败: %s", engagement.id)


async def recover(runner: GraphRunner, data_service: DataService) -> None:
    """启动恢复: 先补做各项目没处理完的上传文件 (DataService.recover), 做完由它带动图继续; 其余项目再按 recover 规则
    处理 (GraphRunner.recover), 同一项目不重复拉起. 先处理文件: 图在等程序要的数据时 (需求 R272), 先拉起图会占着
    项目锁, 文件补做就被跳过."""
    await recover_archives(runner)
    taken = await data_service.recover()
    await runner.recover(skip=taken)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    deps = build_deps(settings)
    engine = cast(PostgresFinancialDataStore, deps.financial_store).engine
    llm_settings = LlmSettingsService(
        PostgresLlmSettingsStore(engine),
        cast(SwitchableLLMClient, deps.llm),
        cast(SwitchableLLMClient, deps.review_llm),
        settings,
    )
    try:
        await llm_settings.load()
        async with open_checkpointer(settings) as checkpointer:
            graph = compile_graph(GraphNodes(deps, settings.PARALLEL_CAP), checkpointer)
            app.state.deps = deps
            app.state.llm_settings = llm_settings
            app.state.runner = GraphRunner(graph, deps, llm_settings.versions)
            app.state.query = GraphStateQuery(graph, deps, app.state.runner)
            app.state.decisions = GraphDecisionSubmit(graph, deps, app.state.runner)
            app.state.assistant_actions = AssistantActions(deps, app.state.decisions)
            app.state.concierge = ConciergeChat(
                deps, app.state.query, make_chat_source(settings, llm_settings), ChatLimits.from_settings(settings)
            )
            app.state.concierge.actions = app.state.assistant_actions
            # Xero 网页连接 (需求 R279): 概览的 sources.xero 读连接表, 连接, 同步与断开经 XeroLink
            xero_store = PostgresXeroConnectionStore(engine)
            # 演示数据导入 (需求 R269) 的清单按数据集取补造目录
            app.state.data_service = DataService(deps, app.state.runner, demo=cast(RoutingDatasetLoader, deps.datasets))
            app.state.xero = XeroLink(deps, app.state.data_service, xero_store, settings)
            app.state.data_service.xero = app.state.xero.connections
            app.state.concierge.xero = app.state.xero.connections  # 对话待办的 Xero 组织信息核对 (需求 R291)
            try:
                await recover(app.state.runner, app.state.data_service)
                yield
            finally:
                # Stop jobs before closing their checkpoint connections, including startup failures.
                await app.state.concierge.shutdown()
                await app.state.assistant_actions.shutdown()
                await app.state.data_service.shutdown()
                await app.state.runner.shutdown()
                await deps.activities.settle()
    finally:
        await engine.dispose()


def operation_id(route: APIRoute) -> str:
    """生成的前端函数名为 {标签}{函数名}, 如 healthHealth."""
    return f"{route.tags[0]}-{route.name}" if route.tags else route.name


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="ELEC5620 审计支持系统", lifespan=lifespan, generate_unique_id_function=operation_id)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(RequestValidationError, validation_error)
    install_log_filter()  # 授权回调的 code 与 state 不进访问日志
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
