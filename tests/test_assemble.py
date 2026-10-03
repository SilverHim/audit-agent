"""R11 P2 装配入库与派发: assemble_pack 把新加入的 P2 程序与阶段写进 Store 与 engagements 表; schedule 只排调度表."""

import pytest

from backend.contracts.models import AuditObjective, Phase, Procedure, ProcedureStatus, RiskLevel, ScheduleRow
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import fixed_procedures
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_OBJECTIVES,
    NUMBAT_PROFILE,
    NUMBAT_RISKS,
    EngagementWrite,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    make_deps,
    make_graph_nodes,
)

EID = ENGAGEMENT.id
# 装配结果的顺序: 匹配到的程序按编号排序后补依赖
P2 = ["CSH-05", "CSH-04", "DOC-06", "CSH-02", "REV-03", "REV-08", "REV-02"]


def after_pln_approve(
    objectives: list[AuditObjective] = NUMBAT_OBJECTIVES,
) -> tuple[GraphNodes, FakeOutputStore, FakeEngagementStore, EngagementState]:
    """PLN-01/03 通过后的项目: Store 与状态有固定集, 审计目标与风险, 重要性三项. 返回节点, 两个存储与状态."""
    store = FakeOutputStore()
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT.model_copy(update={"phase": Phase.P1_Planning})])
    deps = make_deps(FakeRegistry(), FakeTrail(), engagements, store)
    pack = fixed_procedures(deps.library)
    store.rows.extend(pack)
    state = initial_state(ENGAGEMENT, pack, 1)
    state.update(  # type: ignore[typeddict-item]
        phase=Phase.P1_Planning,
        audit_objectives=objectives,
        risk_register=NUMBAT_RISKS,
        materiality_cents=12000000,
        performance_materiality_cents=9000000,
        clearly_trivial_cents=600000,
    )
    return make_graph_nodes(deps), store, engagements, state


def test_fixed_set_has_ten_programs() -> None:
    _, _, _, state = after_pln_approve()

    assert len(state["procedure_pack"]) == 10
    assert not set(P2) & {p.id for p in state["procedure_pack"]}


async def test_assemble_adds_seven_p2_programs_with_objectives() -> None:
    """验收 2: 恰好新增 7 条; objective_ids 为该程序直接匹配的目标, 只经依赖加入的 DOC-06 为空."""
    nodes, _, _, state = after_pln_approve()

    patch = await nodes.assemble_pack(state)

    added = {p.id: p for p in patch["procedure_pack"][10:]}
    assert list(added) == P2
    assert {pid: p.objective_ids for pid, p in added.items()} == {
        "REV-08": ["OBJ-REV-OCC", "OBJ-REV-CUT"],
        "REV-03": ["OBJ-REV-OCC", "OBJ-REV-CUT"],
        "REV-02": ["OBJ-REV-CUT"],
        "CSH-04": ["OBJ-CASH-EXI"],
        "CSH-05": ["OBJ-CASH-EXI"],
        "CSH-02": ["OBJ-CASH-EXI"],
        "DOC-06": [],
    }
    assert all(
        (p.version, p.status, p.phase) == (1, ProcedureStatus.planned, Phase.P2_Executing) for p in added.values()
    )
    assert patch["procedure_pack"][:10] == state["procedure_pack"]  # 固定集不动
    assert patch["phase"] is Phase.P2_Executing


async def test_assemble_writes_programs_and_phase_in_one_commit() -> None:
    """验收 3: 7 条第 1 版与阶段 P2_Executing 在同一次提交; 状态取 Store 返回的对象."""
    nodes, store, engagements, state = after_pln_approve()

    patch = await nodes.assemble_pack(state)

    [commit] = store.commits
    assert sorted(o.id for o in commit if isinstance(o, Procedure)) == sorted(P2)
    assert [w for w in commit if isinstance(w, EngagementWrite)] == [
        EngagementWrite("update", EID, {"phase": Phase.P2_Executing})
    ]
    engagement = await engagements.get(EID)
    assert engagement is not None and engagement.phase is Phase.P2_Executing
    for p in patch["procedure_pack"][10:]:
        assert await store.get(EID, Procedure, p.id) == p


async def test_uncovered_objective_blocks_assembly() -> None:
    """验收 4: 加一条没有程序覆盖的目标 (存货存在性): 抛错, 消息含其 ID, Store 与阶段都不变."""
    inventory = AuditObjective(
        id="OBJ-INV-EXI", account="Inventory", assertion="Existence", risk_level=RiskLevel.high, is_material=True
    )
    nodes, store, engagements, state = after_pln_approve([*NUMBAT_OBJECTIVES, inventory])
    rows = list(store.rows)

    with pytest.raises(ValueError, match="OBJ-INV-EXI"):
        await nodes.assemble_pack(state)

    assert store.rows == rows and store.commits == []
    engagement = await engagements.get(EID)
    assert engagement is not None and engagement.phase is Phase.P1_Planning


async def test_assemble_replay_writes_nothing_new() -> None:
    """验收 5: 上次已把 7 条第 1 版写进 Store, 检查点没跟上, 节点重跑: Store 不新增, 返回的仍是这 7 条."""
    nodes, store, _, state = after_pln_approve()
    first = await nodes.assemble_pack(state)
    rows = list(store.rows)

    second = await nodes.assemble_pack(state)

    assert store.rows == rows
    assert second == first


async def test_replay_returns_stored_objects() -> None:
    """重放前程序库提交变了, 新建的程序与 Store 已存的内容不同: 状态仍取 Store 已存的第 1 版."""
    nodes, _, _, state = after_pln_approve()
    first = await nodes.assemble_pack(state)
    nodes.deps.library.commit = "changed-before-replay"

    second = await nodes.assemble_pack(state)

    assert second == first
    assert all(p.template_commit != "changed-before-replay" for p in second["procedure_pack"][10:])


async def test_nothing_to_add_writes_nothing() -> None:
    """没有审计目标 (或 P2 程序都已在包中) 时不写也不改阶段."""
    nodes, store, _, state = after_pln_approve([])

    assert await nodes.assemble_pack(state) == {}
    assert store.commits == []


async def test_schedule_layers_and_groups_without_writing() -> None:
    """验收 6: 按依赖分层, 同层按业务循环分组.

    验收 7 按 Q6 (派发时不写 executing): schedule 不写 Store, 程序仍 planned.
    """
    nodes, store, _, state = after_pln_approve()
    state["procedure_pack"] = (await nodes.assemble_pack(state))["procedure_pack"]
    commits = list(store.commits)

    patch = await nodes.schedule(state)

    assert patch == {
        "schedule_table": [
            ScheduleRow(layer=0, instance="fw-0-0", procedure_ids=["CSH-04", "CSH-05"]),
            ScheduleRow(layer=0, instance="fw-0-1", procedure_ids=["REV-03", "REV-08"]),
            ScheduleRow(layer=1, instance="fw-1-0", procedure_ids=["DOC-06"]),
            ScheduleRow(layer=1, instance="fw-1-1", procedure_ids=["REV-02"]),
            ScheduleRow(layer=2, instance="fw-2-0", procedure_ids=["CSH-02"]),
        ]
    }
    assert store.commits == commits
    assert all(p.status is ProcedureStatus.planned for p in state["procedure_pack"][10:])
