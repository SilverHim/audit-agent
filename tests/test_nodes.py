"""主图节点. 依赖用 tests/fakes.py 的假实现, 节点函数直接调用, 不经图."""

from typing import ClassVar

import pytest
from pydantic import ValidationError

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId, Phase, ProcedureStatus, RunStatus, RunTrigger
from backend.contracts.state import EngagementState, StatePatch, initial_state
from backend.orchestration.nodes import GraphNodes, next_run, update_procedure
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeRegistry,
    FakeTrail,
    make_deps,
    make_procedure,
    make_run,
)

# ── 3a 执行记录取号, 4c 程序改字段出新版本 ──


def test_next_run_first_run() -> None:
    run = next_run("ACC-04", [], {"window_days": 5}, NOW)

    assert run.id == "RUN-ACC-04-1"
    assert run.procedure_id == "ACC-04"
    assert run.run_no == 1
    assert run.trigger == RunTrigger.initial
    assert run.params_snapshot == {"window_days": 5}
    assert run.started_at == NOW
    assert run.status == RunStatus.running
    assert run.ended_at is None


def test_next_run_counts_only_same_procedure() -> None:
    runs = [make_run("DOC-04", 1), make_run("ACC-04", 1), make_run("DOC-04", 2)]

    run = next_run("ACC-04", runs, {}, NOW)

    assert run.id == "RUN-ACC-04-2"
    assert run.run_no == 2
    assert run.trigger == RunTrigger.rerun


def test_update_procedure_changes_one_and_bumps_version() -> None:
    pack = [make_procedure("ACC-04"), make_procedure("ACC-01", version=2), make_procedure("DOC-04", Phase.P1_Planning)]

    new_pack = update_procedure(pack, "ACC-01", status=ProcedureStatus.completed)

    assert [p.id for p in new_pack] == ["ACC-04", "ACC-01", "DOC-04"]
    assert new_pack[1].status == ProcedureStatus.completed
    assert new_pack[1].version == 3
    assert new_pack[0] is pack[0]  # 其余原样
    assert new_pack[2] is pack[2]
    assert pack[1].status == ProcedureStatus.planned  # 原列表与原对象不变
    assert pack[1].version == 2


def test_update_procedure_changes_several_fields() -> None:
    pack = [make_procedure("REV-02", Phase.P2_Executing)]

    new_pack = update_procedure(pack, "REV-02", status=ProcedureStatus.executing, params={"window_days": 10})

    assert new_pack[0].status == ProcedureStatus.executing
    assert new_pack[0].params == {"window_days": 10}
    assert new_pack[0].version == 2


def test_update_procedure_rejects_misspelled_field() -> None:
    pack = [make_procedure("ACC-01")]

    with pytest.raises(ValidationError):
        update_procedure(pack, "ACC-01", stauts=ProcedureStatus.completed)


# ── 3b execute 与 p0_intel: 注入假登记, 假留痕, 假项目存储, 用真的 ClientIntelAgent ──


def make_nodes(engagements: FakeEngagementStore | None = None) -> GraphNodes:
    registry = FakeRegistry({"68900042015": NUMBAT_ABN}, {"900042015": NUMBAT_ASIC})
    return GraphNodes(make_deps(registry, FakeTrail(), engagements), parallel_cap=2)


def p0_state() -> EngagementState:
    """项目刚启动的状态: 程序包里 ACC-04 与 ACC-01 都是 planned 第 1 版."""
    return initial_state(ENGAGEMENT, [make_procedure("ACC-04"), make_procedure("ACC-01")], NUMBAT_PROFILE.version)


class SpyAgent(ProcedureAgent):
    """记下收到的 ctx, 返回空片段, 用来检查节点传给 Agent 的上下文."""

    agent_id: ClassVar[AgentId] = AgentId.A2  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"ACC-04"})  # 负责的程序
    ctx: RunContext | None = None  # 最近一次收到的上下文

    async def run(self, ctx: RunContext) -> StatePatch:
        self.ctx = ctx
        return {}


async def test_execute_builds_context_for_agent() -> None:
    nodes = make_nodes()
    spy = SpyAgent(nodes.deps)
    nodes.agents["ACC-04"] = spy
    state = p0_state()

    await nodes.execute(state, "ACC-04")

    assert spy.ctx is not None
    assert spy.ctx.engagement == ENGAGEMENT
    assert spy.ctx.procedure.id == "ACC-04"
    assert spy.ctx.procedure is state["procedure_pack"][0]  # 程序包里那条原样传入, 不另建 executing 副本
    assert spy.ctx.template.id == "ACC-04"
    assert spy.ctx.run.id == "RUN-ACC-04-1"
    assert spy.ctx.run.status == RunStatus.running
    assert spy.ctx.state is state


async def test_p0_intel_runs_acc04() -> None:
    patch = await make_nodes().p0_intel(p0_state())

    assert set(patch) == {"acceptance_scorecard", "exceptions", "procedure_runs", "procedure_pack"}
    assert patch["acceptance_scorecard"].version == 1
    assert [r.id for r in patch["procedure_runs"]] == ["RUN-ACC-04-1"]
    assert patch["procedure_runs"][0].status == RunStatus.succeeded
    pack = patch["procedure_pack"]
    assert [p.id for p in pack] == ["ACC-04", "ACC-01"]
    assert pack[0].status == ProcedureStatus.completed
    assert pack[0].version == 2
    assert pack[1].status == ProcedureStatus.planned  # ACC-01 由闸门决定置 completed, 这里不动
    assert pack[1].version == 1


async def test_p0_intel_second_time_is_rerun() -> None:
    """ACC-01 选信息不足后回到 p0_intel: 执行记录取第 2 号, 评分卡与程序各出新版本."""
    nodes = make_nodes()
    state = p0_state()
    first = await nodes.p0_intel(state)
    state["procedure_runs"] = first["procedure_runs"]
    state["acceptance_scorecard"] = first["acceptance_scorecard"]
    state["procedure_pack"] = first["procedure_pack"]

    second = await nodes.p0_intel(state)

    assert second["procedure_runs"][0].id == "RUN-ACC-04-2"
    assert second["procedure_runs"][0].trigger == RunTrigger.rerun
    assert second["acceptance_scorecard"].version == 2
    assert second["procedure_pack"][0].version == 3


async def test_execute_raises_when_engagement_missing() -> None:
    nodes = make_nodes(FakeEngagementStore([NUMBAT_PROFILE], []))

    with pytest.raises(LookupError):
        await nodes.execute(p0_state(), "ACC-04")
