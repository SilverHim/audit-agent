"""连库用例的共用装配 (普通辅助模块, 不是测试模块): 测试库连接串与新用户, 测试库中的项目 (DbProject), 按补造清单写好
账表的 Xero q1 项目 (需求 R112, build_xero_project), 经数据集载入的 Numbat 项目 (numbat_project), 程序第 1 次执行的
记录 (first_run) 与以其参数快照跑规则 (rules_with).

没有 TEST_DATABASE_URL 时 database_url 跳过. 只读的规则用例共用会话级的项目 (conftest 的 xero_db, numbat_db); 要改
账表的用例各建一个 (build_xero_project).
"""

import os
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.adapters.registry_mock import DatasetRegistrySource
from backend.adapters.xero_supplements import plan
from backend.agents.base import AgentDeps
from backend.contracts.data import TableKind
from backend.contracts.models import ClientProfile, Engagement, Phase, Procedure, ProcedureRun
from backend.contracts.records import DatasetTraits
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import build_run
from backend.persistence import models as m
from backend.persistence.stores import RoutingDatasetLoader, XeroDatasetLoader, now

PERIOD_START, PERIOD_END = date(2026, 7, 1), date(2026, 9, 30)  # Xero q1 项目的期间
MATERIALITY = {"materiality_cents": 32431, "performance_materiality_cents": 16216, "clearly_trivial_cents": 1622}
REVENUE = ["OBJ-REV-OCC", "OBJ-REV-CUT"]  # 补造计划预置的收入两条目标, significant
CASH = ["OBJ-CASH-EXI"]  # 现金目标, high
LINKS = {"REV-08": REVENUE, "REV-02": REVENUE, "CSH-05": CASH, "CSH-02": CASH}  # 程序关联的目标; 其余没有
NUMBAT_MATERIALITY = {  # Numbat 的重要性 (R55): 整体 12000000, 执行 9000000, 明显微小 600000
    "materiality_cents": 12000000,
    "performance_materiality_cents": 9000000,
    "clearly_trivial_cents": 600000,
}


def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    return url


async def new_owner(engine: AsyncEngine) -> str:
    """测试库里新建一个用户 (项目负责人), 返回其 ID."""
    owner = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=owner, email=owner + "@example.com", full_name="Jordan Ellis", password_hash="x", created_at=now()
            )
        )
    return owner


@dataclass(frozen=True)
class DbProject:
    """测试库中的一个项目: 依赖 (组装根的真实存储与规则引擎), 项目, 进入 P2 时的状态, 解析参数用的数据集声明与客户
    画像."""

    deps: AgentDeps
    eid: str
    engagement: Engagement
    state: EngagementState
    traits: DatasetTraits
    profile: ClientProfile


@dataclass(frozen=True)
class XeroProject(DbProject):
    """测试库中按补造清单写好账表的 Xero 项目 (state 带计划预置的目标与风险, 闸门定的重要性)."""

    owner: str
    datasets: RoutingDatasetLoader  # 数据集载入与登记 mock 按给定的快照与补造目录
    supplement_tables: list[tuple[TableKind, str]]  # 补造清单的账表与来源文件, 按写入顺序


async def build_xero_project(url: str, snapshot: Path, supplements: Path, tmp: Path) -> XeroProject:
    """测试库中建 Xero 项目 (数据集 xero/<快照目录名>, 期间 2026-07-01 至 09-30), 账表按补造清单写入 (同演示数据导入
    的账表部分: 快照七张为数据版本 1, source xero; 补造账表各一版, source import; 不经数据集载入, 不上传文件).
    snapshot, supplements 为快照目录与补造目录 (基线或其副本); 依赖为组装根的真实实现, 只把数据集载入与登记 mock 换成
    指向这两个目录的."""
    from backend.core.config import Settings
    from backend.main import build_deps

    settings = Settings(DATABASE_URL=url, EVIDENCE_ROOT=tmp / "evidence", LLM_MODE="mock")
    deps = build_deps(settings)
    engine = deps.data.engine  # type: ignore[attr-defined]  # 组装根的引擎 (应用角色)
    xero = XeroDatasetLoader(
        snapshot.parent,
        settings.KNOWLEDGE_ROOT / "planning",
        engine,
        deps.evidence_store,
        supplements=supplements.parent,
        allow_injected=True,  # 副本 (xero_case) 的 manifest 带 injected; 原目录没有, 照常核对
    )
    datasets = RoutingDatasetLoader(deps.datasets.default, {XeroDatasetLoader.PREFIX: xero})  # type: ignore[attr-defined]  # Numbat 照旧
    dataset = f"{XeroDatasetLoader.PREFIX}{snapshot.name}"
    deps = replace(
        deps,
        datasets=datasets,
        registry=DatasetRegistrySource(deps.engagement_store, datasets.supplement_dir),  # type: ignore[arg-type]
    )
    owner = await new_owner(engine)
    profile = xero.client_profile(dataset).model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=owner, period_start=PERIOD_START, period_end=PERIOD_END, dataset=dataset, profile=profile
    )
    demo = plan(xero, dataset)
    await deps.data.write_tables(engagement.id, demo.snapshot, source="xero", actor=owner)
    for table in demo.tables:
        await deps.data.write_tables(
            engagement.id, {table.kind: table.rows}, source="import", actor=owner, file_name=table.file_name
        )
    objectives, risks = xero.planning_presets(dataset)
    state = initial_state(engagement, [], profile.version) | {  # type: ignore[operator]
        "audit_objectives": objectives,
        "risk_register": risks,
        **MATERIALITY,
    }
    return XeroProject(
        deps=deps,
        eid=engagement.id,
        engagement=engagement,
        state=state,  # type: ignore[arg-type]
        traits=xero.dataset_traits(dataset),
        profile=profile,
        owner=owner,
        datasets=datasets,
        supplement_tables=[(t.kind, t.file_name) for t in demo.tables],
    )


async def numbat_project(tmp: Path) -> DbProject:
    """测试库中经数据集载入的 Numbat 项目 (data/demo, 期间 2025-07-01 至 2026-06-30): 没有数据集声明 (取缺省), 状态
    只加重要性 (NUMBAT_MATERIALITY), 没有审计目标."""
    from backend.core.config import Settings
    from backend.main import build_deps

    deps = build_deps(Settings(DATABASE_URL=database_url(), EVIDENCE_ROOT=tmp / "evidence", LLM_MODE="mock"))
    owner = await new_owner(deps.data.engine)  # type: ignore[attr-defined]
    profile = deps.datasets.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=owner, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
    )
    await deps.datasets.load(engagement.id, "demo")
    state = initial_state(engagement, [], profile.version) | NUMBAT_MATERIALITY
    return DbProject(deps, engagement.id, engagement, state, DatasetTraits(), profile)  # type: ignore[arg-type]


def procedure_of(deps: AgentDeps, procedure_id: str) -> Procedure:
    """程序第 1 版 (P2, 关联 LINKS 中的目标)."""
    template = deps.library.load(procedure_id)
    return Procedure(
        id=procedure_id,
        phase=Phase.P2_Executing,
        template_commit=deps.library.commit,
        objective_ids=LINKS.get(procedure_id, []),
        autonomy_level=template.autonomy_level,
    )


def first_run(project: DbProject, procedure_id: str) -> ProcedureRun:
    """该程序第 1 次执行的记录: 参数由程序库按项目, 状态 (重要性, 关联目标的风险), 数据集声明与客户画像解析 (同
    GraphNodes 的 build_run)."""
    deps = project.deps
    return build_run(
        deps.library,
        deps.library.load(procedure_id),
        procedure_of(deps, procedure_id),
        project.engagement,
        project.state,
        datetime.now(UTC),
        traits=project.traits,
        profile=project.profile,
    )


async def rules_with(project: DbProject, procedure_id: str, run: ProcedureRun, **overrides: Any) -> RuleResult:
    """以 run 的参数快照 (叠加 overrides) 用真实的规则引擎跑该程序的规则."""
    deps = project.deps
    return await deps.rules.run(project.eid, deps.library.load(procedure_id), run, run.params_snapshot | overrides)
