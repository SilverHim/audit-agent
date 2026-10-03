"""主图节点. 依赖用 tests/fakes.py 的假实现, 节点函数直接调用, 不经图."""

from datetime import timedelta
from typing import ClassVar

import pytest
from pydantic import ValidationError

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    Phase,
    Procedure,
    ProcedureStatus,
    RunStatus,
    RunTrigger,
    ScheduleRow,
)
from backend.contracts.state import EngagementState, StatePatch, initial_state
from backend.orchestration.nodes import GraphNodes, next_run, save_outputs, update_procedure
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_PROFILE,
    TYPO_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
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


def p0_pack() -> list[Procedure]:
    """项目刚启动时的程序包: ACC-04 与 ACC-01 都是 planned 第 1 版."""
    return [make_procedure("ACC-04"), make_procedure("ACC-01")]


def make_nodes(
    engagement_store: FakeEngagementStore | None = None, output_store: FakeOutputStore | None = None
) -> GraphNodes:
    """审计产出存储缺省预置 p0_pack, 同 runner.start 把固定集第 1 版写进 Store."""
    registry = FakeRegistry({"68900042015": NUMBAT_ABN}, {"900042015": NUMBAT_ASIC})
    if output_store is None:
        output_store = FakeOutputStore(list(p0_pack()))
    return GraphNodes(make_deps(registry, FakeTrail(), engagement_store, output_store), parallel_cap=2)


def p0_state() -> EngagementState:
    """项目刚启动的状态."""
    return initial_state(ENGAGEMENT, p0_pack(), NUMBAT_PROFILE.version)


class SpyAgent(ProcedureAgent[None]):
    """记下收到的 ctx, 返回空片段, 用来检查节点传给 Agent 的上下文."""

    agent_id: ClassVar[AgentId] = AgentId.A2  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"ACC-04"})  # 负责的程序
    ctx: RunContext | None = None  # 最近一次收到的上下文

    async def execute(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
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


# ── 需求 2 第 1 步: 节点返回前在一个事务里写 Store, 状态用 put 返回的对象 ──

LATER = NOW + timedelta(hours=1)


def scorecard(exception_ids: list[str]) -> AcceptanceScorecard:
    return AcceptanceScorecard(
        id="SCORECARD", registry_checks=[], exception_ids=exception_ids, client_profile_version=1
    )


async def test_save_outputs_stores_and_keeps_other_values() -> None:
    store = FakeOutputStore()
    card = scorecard([])
    runs = [make_run("ACC-04", 1), make_run("PLN-07", 1)]
    row = ScheduleRow(layer=0, instance="fw-0-1", procedure_ids=["REV-08"])
    patch = {
        "acceptance_scorecard": card,
        "procedure_runs": runs,
        "exceptions": [],
        "phase": Phase.P1_Planning,
        "client_profile_version": 2,
        "schedule_table": [row],  # 元素不是 Versioned 的列表不入库
    }

    result = await save_outputs(store, ENGAGEMENT.id, patch, actor="A2")

    assert result == patch
    assert store.rows == [card, *runs]


async def test_save_outputs_returns_stored_objects_on_replay() -> None:
    """节点写入 Store 后, 写检查点前中断, 恢复时重跑.

    新生成对象与已存对象 id, version 相同而内容不同, 状态应取已存对象.
    """
    stored_card = scorecard(["X-1"])
    stored_run = make_run().model_copy(update={"status": RunStatus.succeeded, "ended_at": NOW})
    store = FakeOutputStore([stored_card, stored_run])
    replayed = {
        "acceptance_scorecard": scorecard(["X-2"]),
        "procedure_runs": [stored_run.model_copy(update={"ended_at": LATER})],
    }

    result = await save_outputs(store, ENGAGEMENT.id, replayed, actor="A2")

    assert result["acceptance_scorecard"].exception_ids == ["X-1"]
    assert result["procedure_runs"][0].ended_at == NOW
    assert store.rows == [stored_card, stored_run]  # 没有重复写入


async def test_p0_intel_saves_outputs_in_one_transaction() -> None:
    store = FakeOutputStore(list(p0_pack()))
    nodes = make_nodes(FakeEngagementStore([TYPO_PROFILE], [ENGAGEMENT]), store)

    await nodes.p0_intel(p0_state())

    assert len(store.commits) == 1  # 评分卡, 例外, 执行记录与程序新版本在同一次提交里
    assert {(type(o).__name__, o.id, o.version) for o in store.commits[0]} == {
        ("AcceptanceScorecard", "SCORECARD", 1),
        ("AuditException", "X-ACC-04-1-name_match-1", 1),
        ("ProcedureRun", "RUN-ACC-04-1", 1),
        ("Procedure", "ACC-04", 2),
    }


async def test_p0_intel_replay_uses_stored_run() -> None:
    """RUN-ACC-04-1 已在 Store 中 (上次执行已提交, 检查点未写入), 状态中的执行记录应取已存对象."""
    stored_run = make_run().model_copy(update={"status": RunStatus.succeeded, "ended_at": NOW})
    nodes = make_nodes(output_store=FakeOutputStore([*p0_pack(), stored_run]))

    patch = await nodes.p0_intel(p0_state())

    assert patch["procedure_runs"][0].ended_at == NOW
