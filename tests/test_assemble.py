"""R11 P2 装配入库与派发: assemble_pack 把新加入的 P2 程序与阶段写进 Store 与 engagements 表; schedule 只排调度表."""

import shutil
from pathlib import Path

import pytest

from backend.contracts.models import (
    AuditObjective,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureStatus,
    RiskLevel,
    ScheduleRow,
)
from backend.contracts.state import EngagementState, initial_state
from backend.core.config import get_settings
from backend.orchestration.nodes import GraphNodes, apply_patch, uncovered_objectives
from backend.orchestration.runner import GraphRunner, fixed_procedures
from backend.orchestration.scheduler import build_schedule
from backend.services.features import BASELINE
from backend.services.procedure_library import ProcedureLibrary
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
    make_runner,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, assert_rejected, at_pln_gate, inventory, pln_decision

EID = ENGAGEMENT.id
# 装配结果的顺序: 匹配到的程序按编号排序后补依赖
P2 = ["CSH-05", "CSH-04", "DOC-06", "CSH-02", "REV-03", "REV-08", "REV-02"]
# 基线固定集 (P2 以外的程序): 新程序挂功能名, 基线下不进固定集, 所以这里是精确集合
FIXED = {"ACC-04", "ACC-01", "DOC-04", "PLN-07", "PLN-01", "PLN-03", "FIN-04", "FIN-01", "RPT-03", "RPT-01"}


def added_programs(pack: list[Procedure]) -> list[Procedure]:
    """程序包中装配新增的 (不在固定集中的) 程序, 保持顺序."""
    return [p for p in pack if p.id not in FIXED]


def after_pln_approve(
    objectives: list[AuditObjective] = NUMBAT_OBJECTIVES,
    library: ProcedureLibrary | None = None,
    features: frozenset[str] | None = None,
) -> tuple[GraphNodes, FakeOutputStore, FakeEngagementStore, EngagementState]:
    """PLN-01/03 通过后的项目: Store 与状态有固定集, 审计目标与风险, 重要性三项. 返回节点, 两个存储与状态.
    library 与 features: 换程序库与功能集 (见 make_deps), 缺省为 knowledge 目录与基线, 给了 library 时功能集取它的."""
    store = FakeOutputStore()
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT.model_copy(update={"phase": Phase.P1_Planning})])
    deps = make_deps(FakeRegistry(), FakeTrail(), engagements, store, library=library, features=features)
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


def test_fixed_set_is_baseline() -> None:
    """基线固定集恰为 10 条 P2 以外的程序, 都不在 P2 阶段."""
    _, _, _, state = after_pln_approve()

    assert {p.id for p in state["procedure_pack"]} == FIXED and len(state["procedure_pack"]) == len(FIXED)
    assert all(p.phase is not Phase.P2_Executing for p in state["procedure_pack"])


async def test_assemble_adds_seven_p2_programs_with_objectives() -> None:
    """验收 2: 恰好新增 7 条; objective_ids 为该程序直接匹配的目标, 只经依赖加入的 DOC-06 为空."""
    nodes, _, _, state = after_pln_approve()

    patch = await nodes.assemble_pack(state)

    added = {p.id: p for p in added_programs(patch["procedure_pack"])}
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
    assert [p for p in patch["procedure_pack"] if p.id in FIXED] == state["procedure_pack"]  # 固定集不动
    assert patch["procedure_pack"][: len(state["procedure_pack"])] == state["procedure_pack"]  # 新增的追加在后
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
    for p in added_programs(patch["procedure_pack"]):
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
    assert all(p.template_commit != "changed-before-replay" for p in added_programs(second["procedure_pack"]))


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
    assert all(p.status is ProcedureStatus.planned for p in added_programs(state["procedure_pack"]))


# ── R81: P2 固定集 (模板 fixed) ──

X_FIX = """id: X-FIX
name: 自测固定集程序
phase: P2_Executing
cycle: Journals
owner: A6
engine: rule
autonomy_level: L3
fixed: true
feature: journals
"""
X_DEP = (
    X_FIX.replace("X-FIX", "X-DEP").replace("自测固定集程序", "自测固定集程序 (依赖 X-FIX)") + "depends_on: [X-FIX]\n"
)
JOURNALS = frozenset({"critic", "journals"})
INVENTORY = AuditObjective(
    id="OBJ-INV-EXI", account="Inventory", assertion="Existence", risk_level=RiskLevel.high, is_material=True
)


def fixed_library(tmp_path: Path, features: frozenset[str] = JOURNALS, **extra: str) -> ProcedureLibrary:
    """knowledge 目录的副本另加 extra 中的程序 YAML (文件名主干 → 内容), 按 features 建程序库."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    for name, text in extra.items():
        (root / "procedures" / f"{name}.yaml").write_text(text, encoding="utf-8")
    return ProcedureLibrary(root, features)


async def test_fixed_program_is_assembled_regardless_of_objectives(tmp_path: Path) -> None:
    """R81 验收 3: journals 开启时 X-FIX 不经 trigger 也加入, 排在按编号排序的末尾; v1 planned, objective_ids 为空,
    自主度取模板; 8 条第 1 版与阶段 P2_Executing 同一次提交. journals 关闭时仍为原 7 条."""
    nodes, store, _, state = after_pln_approve(library=fixed_library(tmp_path, **{"X-FIX": X_FIX}))
    patch = await nodes.assemble_pack(state)
    added = added_programs(patch["procedure_pack"])
    assert [p.id for p in added] == [*P2, "X-FIX"]
    fix = added[-1]
    assert (fix.version, fix.status, fix.objective_ids, fix.autonomy_level.value) == (
        1,
        ProcedureStatus.planned,
        [],
        "L3",
    )
    [commit] = store.commits
    assert sorted(o.id for o in commit if isinstance(o, Procedure)) == sorted([*P2, "X-FIX"])
    assert EngagementWrite("update", EID, {"phase": Phase.P2_Executing}) in commit

    base = fixed_library(tmp_path / "base", BASELINE, **{"X-FIX": X_FIX})
    nodes, _, _, state = after_pln_approve(library=base)
    assert [p.id for p in added_programs((await nodes.assemble_pack(state))["procedure_pack"])] == P2


async def test_fixed_dependencies_are_closed_and_layered(tmp_path: Path) -> None:
    """R81 验收 4: X-DEP 依赖 X-FIX, 两者都是固定集: 新增程序末尾为 X-FIX, X-DEP; 调度表中 X-DEP 比 X-FIX 低一层."""
    library = fixed_library(tmp_path, **{"X-FIX": X_FIX, "X-DEP": X_DEP})
    nodes, _, _, state = after_pln_approve(library=library)
    pack = (await nodes.assemble_pack(state))["procedure_pack"]
    assert [p.id for p in added_programs(pack)][-2:] == ["X-FIX", "X-DEP"]
    layers = {pid: row.layer for row in build_schedule(pack, library, 4) for pid in row.procedure_ids}
    assert layers["X-DEP"] == layers["X-FIX"] + 1


async def test_fixed_programs_do_not_cover_objectives(tmp_path: Path) -> None:
    """R81 验收 5: 覆盖校验不计固定集程序: 加存货存在性目标时有 X-FIX 也照样阻断, 缺口为 [OBJ-INV-EXI];
    PLN-01/03 approve 报 INVALID_VALUE (R60 不变)."""
    library = fixed_library(tmp_path, **{"X-FIX": X_FIX})
    nodes, store, _, state = after_pln_approve([*NUMBAT_OBJECTIVES, INVENTORY], library=library)
    assert uncovered_objectives(library, state["audit_objectives"]) == ["OBJ-INV-EXI"]
    with pytest.raises(ValueError, match="OBJ-INV-EXI"):
        await nodes.assemble_pack(state)
    assert store.commits == []
    s = await at_pln_gate(inventory(), library=library)
    await assert_rejected(s, pln_decision(GateVerdict.approve), "INVALID_VALUE", "OBJ-INV-EXI")


async def test_fixed_programs_are_not_added_twice(tmp_path: Path) -> None:
    """R81 验收 6: 装配后再装配 (程序包已含) 不再加入; 节点重放 (同一状态再执行) 程序包中各 ID 只出现一次,
    Store 不新增."""
    nodes, store, _, state = after_pln_approve(library=fixed_library(tmp_path, **{"X-FIX": X_FIX, "X-DEP": X_DEP}))
    first = await nodes.assemble_pack(state)
    assert await nodes.assemble_pack(apply_patch(state, first)) == {}
    rows = len(store.rows)
    replay = await nodes.assemble_pack(state)
    ids = [p.id for p in replay["procedure_pack"]]
    assert len(ids) == len(set(ids)) and replay["procedure_pack"] == first["procedure_pack"]
    assert len(store.rows) == rows


async def test_fixed_programs_are_sorted_with_matched(tmp_path: Path) -> None:
    """固定集与匹配集一并按编号排序后补依赖闭包, 不追加在末尾: 编号靠前的 B-FIX 排在最前 (审核 R81-04)."""
    early = X_FIX.replace("X-FIX", "B-FIX")
    nodes, _, _, state = after_pln_approve(library=fixed_library(tmp_path, **{"B-FIX": early}))
    assert [p.id for p in added_programs((await nodes.assemble_pack(state))["procedure_pack"])] == ["B-FIX", *P2]


async def test_fixed_program_added_once_after_reject_then_approve(tmp_path: Path) -> None:
    """R81 验收 6 (审核 R81-05): PLN-01/03 驳回回到 p1_analytics 后再通过, 全图装配一次, X-FIX 只有第 1 版."""
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    library = fixed_library(tmp_path, **{"X-FIX": X_FIX})
    runner, output_store = make_runner(
        engagement_store=engagement_store, library=library, interrupt_before=["schedule"]
    )
    s = Started(runner, output_store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await s.submit.submit(EID, pln_decision(GateVerdict.reject))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("gate_p1_risk",)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve, id="D-PLN-2", target_version=2))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("schedule",)
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    ids = [p.id for p in snapshot.values["procedure_pack"]]
    assert len(ids) == len(set(ids)) and "X-FIX" in ids
    fixed = [r for r in output_store.rows if isinstance(r, Procedure) and r.id == "X-FIX"]
    assert [(p.version, p.objective_ids) for p in fixed] == [(1, [])]
