"""应用入口与组装根: 注入 Store, 适配器与检查点, 编译主图, 挂路由 (C 负责, A 提供图与编排入口)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from backend.adapters.confirmation_mock import MockConfirmationChannel
from backend.adapters.llm import SwitchableLLMClient, llm_version, make_llm_client
from backend.adapters.registry_mock import MockRegistrySource
from backend.agents.base import AgentDeps
from backend.api.errors import validation_error
from backend.api.routes import api_router
from backend.core.config import Settings, get_settings
from backend.orchestration.checkpoint import open_checkpointer
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
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
from backend.services.audit_trail import PostgresAuditTrail
from backend.services.judgment_engine import JudgmentEngine, OutputValidator
from backend.services.llm_settings import LlmSettingsService
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from backend.services.rule_engine import SqlRuleEngine


def build_deps(settings: Settings) -> AgentDeps:
    engine = make_engine(settings)
    financial_store = PostgresFinancialDataStore(engine)
    evidence_store = FileEvidenceStore(settings.EVIDENCE_ROOT, engine)
    library = ProcedureLibrary(settings.KNOWLEDGE_ROOT)
    dataset_dir = settings.DATASET_ROOT / "demo"
    # 主模型与复核模型各一个可切换客户端, 先按 .env 建; lifespan 里 LlmSettingsService.load 再换成设置页保存的配置
    model_name = llm_version(settings)["model"]
    llm = SwitchableLLMClient(make_llm_client(settings), model_name)
    review_llm = SwitchableLLMClient(make_llm_client(settings), model_name)
    trail = PostgresAuditTrail(engine)
    return AgentDeps(
        financial_store=financial_store,
        output_store=PostgresAuditOutputStore(engine),
        evidence_store=evidence_store,
        datasets=RoutingDatasetLoader(
            CsvDatasetLoader(settings.DATASET_ROOT, engine, evidence_store),
            {
                XeroDatasetLoader.PREFIX: XeroDatasetLoader(
                    settings.DATASET_ROOT / "external" / "xero" / "api",
                    settings.KNOWLEDGE_ROOT / "planning",
                    engine,
                    evidence_store,
                )
            },
        ),
        engagement_store=PostgresEngagementStore(engine),
        llm=llm,
        rules=SqlRuleEngine(financial_store),
        judgments=JudgmentEngine(llm, OutputValidator(financial_store, library, trail), trail, library),
        queue=ReviewQueue(library),
        library=library,
        trail=trail,
        registry=MockRegistrySource(dataset_dir),
        confirmations=MockConfirmationChannel(dataset_dir, settings.EVIDENCE_ROOT / ".confirmation_receipts"),
        users=PostgresUserStore(engine),
        review_llm=review_llm,
    )


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
            try:
                await app.state.runner.recover()
                yield
            finally:
                # Stop jobs before closing their checkpoint connections, including startup failures.
                await app.state.runner.shutdown()
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
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
