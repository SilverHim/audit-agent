"""应用入口与组装根: 注入 Store, 适配器与检查点, 编译主图, 挂路由 (C 负责, A 提供图与编排入口)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from backend.adapters.confirmation_mock import MockConfirmationChannel
from backend.adapters.llm import make_llm_client
from backend.adapters.registry_mock import MockRegistrySource
from backend.agents.base import AgentDeps
from backend.api.routes import api_router
from backend.core.config import Settings, get_settings
from backend.orchestration.checkpoint import open_checkpointer
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import (
    CsvDatasetLoader,
    FileEvidenceStore,
    PostgresAuditOutputStore,
    PostgresEngagementStore,
    PostgresFinancialDataStore,
)
from backend.services.audit_trail import PostgresAuditTrail
from backend.services.judgment_engine import JudgmentEngine
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from backend.services.rule_engine import SqlRuleEngine


def build_deps(settings: Settings) -> AgentDeps:
    financial_store = PostgresFinancialDataStore()
    library = ProcedureLibrary(settings.KNOWLEDGE_ROOT)
    dataset_dir = settings.DATASET_ROOT / "demo"
    return AgentDeps(
        financial_store=financial_store,
        output_store=PostgresAuditOutputStore(),
        evidence_store=FileEvidenceStore(settings.EVIDENCE_ROOT),
        datasets=CsvDatasetLoader(settings.DATASET_ROOT),
        engagement_store=PostgresEngagementStore(),
        llm=make_llm_client(settings),
        rules=SqlRuleEngine(financial_store),
        judgments=JudgmentEngine(),
        queue=ReviewQueue(library),
        library=library,
        trail=PostgresAuditTrail(),
        registry=MockRegistrySource(dataset_dir),
        confirmations=MockConfirmationChannel(dataset_dir),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    deps = build_deps(settings)
    async with open_checkpointer(settings) as checkpointer:
        graph = compile_graph(GraphNodes(deps, settings.PARALLEL_CAP), checkpointer)
        app.state.deps = deps
        app.state.runner = GraphRunner(graph, deps)
        app.state.query = GraphStateQuery(graph, deps, app.state.runner)
        app.state.decisions = GraphDecisionSubmit(graph, deps, app.state.runner)
        yield


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
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
