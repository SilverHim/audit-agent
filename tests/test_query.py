"""StateQuery: 读检查点判断停在哪道闸门, 组装闸门材料与项目总览. 依赖用 tests/fakes.py 的假实现."""

from datetime import UTC, datetime

import pytest

from backend.contracts.models import (
    ClientProfile,
    GateDecision,
    GateId,
    GateVerdict,
    ProcedureStatus,
    ReviewTask,
    TargetType,
    TaskKind,
    TaskStatus,
)
from backend.orchestration.decisions import acc01_effects
from backend.orchestration.query import GraphStateQuery, current_gate
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    TYPO_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    make_runner,
    wait_idle,
)
from tests.test_runner import BrokenOutputStore, BrokenRegistry

EID = ENGAGEMENT.id
NOW = datetime(2026, 10, 3, tzinfo=UTC)


async def started(profile: ClientProfile = NUMBAT_PROFILE) -> tuple[GraphRunner, GraphStateQuery, FakeOutputStore]:
    """新项目启动并在后台跑到 ACC-01 闸门前. 返回 runner, query 与审计产出存储."""
    runner, store = make_runner(engagement_store=FakeEngagementStore([profile], [ENGAGEMENT]))
    await runner.start(EID)
    await wait_idle(runner, EID)
    return runner, GraphStateQuery(runner.app, runner.deps, runner), store


# ── 3a 闸门材料 ──


def test_current_gate() -> None:
    assert current_gate(("gate_acc01",)) is GateId.ACC_01
    assert current_gate(("gate_p1_risk",)) is GateId.PLN_01_03
    assert current_gate(("p0_intel",)) is None
    assert current_gate(()) is None


async def test_gate_is_none_before_start() -> None:
    runner, _ = make_runner()
    query = GraphStateQuery(runner.app, runner.deps, runner)

    assert await query.gate(EID) is None


async def test_gate_shows_acc01_material() -> None:
    _, query, _ = await started()

    view = await query.gate(EID)

    assert view is not None
    assert view.gate is GateId.ACC_01
    assert (view.target_id, view.target_version) == ("SCORECARD", 1)
    assert view.material.kind == "ACC-01"
    assert view.material.scorecard.version == 1
    assert view.material.exceptions == []  # Numbat 三项检查都通过
    assert view.material.client_profile == NUMBAT_PROFILE


async def test_gate_lists_scorecard_exceptions() -> None:
    _, query, _ = await started(TYPO_PROFILE)

    view = await query.gate(EID)

    assert view is not None
    assert view.material.kind == "ACC-01"
    assert [x.id for x in view.material.exceptions] == ["X-ACC-04-1-name_match-1"]
    assert view.material.client_profile.legal_name == TYPO_PROFILE.legal_name


async def test_gate_after_profile_update_shows_only_current_exceptions() -> None:
    """名称改对后 ACC-04 重跑: 状态里仍留着第 1 次的例外, 材料只列评分卡第 2 版引用的 (没有)."""
    runner, query, _ = await started(TYPO_PROFILE)
    config = GraphRunner.config(EID)
    saved = await runner.deps.engagement_store.save_profile(NUMBAT_PROFILE.model_copy(update={"version": 2}))
    await runner.app.aupdate_state(config, {"client_profile_version": saved.version}, as_node="gate_acc01")
    await runner.app.ainvoke(None, config)

    view = await query.gate(EID)

    assert view is not None
    assert view.material.kind == "ACC-01"
    assert (view.target_id, view.target_version) == ("SCORECARD", 2)
    assert view.material.exceptions == []
    assert view.material.client_profile.version == 2


async def test_gate_is_none_after_decline() -> None:
    runner, query, _ = await started()
    config = GraphRunner.config(EID)
    pack = (await runner.app.aget_state(config)).values["procedure_pack"]
    reject = GateDecision(
        id="D-1",
        gate=GateId.ACC_01,
        target_id="SCORECARD",
        target_version=1,
        decision=GateVerdict.reject,
        reason="客户不符合承接条件",
        decided_by="U-1",
        decided_at=NOW,
    )
    await runner.app.aupdate_state(config, acc01_effects(pack, reject), as_node="gate_acc01")
    await runner.app.ainvoke(None, config)

    assert await query.gate(EID) is None  # 已走到 declined 结束


# ── 3b 项目总览 ──


async def test_overview_at_acc01() -> None:
    _, query, _ = await started()

    o = await query.overview(EID)

    assert o.engagement == ENGAGEMENT
    assert o.client_name == NUMBAT_PROFILE.legal_name
    assert (o.running, o.run_error) == (False, None)
    assert o.current_gate is GateId.ACC_01
    assert o.awaiting_review is False
    assert o.pending_tasks == 0
    assert o.materiality_cents is None  # P1 才定重要性
    ids = [p.id for p in o.procedures]
    assert ids[:2] == ["ACC-01", "ACC-04"]  # 按阶段 P0 ... P4 排, 同阶段按编号
    assert ids[-2:] == ["RPT-01", "RPT-03"]
    status = {p.id: p.status for p in o.procedures}  # 程序编号 -> 状态
    assert status["ACC-04"] is ProcedureStatus.completed
    assert status["ACC-01"] is ProcedureStatus.planned
    acc04 = next(p for p in o.procedures if p.id == "ACC-04")
    assert acc04.name == "ASIC / ABN 核查"  # 取自程序模板


async def test_overview_while_running() -> None:
    runner, _ = make_runner()
    query = GraphStateQuery(runner.app, runner.deps, runner)

    await runner.start(EID)
    o = await query.overview(EID)  # 后台任务还没开始执行

    assert o.running is True
    assert o.current_gate is None
    assert all(p.status is ProcedureStatus.planned for p in o.procedures)  # start 已把固定集写入 Store
    await wait_idle(runner, EID)


async def test_overview_shows_run_error() -> None:
    runner, _ = make_runner(output_store=BrokenOutputStore())
    query = GraphStateQuery(runner.app, runner.deps, runner)
    await runner.start(EID)
    await wait_idle(runner, EID)

    o = await query.overview(EID)

    assert o.running is False
    assert o.run_error is not None
    assert "Store 不可用" in o.run_error
    assert o.current_gate is None  # 停在出错的 p0_intel, 不在闸门
    assert o.awaiting_review is False


async def test_overview_while_waiting_after_failure() -> None:
    """R03 验收 3: ACC-04 失败后停在节点内等待: 等复核, 不在执行, 无后台错误, 1 个待办."""
    runner, _ = make_runner(registry=BrokenRegistry())
    query = GraphStateQuery(runner.app, runner.deps, runner)
    await runner.start(EID)
    await wait_idle(runner, EID)

    o = await query.overview(EID)

    assert (o.awaiting_review, o.running, o.run_error, o.pending_tasks) == (True, False, None, 1)
    assert o.current_gate is None
    acc04 = next(p for p in o.procedures if p.id == "ACC-04")
    assert (acc04.status, acc04.pending_tasks) == (ProcedureStatus.awaiting_human, 1)


async def test_overview_counts_pending_tasks_from_store() -> None:
    _, query, store = await started()
    task = ReviewTask(
        id="T-X-ACC-04-1-v1",
        kind=TaskKind.normal,
        procedure_id="ACC-04",
        target_type=TargetType.AuditException,
        target_id="X-ACC-04-1",
        target_version=1,
        run_id="RUN-ACC-04-1",
        created_at=NOW,
    )
    decided = task.model_copy(update={"id": "T-other", "status": TaskStatus.decided})
    await store.put(EID, [task, decided], actor="A1")

    o = await query.overview(EID)

    assert o.pending_tasks == 1  # 已决定的不算
    acc04 = next(p for p in o.procedures if p.id == "ACC-04")
    assert acc04.pending_tasks == 1


async def test_overview_raises_when_engagement_missing() -> None:
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], []))
    query = GraphStateQuery(runner.app, runner.deps, runner)

    with pytest.raises(LookupError):
        await query.overview(EID)
