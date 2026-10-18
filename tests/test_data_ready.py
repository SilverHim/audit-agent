"""需求 R10: 建项目不再载入财务数据, P1 开始前等数据就绪 (p1_await_data),
审计师确认数据已齐 (POST /data/ready) 后继续."""

from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.types import Command

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.contracts.data import TableKind, TableStats
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    Decision,
    GateId,
    GateVerdict,
    ProcedureStatus,
    ReviewRecord,
    ReviewTask,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import StatePatch
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.nodes import DATA_READY
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner, awaiting_data, review_waits
from backend.services.data_service import DataService
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    SEEDED_STATS,
    FakeDatasetLoader,
    FakeDataStore,
    FakeEngagementStore,
    FakeOutputStore,
    FakeTrail,
    ScriptA3,
    api_client,
    finished,
    make_runner,
    wait_idle,
)
from tests.test_decisions import acc01_decision

EID = ENGAGEMENT.id
NOW = datetime(2026, 10, 18, tzinfo=UTC)
REQUIRED = [TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries]


async def waiting_for_data(
    data: FakeDataStore | None = None,
) -> tuple[GraphRunner, FakeOutputStore, FakeDataStore]:
    """新项目 (还没有任何数据) 启动并通过 ACC-01, 停在 p1_await_data. 返回 runner, 审计产出存储与数据管理存储."""
    data = data if data is not None else FakeDataStore(ready=False)
    runner, store = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    return runner, store, data


def data_trail(runner: GraphRunner) -> list[dict[str, object]]:
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e.payload for e in trail.entries if e.kind is TrailKind.data]


async def test_new_engagement_waits_for_data_after_acceptance() -> None:
    runner, _, _ = await waiting_for_data()

    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("p1_await_data",)
    overview = await GraphStateQuery(runner.app, runner.deps, runner).overview(EID)
    assert (overview.awaiting_data, overview.awaiting_review, overview.running) == (True, False, False)
    assert overview.missing_tables == REQUIRED
    assert overview.current_gate is None
    doc04 = next(p for p in overview.procedures if p.id == "DOC-04")
    assert doc04.status is ProcedureStatus.planned  # DOC-04 还没跑
    loader = runner.deps.datasets
    assert isinstance(loader, FakeDatasetLoader) and loader.loads == []  # 不再自动载入数据集


async def test_missing_tables_follow_current_data() -> None:
    """缺失表现查: 只导入了科目表与本期试算表时, 缺上期试算表 (试算表算缺) 与总账分录."""
    runner, _, data = await waiting_for_data()
    data.table_stats = {
        TableKind.accounts: TableStats(records=23),
        TableKind.trial_balance: TableStats(records=23, current=23),
    }

    overview = await GraphStateQuery(runner.app, runner.deps, runner).overview(EID)

    assert overview.missing_tables == [TableKind.trial_balance, TableKind.journal_entries]


async def test_ready_rejected_while_tables_missing() -> None:
    runner, _, data = await waiting_for_data()
    data.table_stats = {TableKind.accounts: TableStats(records=23)}

    async with api_client(runner) as client:
        response = await client.post(f"/api/v1/engagements/{EID}/data/ready")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "DATA_NOT_READY"
    assert detail["missing_tables"] == ["trial_balance", "journal_entries"]
    assert data.confirm_calls == [] and data.ready_at is None
    assert not runner.is_running(EID)  # 项目锁已释放
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("p1_await_data",)


async def test_ready_resumes_into_p1_analysis() -> None:
    runner, store, data = await waiting_for_data()
    data.table_stats = dict(SEEDED_STATS)  # 审计师已接入齐数据

    async with api_client(runner) as client:
        response = await client.post(f"/api/v1/engagements/{EID}/data/ready")
        assert response.status_code == 202
        assert response.json()["ready_by"] == "U-1"
        assert runner.is_running(EID)  # 立即返回, 后台继续
        await wait_idle(runner, EID)
        again = await client.post(f"/api/v1/engagements/{EID}/data/ready")

    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)
    doc04 = max((r for r in store.rows if getattr(r, "id", None) == "DOC-04"), key=lambda r: r.version)
    assert getattr(doc04, "status", None) is ProcedureStatus.completed
    assert data.confirm_calls == [(EID, "U-1")]
    assert data_trail(runner) == [{"action": "ready"}]
    assert (again.status_code, again.json()["detail"]["code"]) == (409, "NOT_AWAITING_DATA")
    overview = await GraphStateQuery(runner.app, runner.deps, runner).overview(EID)
    assert (overview.awaiting_data, overview.missing_tables) == (False, [])


async def test_ready_rejected_when_not_waiting_for_data() -> None:
    """停在 ACC-01 闸门 (还没承接) 时确认数据已齐: 409 NOT_AWAITING_DATA, 不记确认."""
    data = FakeDataStore(ready=False, stats=SEEDED_STATS)
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data)
    await runner.start(EID)
    await wait_idle(runner, EID)

    async with api_client(runner) as client:
        response = await client.post(f"/api/v1/engagements/{EID}/data/ready")

    assert (response.status_code, response.json()["detail"]["code"]) == (409, "NOT_AWAITING_DATA")
    assert data.ready_at is None and not runner.is_running(EID)


async def test_ready_rejected_when_locked_or_busy() -> None:
    runner, _, data = await waiting_for_data()
    data.table_stats = dict(SEEDED_STATS)
    engagements = runner.deps.engagement_store
    assert isinstance(engagements, FakeEngagementStore)

    async with api_client(runner) as client:
        runner.acquire(EID)
        busy = await client.post(f"/api/v1/engagements/{EID}/data/ready")
        runner.release(EID)
        await engagements.update(EID, locked=True)
        locked = await client.post(f"/api/v1/engagements/{EID}/data/ready")

    assert (busy.status_code, busy.json()["detail"]["code"]) == (409, "ENGAGEMENT_BUSY")
    assert (locked.status_code, locked.json()["detail"]["code"]) == (423, "ENGAGEMENT_LOCKED")
    assert data.confirm_calls == []


async def test_review_decision_rejected_while_waiting_for_data() -> None:
    """等数据就绪不是等复核: 复核决定报 NOT_AWAITING_REVIEW (即使 Store 里有待决任务)."""
    runner, store, _ = await waiting_for_data()
    task = ReviewTask(
        id="T-X",
        kind=TaskKind.failed,
        procedure_id="DOC-04",
        target_type=TargetType.ProcedureRun,
        target_id="RUN-DOC-04-1",
        target_version=1,
        run_id="RUN-DOC-04-1",
        created_at=NOW,
    )
    store.rows.append(task)
    record = ReviewRecord(
        id="D-R1",
        task_id="T-X",
        target_version=1,
        decision=Decision.rerun,
        reason="test",
        reviewed_by="U-1",
        decided_at=NOW,
    )

    try:
        await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, record)
    except DecisionRejected as e:
        assert e.code == "NOT_AWAITING_REVIEW"
    else:
        raise AssertionError("应拒绝")


async def test_recover_resumes_after_confirmation_written_but_not_resumed() -> None:
    """确认已写入 Store 而图没恢复 (提交后中断): recover 以 DATA_READY 恢复, 继续到 PLN-01/03 闸门; 不重复确认."""
    runner, _, data = await waiting_for_data()
    data.table_stats = dict(SEEDED_STATS)
    await data.confirm_ready(EID, "U-1")  # 只写了确认

    await runner.recover()
    await wait_idle(runner, EID)

    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)
    assert runner.run_error(EID) is None
    assert data.confirm_calls == [(EID, "U-1")]


async def test_recover_leaves_unconfirmed_wait_alone() -> None:
    runner, _, _ = await waiting_for_data()

    await runner.recover()

    assert not runner.is_running(EID)
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("p1_await_data",)


async def test_resume_without_confirmation_is_a_node_error() -> None:
    """绕过 /data/ready 直接恢复: 节点报错并写明须先确认, 仍算在等数据; 之后照常确认即可继续."""
    runner, _, data = await waiting_for_data()

    runner.acquire(EID)
    runner.launch(EID, Command(resume=DATA_READY))
    await wait_idle(runner, EID)

    assert "须经 POST /data/ready 确认" in (runner.run_error(EID) or "")
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert [(t.name, bool(t.error), bool(t.interrupts)) for t in snapshot.tasks] == [("p1_await_data", True, True)]
    overview = await GraphStateQuery(runner.app, runner.deps, runner).overview(EID)
    assert overview.awaiting_data

    data.table_stats = dict(SEEDED_STATS)
    async with api_client(runner) as client:
        response = await client.post(f"/api/v1/engagements/{EID}/data/ready")
        await wait_idle(runner, EID)

    assert response.status_code == 202
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)
    assert runner.run_error(EID) is None


async def test_seeded_engagement_does_not_wait() -> None:
    """数据集载入 (种子, 测试, 演示项目) 视同已确认: 通过 ACC-01 后直接进 P1 分析, 停在 PLN-01/03 闸门."""
    runner, _, data = await waiting_for_data(FakeDataStore())

    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)
    assert data.confirm_calls == []
    assert GateId.PLN_01_03 is (await GraphStateQuery(runner.app, runner.deps, runner).overview(EID)).current_gate


# ── 审核 FD-R10 第 1 轮后补充: 恢复补写确认留痕, 升级前的旧检查点, 恢复值不串到 DOC-04 ──


def ready_entries(runner: GraphRunner) -> list[TrailEntry]:
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.idempotency_key == f"{EID}:data:ready"]


@pytest.mark.parametrize("recovery", ["resume", "recover"])
async def test_recovery_backfills_ready_trail(monkeypatch: pytest.MonkeyPatch, recovery: str) -> None:
    """确认已写入而 data 留痕没写成 (接口报错): resume 与 recover 先补写唯一一条留痕, 再越过等待."""
    runner, _, data = await waiting_for_data()
    data.table_stats = dict(SEEDED_STATS)
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    original = trail.record

    async def fail_data(entry: TrailEntry) -> str:
        if entry.kind is TrailKind.data:
            raise OSError("留痕不可用")
        return await original(entry)

    monkeypatch.setattr(trail, "record", fail_data)
    with pytest.raises(OSError):
        await DataService(runner.deps, runner).confirm_ready(EID, "U-1")
    assert data.ready_at is not None and not runner.is_running(EID)
    # 补做时留痕仍写不成: 报错, 留在等待
    restarted = GraphRunner(runner.app, runner.deps)
    await restarted.recover()
    assert "留痕不可用" in (restarted.run_error(EID) or "")
    assert awaiting_data(await runner.app.aget_state(GraphRunner.config(EID)))

    monkeypatch.setattr(trail, "record", original)
    restarted = GraphRunner(runner.app, runner.deps)
    if recovery == "resume":
        await restarted.resume(EID)
    else:
        await restarted.recover()
    await wait_idle(restarted, EID)

    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)
    assert [(e.actor, e.payload) for e in ready_entries(runner)] == [("U-1", {"action": "ready"})]


async def legacy_checkpoint(data: FakeDataStore) -> GraphRunner:
    """升级前的检查点形状: 已过 ACC-01, 待执行 p1_ingest (旧拓扑里由它载入数据集), 没经过 p1_await_data."""
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data)
    await runner.start(EID)
    await wait_idle(runner, EID)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    original = runner.launch
    runner.launch = lambda eid, graph_input: runner.release(eid)  # type: ignore[method-assign]  # 决定写入后不推进
    await submit.submit(EID, acc01_decision(GateVerdict.approve))
    runner.launch = original  # type: ignore[method-assign]
    config = GraphRunner.config(EID)
    assert (await runner.app.aget_state(config)).next == ("p1_await_data",)
    await runner.app.aupdate_state(config, {}, as_node="p1_await_data")  # 越过等待, 排上 p1_ingest
    assert (await runner.app.aget_state(config)).next == ("p1_ingest",)
    return GraphRunner(runner.app, runner.deps)


async def test_legacy_checkpoint_without_data_is_rerouted_to_wait() -> None:
    data = FakeDataStore(ready=False)
    restarted = await legacy_checkpoint(data)

    await restarted.recover()
    await wait_idle(restarted, EID)

    snapshot = await restarted.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("p1_await_data",) and awaiting_data(snapshot)
    assert restarted.run_error(EID) is None
    data.table_stats = dict(SEEDED_STATS)
    await DataService(restarted.deps, restarted).confirm_ready(EID, "U-1")
    await wait_idle(restarted, EID)
    assert (await restarted.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_legacy_checkpoint_with_loaded_data_continues() -> None:
    """升级前已载入的项目 (迁移补上了确认): 旧检查点照常从 p1_ingest 继续."""
    restarted = await legacy_checkpoint(FakeDataStore())

    await restarted.recover()
    await wait_idle(restarted, EID)

    assert (await restarted.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_ready_resume_value_not_taken_by_doc04_failure_wait() -> None:
    """确认数据已齐后 DOC-04 失败: 节点内等复核, 恢复值没有被当成复核决定 ID (Codex 审核 FD-R10 的探针)."""

    def agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        def doc04(ctx: RunContext) -> StatePatch:
            run = finished(ctx.run)
            task = deps.queue.make_task(TaskKind.failed, run, "DOC-04", run.id, NOW)
            return {"procedure_runs": [run], "review_tasks": [task]}

        return {"DOC-04": ScriptA3(deps, doc04)}

    data = FakeDataStore(ready=False)
    runner, store = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data, agents=agents
    )
    await runner.start(EID)
    await wait_idle(runner, EID)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    data.table_stats = dict(SEEDED_STATS)

    await DataService(runner.deps, runner).confirm_ready(EID, "U-1")
    await wait_idle(runner, EID)

    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert runner.run_error(EID) is None and not awaiting_data(snapshot)
    assert [w["procedure_id"] for w in review_waits(snapshot)] == ["DOC-04"]
    assert len(await store.latest(EID, ReviewTask, status=TaskStatus.pending)) == 1


async def test_retry_after_launch_failure_keeps_first_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认已写入而后台没启动: 再确认一次照常恢复, 确认人不变, 留痕只有一条 (Codex 审核 FD-R10 的探针)."""
    runner, _, data = await waiting_for_data()
    data.table_stats = dict(SEEDED_STATS)
    launch = runner.launch

    def fail_launch(eid: str, graph_input: object) -> None:
        runner.release(eid)
        raise RuntimeError("后台启动失败")

    monkeypatch.setattr(runner, "launch", fail_launch)
    service = DataService(runner.deps, runner)
    with pytest.raises(RuntimeError):
        await service.confirm_ready(EID, "U-1")
    monkeypatch.setattr(runner, "launch", launch)

    again = await service.confirm_ready(EID, "U-2")
    await wait_idle(runner, EID)

    assert again.ready_by == "U-1"
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)
    assert len(ready_entries(runner)) == 1
