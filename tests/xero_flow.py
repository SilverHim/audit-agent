"""Xero 演示项目从启动到确认数据已齐的流程辅助 (需求 R269; R119, R120, R259 以后共用).

只经 GraphRunner, GraphDecisionSubmit 与 DataService 的接口推进, 假存储 (tests/xero_fakes) 与真实库都能用:
start → 停在 ACC-01 闸门 → 演示导入 acceptance → ACC-01 approve → 停在 p1_await_data → 演示导入 fieldwork →
确认数据已齐 → 图继续 (DOC-04 沿用上传时抽出的合同).

经 HTTP 走的演示 (走通用例 R287 与真实模型变体 R311 的不联网自测) 另用 demo_offline 装离线依赖与客户端, xero_link
给数据接口的 Xero 连接服务 (需求 R353).
"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from backend.adapters.registry_mock import DatasetRegistrySource
from backend.agents.base import AgentDeps
from backend.contracts.data import DemoImportResult, DemoStage
from backend.contracts.models import GateVerdict, TrailEntry, TrailKind
from backend.contracts.ports import DatasetLoader, LLMClient
from backend.core.config import get_settings
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import RoutingDatasetLoader, XeroDatasetLoader
from backend.services.data_service import DataService
from backend.services.demo_import import DEMO_JOB
from backend.services.xero_link import XeroLink
from tests.external import supplements_dir
from tests.fakes import (
    NUMBAT_AUDITOR,
    CreatingEngagementStore,
    FakeRuleEngine,
    FakeTrail,
    FakeXeroConnectionStore,
    acc01_decision,
    api_client,
    wait_idle,
)
from tests.xero_fakes import (
    DEMO_FEATURES,
    PLANNING,
    SNAPSHOT_NAME,
    SNAPSHOT_REPO,
    XERO_RULES,
    new_xero_deps,
    snapshot_root,
)

API = "/api/v1"  # 接口前缀
READY_REASON = "演示数据已导入"  # 确认数据已齐时的理由 (Xero 录入人为连接器占位值, 审批日志一项待补交)


def demo_source(supplements: Path | None = None) -> RoutingDatasetLoader:
    """演示导入清单的来源, 同组装根按数据集名分流: xero/ 走不连库的 Xero 载入器 (只用 supplement; 快照取
    tests.xero_fakes 定位到的, 没有时跳过), 其余 (Numbat) 没有补造目录. supplements 可换成补造目录的临时副本, 缺省
    为经 tests.external 核对的补造根."""
    supplements = supplements_dir(SNAPSHOT_NAME).parent if supplements is None else supplements
    xero = XeroDatasetLoader(snapshot_root(SNAPSHOT_REPO).parent, PLANNING, supplements=supplements)
    return RoutingDatasetLoader(cast(DatasetLoader, object()), {XeroDatasetLoader.PREFIX: xero})


def offline_runner(deps: AgentDeps) -> GraphRunner:
    """deps 上的主图: 全用真实的 Agent (不用 tests.fakes 里 Numbat 的 P1 脚本: DOC-04 要沿用上传时抽出的 Xero 合同),
    内存检查点."""
    return GraphRunner(compile_graph(GraphNodes(deps, 2), InMemorySaver(serde=checkpoint_serde())), deps)


async def drain(service: DataService) -> None:
    """等 DataService 的后台作业 (演示导入, 文件处理) 都结束."""
    while service.tasks:
        await asyncio.gather(*list(service.tasks))


async def import_stage(service: DataService, eid: str, stage: DemoStage, actor: str) -> DemoImportResult | None:
    """经 DataService.demo_import 导入一个阶段, 等作业做完, 等图停下; 返回这次记下的 demo_import 留痕的结果: 有写入,
    或上次这一阶段没做完时为 imported, 出错时为 failed; 什么都没写 (上次也做完了) 不记留痕, 为 None."""
    before = await service.deps.trail.last_of(eid, TrailKind.data, [DEMO_JOB])
    await service.demo_import(eid, stage, actor)
    await drain(service)
    await wait_idle(service.runner, eid)
    entry = await service.deps.trail.last_of(eid, TrailKind.data, [DEMO_JOB])
    if entry is None or (before is not None and entry.id == before.id):
        return None
    return DemoImportResult.model_validate({k: v for k, v in entry.payload.items() if k != "action"})


async def to_acceptance_gate(runner: GraphRunner, eid: str) -> None:
    """启动, 停在 ACC-01 闸门."""
    await runner.start(eid)
    await wait_idle(runner, eid)


async def approve_acceptance(runner: GraphRunner, eid: str, actor: str) -> None:
    """ACC-01 approve (绑定评分卡第 1 版), 停在 p1_await_data."""
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(
        eid, acc01_decision(GateVerdict.approve, id="D-ACC-01", decided_by=actor)
    )
    await wait_idle(runner, eid)


async def confirm_ready(service: DataService, eid: str, actor: str) -> None:
    """确认数据已齐 (带理由), 等图继续到下一个停点."""
    await service.confirm_ready(eid, actor, READY_REASON)
    await wait_idle(service.runner, eid)


async def to_ready(service: DataService, eid: str, actor: str) -> None:
    """从启动推到确认数据已齐之后 (DOC-04 等 P1 程序跑完, 停在 PLN 闸门)."""
    await to_acceptance_gate(service.runner, eid)
    await import_stage(service, eid, "acceptance", actor)
    await approve_acceptance(service.runner, eid, actor)
    await import_stage(service, eid, "fieldwork", actor)
    await confirm_ready(service, eid, actor)


def entered_p2_by(entries: Iterable[TrailEntry]) -> list[str | None]:
    """项目进入执行阶段 (到 P2_Executing) 的 transition 留痕各记在哪条程序下: 打开 planning 时为 PLN-15 (计划批准推进,
    需求 R317), 关闭时为装配处 (procedure_id 为空). 走通用例与真实模型变体的汇总共用."""
    return [
        e.procedure_id
        for e in entries
        if e.kind is TrailKind.transition and (e.payload["object"], e.payload["to"]) == ("engagement", "P2_Executing")
    ]


def xero_link(deps: AgentDeps, service: DataService) -> XeroLink:
    """数据接口用的 Xero 连接服务 (假连接存储, 一律未连接; Xero 应用与主密钥置空, 同 tests.fakes.api_client)."""
    blank = get_settings().model_copy(
        update={"XERO_CLIENT_ID": "", "XERO_CLIENT_SECRET": "", "SETTINGS_SECRET_KEY": ""}
    )
    return XeroLink(deps, service, FakeXeroConnectionStore(), blank)


@asynccontextmanager
async def demo_offline(llm: LLMClient) -> AsyncIterator[tuple[GraphRunner, DataService, httpx.AsyncClient]]:
    """离线的演示项目: 能建项目的假项目存储, 空的 Xero 假存储, 规则引擎 FakeRuleEngine(XERO_RULES), 登记 mock 按
    数据集定位, 功能集为演示配置; llm 同时作复核模型. 给出 (runner, 数据服务, 以项目负责人登录的接口客户端). 走通用例
    的 walked 与真实模型变体的 offline_walk 共用 (需求 R353)."""
    store = CreatingEngagementStore()
    datasets = demo_source()
    deps = new_xero_deps(
        trail=FakeTrail(),
        llm=llm,
        review_llm=llm,
        rules=FakeRuleEngine(dict(XERO_RULES)),
        features=DEMO_FEATURES,
        engagement_store=store,
    )
    deps = replace(deps, registry=DatasetRegistrySource(store, datasets.supplement_dir))
    runner = offline_runner(deps)
    service = DataService(deps, runner, demo=datasets)
    async with api_client(runner, NUMBAT_AUDITOR.id, xero=xero_link(deps, service)) as client:
        yield runner, service, client
