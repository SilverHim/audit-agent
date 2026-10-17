"""验收: P2 中途中断 (进程被杀, 或写 Store 后未写检查点), 用同一检查点与存储新建 runner 调 recover 后继续,
已持久化的程序不重跑, 函证不重复发出, 结果与不中断时 (test_e2e_mock) 一致.

"已持久化" 的边界: 程序终态已写入 Store, 或其所在 fieldwork 实例的结果已存进检查点, 恢复后不再执行 Agent.
同一实例内已执行但实例结果尚未保存的程序允许重跑 (Run 编号不变, 大模型会多调); 发函仍由成功留痕的幂等键保证不重复.
"""

import asyncio
from typing import Any

import pytest

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.ingest import Extracted, IngestAgent
from backend.contracts.models import TrailEntry, TrailKind
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner
from tests.fakes import ENGAGEMENT, FakeConfirmationChannel, FakeTrail, make_graph_nodes, wait_idle
from tests.test_decisions import Started
from tests.test_e2e_mock import (
    assert_fixtures_used_once,
    assert_pointers_resolve,
    assert_results,
    assert_trail_counts,
    finish_demo,
    real_agents,
    start_demo,
)
from tests.test_flow import until

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)
LAYER0 = ["REV-08", "REV-03", "CSH-04", "CSH-05"]


async def kill(s: Started) -> None:
    """模拟进程被杀: 取消后台任务, 不做任何收尾."""
    task = s.runner.tasks[EID]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def restart(s: Started) -> Started:
    """用同一检查点与存储 (同一套依赖) 新建 runner, 调 recover (应用启动时的恢复); Agent 都是真实的."""
    deps = s.runner.deps
    nodes = make_graph_nodes(deps)
    nodes.agents.update(real_agents(deps))
    runner = GraphRunner(compile_graph(nodes, s.runner.app.checkpointer), deps)
    await runner.recover()
    return Started(runner, s.output_store, s.engagement_store)


async def assert_same_as_uninterrupted(s: Started, channel: FakeConfirmationChannel) -> None:
    """验收 1, 2: 结果, 留痕行数与证据指针同不中断时; 函证只发 2 次; 第 0 层程序都只有第 1 次执行."""
    await assert_results(s)
    assert_trail_counts(trail_of(s).entries)  # 含 CSH-04 的 external_call 2 行, llm_call 15 行 (含 GEN-09 两轮)
    await assert_pointers_resolve(s)
    assert_fixtures_used_once(trail_of(s).entries)
    assert len(channel.submits) == 2
    values = (await s.runner.app.aget_state(CONFIG)).values
    runs = sorted(r.id for r in values["procedure_runs"] if r.procedure_id in LAYER0)
    assert runs == sorted(f"RUN-{pid}-1" for pid in LAYER0)


def trail_of(s: Started) -> FakeTrail:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return trail


# ── 进程在 DOC-06 执行中被杀 (Q7 选 1: 第 0 层汇合后先派发 DOC-06) ─────────────────────────────────────────


class BlockingIngest(IngestAgent):
    """DOC-06 第一次执行在调大模型之前卡住 (entered 置位), 之后照常; 其余程序照常."""

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        self.entered = asyncio.Event()  # 第一次 DOC-06 已进入执行

    async def execute(self, ctx: RunContext) -> list[Extracted]:
        if ctx.procedure.id == "DOC-06" and not self.entered.is_set():
            self.entered.set()
            await asyncio.Event().wait()
        return await super().execute(ctx)


async def test_killed_during_doc06_recovers_without_resending() -> None:
    """验收 1, 2: 第 0 层汇合后 DOC-06 执行中进程被杀; 新 runner recover 后跑到归档, 结果同不中断时,
    第 0 层不重跑, 函证不重发."""
    holder: dict[str, BlockingIngest] = {}

    def agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        holder["DOC-06"] = BlockingIngest(deps)
        return {**real_agents(deps), "DOC-06": holder["DOC-06"]}

    channel = FakeConfirmationChannel()
    s = await start_demo(agents, confirmations=channel)
    await asyncio.wait_for(holder["DOC-06"].entered.wait(), timeout=5)
    values = (await s.runner.app.aget_state(CONFIG)).values
    assert sorted(r.id for r in values["procedure_runs"] if r.procedure_id in LAYER0) == sorted(
        f"RUN-{pid}-1" for pid in LAYER0
    )  # 第 0 层已汇合进检查点
    await kill(s)

    s = await restart(s)
    await finish_demo(s)

    await assert_same_as_uninterrupted(s, channel)


# ── fan_in 写 Store 后, 写检查点前抛错 ─────────────────────────────────────────


async def test_fan_in_fails_after_store_write_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 3: 第一次 fan_in 写完 Store 后抛错 (检查点没记下汇合); recover 后重做汇合 (Store 中已有的对象 put 跳过),
    各 fieldwork 实例不重跑, 最终大模型调用行数同不中断时."""
    original = GraphNodes.fan_in
    failed: list[str] = []

    async def fan_in(self: GraphNodes, state: EngagementState) -> StatePatch:
        patch = await original(self, state)
        if not failed:
            failed.append("fan_in")
            raise RuntimeError("fan_in 写 Store 后, 写检查点前中断")
        return patch

    monkeypatch.setattr(GraphNodes, "fan_in", fan_in)
    channel = FakeConfirmationChannel()
    s = await start_demo(confirmations=channel)
    await wait_idle(s.runner, EID)
    assert failed and "写检查点前中断" in (s.runner.run_error(EID) or "")
    assert (await s.runner.app.aget_state(CONFIG)).next == ("fan_in",)  # 检查点停在汇合之前

    s = await restart(s)
    await finish_demo(s)  # 第 0 层不重跑: 最终每份 fixture 只读一次, 第 0 层只有第 1 次执行

    await assert_same_as_uninterrupted(s, channel)


# ── CSH-04 发函并写 external_call 后, Store 提交前进程被杀 ─────────────────────────────────────────


class HangAfterFirstConfirmation(FakeTrail):
    """第一行 CSH-04 的 external_call 写入后卡住 (模拟随即被杀): 回执已留痕, 函证尚未进 Store 与检查点."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()  # 第一份函证已留痕

    async def record(self, entry: TrailEntry) -> str:
        trail_id = await super().record(entry)
        if entry.kind is TrailKind.external_call and entry.procedure_id == "CSH-04" and not self.entered.is_set():
            self.entered.set()
            await asyncio.Event().wait()
        return trail_id


async def test_killed_after_confirmation_sent_does_not_resend() -> None:
    """验收 4: BA-01 发函并留痕后被杀; recover 后 CSH-04 重新执行, BA-01 按留痕的幂等键命中, 不再发函;
    函证共发 2 次, CSH-04 的 external_call 共 2 行."""
    trail, channel = HangAfterFirstConfirmation(), FakeConfirmationChannel()
    s = await start_demo(trail=trail, confirmations=channel)
    await asyncio.wait_for(trail.entered.wait(), timeout=5)

    async def revenue_instance_saved() -> bool:  # 另一个实例 (收入线) 已存进检查点, 被杀时不在执行中
        snapshot = await s.runner.app.aget_state(CONFIG)
        return any(t.name == "fieldwork" and t.result is not None for t in snapshot.tasks)

    await until(revenue_instance_saved)
    await kill(s)
    assert len(channel.submits) == 1

    s = await restart(s)
    await finish_demo(s)

    assert [key for key, _ in channel.submits].count(channel.submits[0][0]) == 1  # BA-01 没有再发
    await assert_same_as_uninterrupted(s, channel)
