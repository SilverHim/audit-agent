"""主图节点. 依赖用 tests/fakes.py 的假实现; 节点函数直接调用, 要 interrupt() 的经单节点图调用."""

import asyncio
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, ClassVar

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import ValidationError

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.errors import RuleError
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AgentId,
    AuditObjective,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewTask,
    RunStatus,
    RunTrigger,
    ScheduleRow,
    TargetType,
    TaskKind,
    TaskStatus,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.state import EngagementState, StatePatch, initial_state
from backend.core.config import get_settings
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.nodes import GraphNodes, build_run, fail_run, next_run, save_outputs, update_procedure
from backend.orchestration.runner import GraphRunner
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_OBJECTIVES,
    NUMBAT_PROFILE,
    TYPO_PROFILE,
    FakeDatasetLoader,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    ScriptA3,
    ScriptAgent,
    finished,
    make_deps,
    make_procedure,
    make_run,
    make_runner,
    wait_idle,
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
    """审计产出存储缺省预置 p0_pack, 同 runner.start 把固定集第 1 版写进 Store. 同 make_graph_nodes 按种子已载入
    Numbat 登记 3 份合同: DOC-04 每次执行前查有没有合同 (需求 R272)."""
    registry = FakeRegistry({"68900042015": NUMBAT_ABN}, {"900042015": NUMBAT_ASIC})
    if output_store is None:
        output_store = FakeOutputStore(list(p0_pack()))
    deps = make_deps(registry, FakeTrail(), engagement_store, output_store)
    assert isinstance(deps.datasets, FakeDatasetLoader)
    deps.datasets.seed()
    return GraphNodes(deps, parallel_cap=2)


def p0_state() -> EngagementState:
    """项目刚启动的状态."""
    return initial_state(ENGAGEMENT, p0_pack(), NUMBAT_PROFILE.version)


class SpyAgent(ProcedureAgent[None]):
    """记下收到的 ctx, 只返回本次执行的终态, 用来检查节点传给 Agent 的上下文."""

    agent_id: ClassVar[AgentId] = AgentId.A2  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"ACC-04"})  # 负责的程序
    ctx: RunContext | None = None  # 最近一次收到的上下文

    async def execute(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        return {"procedure_runs": [finished(ctx.run)]}


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
    assert spy.ctx.run.params_snapshot == {}  # ACC-04 模板没有参数
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


# ── R01 建执行记录: 模板参数按项目与状态解析, 再叠加 rerun 改的参数 ──

PERIOD_END = date(2026, 6, 30)


def library() -> ProcedureLibrary:
    return ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)


def numbat_state(materiality_set: bool = True) -> EngagementState:
    """Numbat 进入 P2 时的状态: 审计目标按数据集预置; 重要性取演示中闸门批准的值 (合成数据集设计.md 第 7 节)."""
    state = initial_state(ENGAGEMENT, [], NUMBAT_PROFILE.version)
    state["audit_objectives"] = list(NUMBAT_OBJECTIVES)
    if materiality_set:
        state["materiality_cents"] = 12_000_000
        state["performance_materiality_cents"] = 9_000_000
        state["clearly_trivial_cents"] = 600_000
    return state


def assembled(
    lib: ProcedureLibrary,
    procedure_id: str,
    objectives: list[AuditObjective] = NUMBAT_OBJECTIVES,
    **changes: Any,
) -> Procedure:
    """同 assemble_pack 实例化一条程序: objective_ids 为 trigger 匹配到的审计目标. changes 覆盖其他字段."""
    template = lib.load(procedure_id)
    return Procedure(
        id=procedure_id,
        phase=template.phase,
        template_commit=lib.commit,
        objective_ids=[o.id for o in objectives if template in lib.match(o)],
        autonomy_level=template.autonomy_level,
        **changes,
    )


@pytest.mark.parametrize(
    ("procedure_id", "expected"),
    [
        ("REV-08", {"window_days": 20, "period_end": PERIOD_END}),
        ("REV-02", {"window_days": 20, "threshold_cents": 9_000_000, "period_end": PERIOD_END}),
        ("CSH-04", {"as_of": PERIOD_END}),
        ("CSH-02", {"period_end": PERIOD_END, "subsequent_days": 10, "lookback_days": 5}),
        ("FIN-04", {"materiality_cents": 12_000_000, "clearly_trivial_cents": 600_000}),
    ],
)
def test_build_run_resolves_numbat_params(procedure_id: str, expected: dict[str, Any]) -> None:
    """Numbat 的收入目标都是特别风险, window_days 取 20; ref 取项目期末与闸门批准的重要性."""
    lib = library()

    run = build_run(
        lib,
        lib.load(procedure_id),
        assembled(lib, procedure_id),
        ENGAGEMENT,
        numbat_state(),
        NOW,
        traits=DatasetTraits(),
        profile=None,
    )

    assert run.params_snapshot == expected
    assert run.id == f"RUN-{procedure_id}-1"
    assert run.status == RunStatus.running


def test_build_run_literal_params_equal_yaml() -> None:
    lib = library()

    run = build_run(
        lib,
        lib.load("PLN-07"),
        assembled(lib, "PLN-07"),
        ENGAGEMENT,
        numbat_state(),
        NOW,
        traits=DatasetTraits(),
        profile=None,
    )

    assert run.params_snapshot == lib.load("PLN-07").params


def test_build_run_uses_highest_linked_risk() -> None:
    """REV-08 关联中风险与高风险两条收入目标: 取高风险的 15 天; 货币资金的特别风险不关联, 不取 20 天."""
    objectives = [
        AuditObjective(
            id="OBJ-REV-OCC", account="Revenue", assertion="Occurrence", risk_level="medium", is_material=True
        ),
        AuditObjective(id="OBJ-REV-CUT", account="Revenue", assertion="Cutoff", risk_level="high", is_material=True),
        AuditObjective(
            id="OBJ-CASH-EXI", account="Cash", assertion="Existence", risk_level="significant", is_material=True
        ),
    ]
    state = numbat_state()
    state["audit_objectives"] = objectives
    lib = library()

    run = build_run(
        lib,
        lib.load("REV-08"),
        assembled(lib, "REV-08", objectives),
        ENGAGEMENT,
        state,
        NOW,
        traits=DatasetTraits(),
        profile=None,
    )

    assert run.params_snapshot["window_days"] == 15


def test_build_run_rerun_params_override_template() -> None:
    """rerun 决定把 window_days 改为 25: 同名键以 Procedure.params 为准, 其余照常解析; 执行记录取第 2 号."""
    state = numbat_state()
    state["procedure_runs"] = [make_run("REV-08", 1)]
    lib = library()
    procedure = assembled(lib, "REV-08", params={"window_days": 25})

    run = build_run(lib, lib.load("REV-08"), procedure, ENGAGEMENT, state, NOW, traits=DatasetTraits(), profile=None)

    assert run.params_snapshot == {"window_days": 25, "period_end": PERIOD_END}
    assert run.id == "RUN-REV-08-2"
    assert run.trigger == RunTrigger.rerun


def test_build_run_snapshot_not_shared_with_procedure() -> None:
    """rerun 把 PLN-07 的收入科目改为 ["other_income"]: 改第 2 次执行快照里的列表, 程序与第 1 次快照不受影响."""
    state = numbat_state()
    lib = library()
    template = lib.load("PLN-07")
    procedure = assembled(lib, "PLN-07", params={"revenue_categories": ["other_income"]})
    first = build_run(lib, template, procedure, ENGAGEMENT, state, NOW, traits=DatasetTraits(), profile=None)
    state["procedure_runs"] = [first]
    second = build_run(lib, template, procedure, ENGAGEMENT, state, NOW, traits=DatasetTraits(), profile=None)

    second.params_snapshot["revenue_categories"].append("cost_of_sales")

    assert procedure.params["revenue_categories"] == ["other_income"]
    assert first.params_snapshot["revenue_categories"] == ["other_income"]
    assert template.params["revenue_categories"] == ["revenue"]


def test_build_run_refuses_unset_materiality() -> None:
    """重要性未经闸门确定 (状态中为 None) 时执行 FIN-04: 抛错, 不建执行记录."""
    lib = library()

    with pytest.raises(ValueError, match="materiality_cents"):
        build_run(
            lib,
            lib.load("FIN-04"),
            assembled(lib, "FIN-04"),
            ENGAGEMENT,
            numbat_state(False),
            NOW,
            traits=DatasetTraits(),
            profile=None,
        )


def test_build_run_by_risk_without_objectives() -> None:
    """REV-08 没有关联审计目标, 无从按风险取 window_days: 抛错."""
    lib = library()

    with pytest.raises(ValueError, match=r"REV-08\.window_days"):
        build_run(
            lib,
            lib.load("REV-08"),
            assembled(lib, "REV-08", []),
            ENGAGEMENT,
            numbat_state(),
            NOW,
            traits=DatasetTraits(),
            profile=None,
        )


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


# ── R03 P2 以外程序失败: 转失败执行与 failed 任务, 节点内等人处理 ──

EID = ENGAGEMENT.id


def boom(ctx: RunContext) -> StatePatch:
    raise RuntimeError("boom")


def rule_error(ctx: RunContext) -> StatePatch:
    raise RuleError("REV-08.R01: SQL 超时")


def scorecard_with_judgments(ctx: RunContext) -> StatePatch:
    """A2 越权写 judgments: 合并校验不过."""
    return {"acceptance_scorecard": scorecard([]), "judgments": [], "procedure_runs": [finished(ctx.run)]}


def single_node(nodes: GraphNodes, procedure_id: str) -> CompiledStateGraph[EngagementState]:
    """只有一个节点的图: 节点即 nodes.execute(state, procedure_id). interrupt() 只能在图里调用, 故用它测等待."""
    graph = StateGraph(EngagementState)

    async def step(state: EngagementState) -> StatePatch:
        return await nodes.execute(state, procedure_id)

    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=InMemorySaver(serde=checkpoint_serde()))


def test_fail_run_builds_failed_run_and_task() -> None:
    nodes = make_nodes()

    patch = fail_run(nodes.deps.queue, make_run(), "RuntimeError: boom", NOW)

    run = patch["procedure_runs"][0]
    assert (run.id, run.status, run.failure_reason, run.ended_at) == (
        "RUN-ACC-04-1",
        RunStatus.failed,
        "RuntimeError: boom",
        NOW,
    )
    assert run.started_at == make_run().started_at and run.version == 1  # 其余字段不变
    task = patch["review_tasks"][0]
    assert task == ReviewTask(
        id="T-RUN-ACC-04-1-v1",
        kind=TaskKind.failed,
        procedure_id="ACC-04",
        target_type=TargetType.ProcedureRun,
        target_id="RUN-ACC-04-1",
        target_version=1,
        run_id="RUN-ACC-04-1",
        subject_id=None,
        status=TaskStatus.pending,
        created_at=NOW,
    )
    assert set(patch) == {"procedure_runs", "review_tasks"}


async def test_run_agent_returns_valid_patch_as_is() -> None:
    nodes = make_nodes()
    ctx = RunContext(
        engagement=ENGAGEMENT,
        procedure=p0_pack()[0],
        template=nodes.deps.library.load("ACC-04"),
        run=make_run(),
        state=p0_state(),
    )
    patch = {"acceptance_scorecard": scorecard([]), "exceptions": [], "procedure_runs": [finished(make_run())]}

    result, writer = await nodes.run_agent(ScriptAgent(nodes.deps, lambda ctx: patch), ctx)

    assert result is patch
    assert writer is AgentId.A2


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        (boom, "RuntimeError: boom"),
        (rule_error, "RuleError: REV-08.R01: SQL 超时"),
        (scorecard_with_judgments, "merge_rejected: A2 不能写字段 judgments"),
    ],
)
async def test_failure_writes_failed_run_task_and_waits(script: Any, reason: str) -> None:
    """验收 1, 2: Agent 抛异常 (含 RuleError) 或片段校验不过: 不向上抛, 一次提交写失败执行, failed 任务与
    ACC-04 的 awaiting_human 新版本, Agent 片段不写入; 然后停在节点内 interrupt()."""
    store = FakeOutputStore(list(p0_pack()))
    nodes = make_nodes(output_store=store)
    nodes.agents["ACC-04"] = ScriptAgent(nodes.deps, script)
    app = single_node(nodes, "ACC-04")
    config = GraphRunner.config(EID)

    result = await app.ainvoke(p0_state(), config)

    assert [i.value for i in result["__interrupt__"]] == [
        {"awaiting": "human", "procedure_id": "ACC-04", "run_id": "RUN-ACC-04-1", "records": []}
    ]
    assert len(store.commits) == 1
    run, task, procedure = store.commits[0]
    assert isinstance(run, ProcedureRun)
    assert (run.id, run.status, run.failure_reason, run.ended_at is not None) == (
        "RUN-ACC-04-1",
        RunStatus.failed,
        reason,
        True,
    )
    assert isinstance(task, ReviewTask)
    assert (task.id, task.kind, task.target_type, task.target_id, task.subject_id, task.status) == (
        "T-RUN-ACC-04-1-v1",
        TaskKind.failed,
        TargetType.ProcedureRun,
        "RUN-ACC-04-1",
        None,
        TaskStatus.pending,
    )
    assert isinstance(procedure, Procedure)
    assert (procedure.id, procedure.version, procedure.status) == ("ACC-04", 2, ProcedureStatus.awaiting_human)
    snapshot = await app.aget_state(config)
    assert snapshot.next == ("step",)  # 停在节点内, 本次返回值未进状态
    assert snapshot.values["procedure_runs"] == []
    assert snapshot.values["acceptance_scorecard"] is None


async def test_agent_patch_with_pending_failed_task_is_written_then_waits() -> None:
    """验收 4: DOC-04 一份合同失败, Agent 返回 succeeded 执行与带 subject_id 的 failed 任务: 片段照常写入, 然后等待."""
    pack = [make_procedure("DOC-04", Phase.P1_Planning)]
    store = FakeOutputStore(list(pack))
    nodes = make_nodes(output_store=store)

    def one_contract_failed(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        task = nodes.deps.queue.make_task(
            TaskKind.failed, run, procedure_id="DOC-04", run_id=run.id, now=NOW, subject_id="CON-002"
        )
        return {"procedure_runs": [run], "review_tasks": [task]}

    nodes.agents["DOC-04"] = ScriptA3(nodes.deps, one_contract_failed)
    app = single_node(nodes, "DOC-04")

    result = await app.ainvoke(initial_state(ENGAGEMENT, pack, 1), GraphRunner.config(EID))

    assert "__interrupt__" in result
    run, task, procedure = store.commits[0]
    assert (run.id, run.status) == ("RUN-DOC-04-1", RunStatus.succeeded)
    assert (task.id, task.subject_id, task.status) == ("T-RUN-DOC-04-1-CON-002-v1", "CON-002", TaskStatus.pending)
    assert (procedure.id, procedure.version, procedure.status) == ("DOC-04", 2, ProcedureStatus.awaiting_human)


class TransientRegistry(FakeRegistry):
    """第一次查 ABN 抛错, 之后恢复正常: 用来测恢复时不再调 Agent."""

    def __init__(self) -> None:
        super().__init__({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC})
        self.calls = 0  # ABN 查询次数

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("临时登记故障")
        return await super().abn(eid, abn)


@pytest.mark.parametrize("resume", [None, Command(resume="D-1")], ids=["ainvoke_none", "resume_with_decision"])
async def test_resume_after_failure_does_not_call_agent_again(resume: Command[str] | None) -> None:
    """失败后恢复, 节点从头执行: 本次执行在 Store 中已失败, 不再调 Agent (这次会成功), 照 Store 再走等待.
    不带决定恢复则再次停在同一 interrupt(); 带决定 ID 恢复则去 Store 读该决定, 这里没有预置,
    报决定不存在 (Codex R03-01)."""
    registry = TransientRegistry()
    runner, store = make_runner(registry=registry)
    config = GraphRunner.config(EID)
    await runner.start(EID)
    await wait_idle(runner, EID)
    rows = list(store.rows)

    if resume is None:
        await runner.app.ainvoke(None, config)
    else:
        with pytest.raises(LookupError, match="D-1"):
            await runner.app.ainvoke(resume, config)

    assert registry.calls == 1
    assert store.rows == rows  # 没有新写入: 无评分卡, 执行记录仍为 failed, ACC-04 仍为 awaiting_human v2
    snapshot = await runner.app.aget_state(config)
    assert [task.name for task in snapshot.tasks] == ["p0_intel"]  # 仍停在 p0_intel, 没有进 ACC-01 闸门
    assert snapshot.values["procedure_runs"] == []
    if resume is None:
        assert snapshot.next == ("p0_intel",)  # 再次停在同一 interrupt()
    else:
        assert "D-1" in str(snapshot.tasks[0].error)  # 节点出错时 LangGraph 的 next 为空, 出错的任务记在 tasks 里


async def test_resume_keeps_waiting_when_failed_task_would_disappear() -> None:
    """DOC-04 第一次带单对象失败任务, 再调会不带: 恢复时不调 Agent, 仍按 Store 中的任务等待."""
    pack = [make_procedure("DOC-04", Phase.P1_Planning)]
    store = FakeOutputStore(list(pack))
    nodes = make_nodes(output_store=store)

    def first_time_fails_one(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        if agent.calls > 1:
            return {"procedure_runs": [run]}
        task = nodes.deps.queue.make_task(TaskKind.failed, run, "DOC-04", run.id, NOW, subject_id="CON-002")
        return {"procedure_runs": [run], "review_tasks": [task]}

    agent = ScriptA3(nodes.deps, first_time_fails_one)
    nodes.agents["DOC-04"] = agent
    app = single_node(nodes, "DOC-04")
    config = GraphRunner.config(EID)
    await app.ainvoke(initial_state(ENGAGEMENT, pack, 1), config)

    with pytest.raises(LookupError, match="D-1"):
        await app.ainvoke(Command(resume="D-1"), config)

    assert agent.calls == 1
    assert [(type(r).__name__, r.id, r.version) for r in store.rows] == [
        ("Procedure", "DOC-04", 1),
        ("ProcedureRun", "RUN-DOC-04-1", 1),
        ("ReviewTask", "T-RUN-DOC-04-1-CON-002-v1", 1),
        ("Procedure", "DOC-04", 2),
    ]  # 恢复后没有新写入


async def test_failure_written_by_a1_agent_patch_by_agent() -> None:
    """写入者: 失败执行与任务记 A1; Agent 的片段记 Agent 编号; 程序新版本一律记 A1. 任务创建时间即失败时间."""

    class ActorStore(FakeOutputStore):
        def __init__(self, rows: list[Procedure]) -> None:
            super().__init__(list(rows))
            self.actors: list[tuple[list[str], str]] = []  # (写入的对象 ID, 写入者)

        async def put(self, eid: str, objs: Any, *, actor: str) -> Any:
            self.actors.append(([obj.id for obj in objs], actor))
            return await super().put(eid, objs, actor=actor)

    failing = ActorStore(p0_pack())
    nodes = make_nodes(output_store=failing)
    nodes.agents["ACC-04"] = ScriptAgent(nodes.deps, boom)
    await single_node(nodes, "ACC-04").ainvoke(p0_state(), GraphRunner.config(EID))
    run, task, _ = failing.commits[0]
    assert run.ended_at == task.created_at
    assert failing.actors == [
        (["RUN-ACC-04-1"], AgentId.A1),
        (["T-RUN-ACC-04-1-v1"], AgentId.A1),
        (["ACC-04"], AgentId.A1),
    ]

    passing = ActorStore(p0_pack())
    nodes = make_nodes(output_store=passing)
    nodes.agents["ACC-04"] = ScriptAgent(nodes.deps, lambda ctx: {"procedure_runs": [finished(ctx.run)]})
    await nodes.execute(p0_state(), "ACC-04")
    assert passing.actors == [(["RUN-ACC-04-1"], AgentId.A2), (["ACC-04"], AgentId.A1)]


async def test_cancelled_agent_writes_nothing() -> None:
    """取消不是执行失败: CancelledError 原样抛出, 不写失败执行与任务."""
    store = FakeOutputStore(p0_pack())
    nodes = make_nodes(output_store=store)

    def cancelled(ctx: RunContext) -> StatePatch:
        raise asyncio.CancelledError

    nodes.agents["ACC-04"] = ScriptAgent(nodes.deps, cancelled)

    with pytest.raises(asyncio.CancelledError):
        await nodes.execute(p0_state(), "ACC-04")
    assert store.commits == []


async def test_graph_interrupt_from_agent_is_not_a_failure() -> None:
    """LangGraph 的中断信号不是执行失败, 原样抛出."""
    nodes = make_nodes()

    def interrupts(ctx: RunContext) -> StatePatch:
        raise GraphInterrupt(())

    ctx = RunContext(
        engagement=ENGAGEMENT,
        procedure=p0_pack()[0],
        template=nodes.deps.library.load("ACC-04"),
        run=make_run(),
        state=p0_state(),
    )

    with pytest.raises(GraphInterrupt):
        await nodes.run_agent(ScriptAgent(nodes.deps, interrupts), ctx)


async def test_store_failure_is_a_node_error() -> None:
    """验收 6: 节点自身的错误 (此处写 Store 失败) 照常抛出, 不转 failed 任务."""

    class BrokenStore(FakeOutputStore):
        async def put(self, eid: str, objs: Any, *, actor: str) -> Any:
            raise RuntimeError("Store 不可用")

    nodes = make_nodes(output_store=BrokenStore())

    with pytest.raises(RuntimeError, match="Store 不可用"):
        await nodes.execute(p0_state(), "ACC-04")


# ── R95: 参数引用项目数据集的声明 ──

XERO_DATASET = "xero/20261017-q1"
XERO_LIKE = ENGAGEMENT.model_copy(update={"dataset": XERO_DATASET})  # 数据集为 Xero 演示的项目 (其余同 Numbat)
REF_PARAMS = {"a": date(2025, 7, 1), "b": ["477"], "c": "AJE-"}  # 测试模板按第 5.2 节声明解析的参数


class RefSpy(ProcedureAgent[None]):
    """测试模板 X-REF 的 Agent: 记下收到的执行记录, 只返回本次执行的终态."""

    agent_id: ClassVar[AgentId] = AgentId.A4  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"X-REF"})  # 负责的程序
    runs: list[ProcedureRun]  # 收到的执行记录

    async def execute(self, ctx: RunContext) -> None:
        self.runs.append(ctx.run)

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        return {"procedure_runs": [finished(ctx.run)]}


def ref_nodes(tmp_path: Any) -> tuple[GraphNodes, FakeDatasetLoader, RefSpy, FakeOutputStore]:
    """程序库带测试模板 X-REF 的节点; 数据集载入对 Xero 演示数据集返回第 5.2 节的声明; Store 预置 X-REF 第 1 版."""
    from tests.test_knowledge import design_traits, ref_library

    output_store = FakeOutputStore([make_procedure("X-REF", Phase.P1_Planning)])
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [XERO_LIKE])
    deps = make_deps(FakeRegistry(), FakeTrail(), engagement_store, output_store, library=ref_library(tmp_path))
    loader = FakeDatasetLoader(deps.financial_store, traits={XERO_DATASET: design_traits()})
    nodes = GraphNodes(replace(deps, datasets=loader), parallel_cap=2)
    spy = RefSpy(nodes.deps)
    spy.runs = []
    nodes.agents["X-REF"] = spy
    return nodes, loader, spy, output_store


def ref_state() -> EngagementState:
    return initial_state(XERO_LIKE, [make_procedure("X-REF", Phase.P1_Planning)], NUMBAT_PROFILE.version)


async def test_execute_and_fieldwork_resolve_dataset_refs(tmp_path: Any) -> None:
    """建执行记录时按项目的数据集取声明, 解析 dataset. 的 ref; execute 与 fieldwork 结果相同."""
    nodes, loader, spy, _ = ref_nodes(tmp_path / "execute")
    patch = await nodes.execute(ref_state(), "X-REF")
    [run] = patch["procedure_runs"]
    assert (run.id, run.params_snapshot) == ("RUN-X-REF-1", REF_PARAMS)
    assert spy.runs[0].params_snapshot == REF_PARAMS and loader.traits_calls == [XERO_DATASET]

    nodes, loader, spy, _ = ref_nodes(tmp_path / "fieldwork")
    staged = await nodes.fieldwork({"instance": "fw-0-0", "procedure_ids": ["X-REF"], "state": ref_state()})
    [result] = staged["staged_results"]
    [run] = result["patch"]["procedure_runs"]
    assert (run.id, run.instance, run.params_snapshot) == ("RUN-X-REF-1", "fw-0-0", REF_PARAMS)
    assert loader.traits_calls == [XERO_DATASET]


async def test_replay_keeps_stored_params_when_traits_change(tmp_path: Any) -> None:
    """重放 (Store 已有终态执行): 照常先建执行记录 (取一次声明), 取回的执行以 Store 中的参数为准, 声明改了也不影响."""
    nodes, loader, spy, store = ref_nodes(tmp_path)
    await nodes.execute(ref_state(), "X-REF")
    loader.traits[XERO_DATASET] = DatasetTraits()  # 声明在两次之间被改

    patch = await nodes.execute(ref_state(), "X-REF")
    [run] = patch["procedure_runs"]
    assert run.params_snapshot == REF_PARAMS and len(spy.runs) == 1  # 不再调 Agent
    assert loader.traits_calls == [XERO_DATASET, XERO_DATASET]
    stored = await store.get(XERO_LIKE.id, ProcedureRun, "RUN-X-REF-1")
    assert stored is not None and stored.params_snapshot == REF_PARAMS


# R127: 画像的两个版本; 第 2 版由同客户的另一个项目改出 (同 PUT /client-profile 的效果)
PROFILE_V1 = NUMBAT_PROFILE.model_copy(update={"approval_limit_cents": 500000})
PROFILE_V2 = PROFILE_V1.model_copy(update={"version": 2, "approval_limit_cents": 600000})


def profile_nodes(tmp_path: Any) -> tuple[GraphNodes, RefSpy, FakeEngagementStore]:
    """程序库带测试模板 X-REF (参数 limit 引 profile.approval_limit_cents) 的节点; 项目存储有画像第 1, 2 版."""
    from tests.test_knowledge import REF_TEMPLATE, ref_library

    template = REF_TEMPLATE.split("params:")[0] + "params:\n  limit: {ref: profile.approval_limit_cents}\n"
    output_store = FakeOutputStore([make_procedure("X-REF", Phase.P1_Planning)])
    engagement_store = FakeEngagementStore([PROFILE_V1, PROFILE_V2], [XERO_LIKE])
    library = ref_library(tmp_path, template)
    nodes = GraphNodes(make_deps(FakeRegistry(), FakeTrail(), engagement_store, output_store, library=library), 2)
    spy = RefSpy(nodes.deps)
    spy.runs = []
    nodes.agents["X-REF"] = spy
    return nodes, spy, engagement_store


def profile_state(version: int) -> EngagementState:
    return initial_state(XERO_LIKE, [make_procedure("X-REF", Phase.P1_Planning)], version)


async def test_profile_refs_use_the_adopted_version(tmp_path: Any) -> None:
    """建执行记录时按 state 的 client_profile_version 取画像解析 profile. 的 ref: 本项目采用第 1 版时为 500000,
    同客户已有第 2 版 (600000) 也不受影响; execute 与 fieldwork 相同; 采用第 2 版时为 600000."""
    nodes, spy, _ = profile_nodes(tmp_path / "execute")
    [run] = (await nodes.execute(profile_state(1), "X-REF"))["procedure_runs"]
    assert run.params_snapshot == {"limit": 500000} and spy.runs[0].params_snapshot == {"limit": 500000}

    nodes, _, _ = profile_nodes(tmp_path / "fieldwork")
    staged = await nodes.fieldwork({"instance": "fw-0-0", "procedure_ids": ["X-REF"], "state": profile_state(1)})
    [result] = staged["staged_results"]
    assert result["patch"]["procedure_runs"][0].params_snapshot == {"limit": 500000}

    nodes, _, _ = profile_nodes(tmp_path / "v2")
    [run] = (await nodes.execute(profile_state(2), "X-REF"))["procedure_runs"]
    assert run.params_snapshot == {"limit": 600000}


async def test_profile_ref_without_value_does_not_build_a_run(tmp_path: Any) -> None:
    """采用的画像没有审批限额 (参数没写 optional): 解析抛 ValueError, 不建执行记录, 不调 Agent."""
    nodes, spy, store = profile_nodes(tmp_path)
    store.profiles = [NUMBAT_PROFILE]
    with pytest.raises(ValueError, match="approval_limit_cents 尚未确定"):
        await nodes.execute(profile_state(1), "X-REF")
    assert spy.runs == []


def test_numbat_params_do_not_depend_on_traits() -> None:
    """Numbat 的 18 条程序都不引 dataset., 用第 5.2 节的声明或缺省声明建执行记录, 参数相同 (验收 7 的守卫)."""
    from tests.test_knowledge import design_traits

    lib = library()
    assert len(lib.all()) == 18
    for template in lib.all():
        procedure = assembled(lib, template.id)
        default = build_run(
            lib, template, procedure, ENGAGEMENT, numbat_state(), NOW, traits=DatasetTraits(), profile=None
        )
        declared = build_run(
            lib, template, procedure, ENGAGEMENT, numbat_state(), NOW, traits=design_traits(), profile=None
        )
        assert declared.params_snapshot == default.params_snapshot, template.id
