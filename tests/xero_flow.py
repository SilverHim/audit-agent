"""Xero 演示项目从启动到确认数据已齐的流程辅助 (需求 R269; R119, R120, R259 以后共用).

只经 GraphRunner, GraphDecisionSubmit 与 DataService 的接口推进, 假存储 (tests/xero_fakes) 与真实库都能用:
start → 停在 ACC-01 闸门 → 演示导入 acceptance → ACC-01 approve → 停在 p1_await_data → 演示导入 fieldwork →
确认数据已齐 → 图继续 (DOC-04 沿用上传时抽出的合同).
"""

import asyncio
from pathlib import Path
from typing import cast

from langgraph.checkpoint.memory import InMemorySaver

from backend.agents.base import AgentDeps
from backend.contracts.data import DemoImportResult, DemoStage
from backend.contracts.models import GateVerdict, TrailKind
from backend.contracts.ports import DatasetLoader
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import RoutingDatasetLoader, XeroDatasetLoader
from backend.services.data_service import DataService
from backend.services.demo_import import DEMO_JOB
from tests.fakes import acc01_decision, wait_idle
from tests.xero_fakes import PLANNING, SNAPSHOT_REPO, SUPPLEMENTS, snapshot_root

READY_REASON = "演示数据已导入"  # 确认数据已齐时的理由 (Xero 录入人为连接器占位值, 审批日志一项待补交)


def demo_source(supplements: Path = SUPPLEMENTS) -> RoutingDatasetLoader:
    """演示导入清单的来源, 同组装根按数据集名分流: xero/ 走不连库的 Xero 载入器 (只用 supplement; 快照取
    tests.xero_fakes 定位到的, 没有时跳过), 其余 (Numbat) 没有补造目录. supplements 可换成补造目录的临时副本."""
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
    """经 DataService.demo_import 导入一个阶段, 等作业做完, 等图停下; 返回这次记下的 demo_import 留痕 (有写入时才有),
    什么都没写为 None."""
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
