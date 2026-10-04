"""EngagementRunner: 项目锁, 后台推进, 启动时固定集写入 Store, 重启恢复 (recover) 与手动重试 (resume).
依赖用 tests/fakes.py 的假实现."""

import asyncio
import hashlib
import inspect
import shutil
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from langgraph.types import Command

from backend.adapters.llm import llm_version
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    Engagement,
    GateDecision,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureRun,
    ReviewTask,
    RunStatus,
    Versioned,
)
from backend.core.config import Settings, get_settings
from backend.main import lifespan
from backend.orchestration import runner as runner_module
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner, fixed_procedures
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_PROFILE,
    EngagementWrite,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    NoTaskAsyncio,
    make_runner,
    preset_rerun,
    wait_idle,
)
from tests.test_decisions import acc01_decision
from tests.test_rerun import failed_at_p0

EID = ENGAGEMENT.id


class BrokenRegistry(FakeRegistry):
    """ABN Lookup 不可用: 查询即抛错. ACC-04 执行失败, 节点转成失败执行与 failed 任务后等人处理."""

    async def abn(self, abn: str) -> AbnRecord | None:
        raise RuntimeError("ABN Lookup 不可用")


class BrokenOutputStore(FakeOutputStore):
    """写执行记录时抛错 (start 写固定集不受影响): 节点自身出错, 用来制造后台执行错误 run_error."""

    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        if any(isinstance(obj, ProcedureRun) for obj in objs):
            raise RuntimeError("Store 不可用")
        return await super().put(eid, objs, actor=actor)


class HangingRegistry(FakeRegistry):
    """ABN Lookup 一直不返回: 后台任务停在 ACC-04 里, 用来测取消与退出. entered 在查询开始时置位."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def abn(self, abn: str) -> AbnRecord | None:
        self.entered.set()
        await asyncio.Event().wait()
        return None


async def test_start_returns_while_running_then_stops_at_acc01() -> None:
    runner, _ = make_runner()

    await runner.start(EID)
    assert runner.is_running(EID)  # start 立即返回, 图还在后台执行

    await wait_idle(runner, EID)
    assert not runner.is_running(EID)  # 停在闸门, 项目锁已释放
    assert runner.run_error(EID) is None
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_acc01",)


async def test_start_saves_fixed_procedures_v1() -> None:
    runner, store = make_runner()

    await runner.start(EID)
    await wait_idle(runner, EID)

    *first, _ = store.commits[0]  # start 写入的那一批, 在 p0_intel 之前; 末项为版本快照 (见 R32 的测试)
    assert [p.id for p in first] == [p.id for p in fixed_procedures(runner.deps.library)]
    assert all(isinstance(p, Procedure) and p.version == 1 for p in first)


async def test_start_rejects_when_busy() -> None:
    runner, _ = make_runner()
    await runner.start(EID)

    task = runner.tasks[EID]

    with pytest.raises(DecisionRejected) as e:
        await runner.start(EID)
    assert e.value.code == "ENGAGEMENT_BUSY"
    assert runner.is_running(EID)  # 被拒的调用不能放掉原持有者的锁
    assert runner.tasks[EID] is task and not task.done()

    await wait_idle(runner, EID)


async def test_background_error_is_recorded_and_lock_released() -> None:
    runner, _ = make_runner(output_store=BrokenOutputStore())

    await runner.start(EID)
    await wait_idle(runner, EID)  # 后台任务不把异常抛给等待者

    assert not runner.is_running(EID)
    error = runner.run_error(EID)
    assert error is not None
    assert "Store 不可用" in error


async def test_agent_failure_waits_instead_of_run_error() -> None:
    """R03: Agent 出错不是后台执行错误: 失败执行与 failed 任务写入 Store, 图停在 p0_intel 节点内等人处理, 锁已释放."""
    runner, store = make_runner(registry=BrokenRegistry())

    await runner.start(EID)
    await wait_idle(runner, EID)

    assert not runner.is_running(EID)
    assert runner.run_error(EID) is None
    run = await store.get(EID, ProcedureRun, "RUN-ACC-04-1")
    assert run is not None and run.status is RunStatus.failed
    assert run.failure_reason == "RuntimeError: ABN Lookup 不可用"
    assert [t.id for t in await store.latest(EID, ReviewTask)] == ["T-RUN-ACC-04-1-v1"]
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("p0_intel",)
    assert [i.value["run_id"] for task in snapshot.tasks for i in task.interrupts] == ["RUN-ACC-04-1"]


async def test_success_clears_previous_error() -> None:
    runner, _ = make_runner()
    runner.errors[EID] = "RuntimeError: 上次失败"

    await runner.start(EID)
    await wait_idle(runner, EID)

    assert runner.run_error(EID) is None


async def test_start_releases_lock_when_engagement_missing() -> None:
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], []))

    with pytest.raises(LookupError):
        await runner.start(EID)

    assert not runner.is_running(EID)
    assert EID not in runner.tasks  # 没有启动后台任务


# ── F1: 任务异常结束的各条路径都释放项目锁 ──


async def test_cancel_before_task_starts_releases_lock() -> None:
    """start 返回后任务还没开始执行就被取消: _advance 的 finally 不会执行, 由结束回调释放锁."""
    runner, _ = make_runner()
    await runner.start(EID)
    task = runner.tasks[EID]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not runner.is_running(EID)
    assert EID not in runner.tasks


async def test_launch_failure_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """建后台任务失败: start 抛出, 锁已释放, 不登记任务."""
    runner, _ = make_runner()

    no_tasks = NoTaskAsyncio()
    monkeypatch.setattr(runner_module, "asyncio", no_tasks)
    with pytest.raises(RuntimeError, match="事件循环已关闭"):
        await runner.start(EID)

    assert not runner.is_running(EID)
    assert EID not in runner.tasks
    [coro] = no_tasks.coros
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED  # launch 关掉了不会执行的协程


async def test_finished_task_does_not_clear_newer_task() -> None:
    """旧任务的清理晚到时, 同一项目已换成新任务: 不动新任务的锁与引用."""
    runner, _ = make_runner()
    old = asyncio.create_task(asyncio.sleep(0))
    await old
    runner.acquire(EID)
    newer = asyncio.create_task(asyncio.sleep(0))
    runner.tasks[EID] = newer

    runner._forget(EID, old)

    assert runner.is_running(EID)
    assert runner.tasks[EID] is newer
    await newer


async def test_shutdown_cancels_running_tasks() -> None:
    registry = HangingRegistry()
    runner, _ = make_runner(registry=registry)
    await runner.start(EID)
    await asyncio.wait_for(registry.entered.wait(), 2)
    task = runner.tasks[EID]

    await runner.shutdown()

    assert task.cancelled()
    assert not runner.is_running(EID)
    assert runner.tasks == {}
    assert runner.run_error(EID) is None  # 退出时的取消不算执行出错


async def test_lifespan_exit_stops_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """应用退出: 关闭检查点连接前取消并等完后台任务. 后台执行换成一直等待的协程."""
    started = asyncio.Event()

    async def hanging_advance(self: GraphRunner, eid: str, graph_input: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(GraphRunner, "_advance", hanging_advance)
    app = FastAPI()
    async with lifespan(app):
        runner: GraphRunner = app.state.runner
        runner.acquire(EID)
        runner.launch(EID, None)
        await asyncio.wait_for(started.wait(), 2)
        task = runner.tasks[EID]

    assert task.cancelled()
    assert not runner.is_running(EID)
    assert runner.tasks == {}


# ── R29: recover 与 resume ──

SECOND = ENGAGEMENT.model_copy(update={"id": "ENG-2026-002"})


class HangOnceRegistry(FakeRegistry):
    """第一次查 ABN 一直不返回 (模拟进程在 ACC-04 执行中被杀), 之后照 Numbat 返回. entered 在第一次查询时置位."""

    def __init__(self) -> None:
        super().__init__({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC})
        self.entered = asyncio.Event()
        self.calls = 0

    async def abn(self, abn: str) -> AbnRecord | None:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await asyncio.Event().wait()
        return await super().abn(abn)


class FailOnceOutputStore(FakeOutputStore):
    """第一次写执行记录时抛错 (节点自身出错, 记入 run_error), 之后正常."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        if not self.failed and any(isinstance(obj, ProcedureRun) for obj in objs):
            self.failed = True
            raise RuntimeError("Store 暂时不可用")
        return await super().put(eid, objs, actor=actor)


def restart(runner: GraphRunner) -> GraphRunner:
    """模拟重启: 新 runner 共用检查点与各假存储 (同一个编译好的图), 没有锁, 任务与错误记录."""
    return GraphRunner(runner.app, runner.deps)


def config(eid: str = EID) -> Any:
    return GraphRunner.config(eid)


async def killed_in_p0(engagements: list[Engagement] | None = None) -> tuple[GraphRunner, FakeOutputStore]:
    """项目启动后在 ACC-04 查登记时被杀 (取消后台任务): 检查点停在 p0_intel 之前, Store 只有固定集."""
    registry = HangOnceRegistry()
    store = FakeEngagementStore([NUMBAT_PROFILE], engagements or [ENGAGEMENT])
    runner, output_store = make_runner(registry=registry, engagement_store=store)
    await runner.start(EID)
    await asyncio.wait_for(registry.entered.wait(), 2)
    await runner.shutdown()
    return runner, output_store


async def test_recover_after_restart_continues_from_checkpoint() -> None:
    """验收 1, 2: p0_intel 执行中进程被杀; 新 runner recover 返回时该项目已在后台执行, 停在 ACC-01 闸门;
    SCORECARD v1, Store 只有 RUN-ACC-04-1."""
    first, store = await killed_in_p0()
    assert (await first.app.aget_state(config())).next == ("p0_intel",)

    runner = restart(first)
    await runner.recover()

    assert runner.is_running(EID)
    await wait_idle(runner, EID)
    assert (await runner.app.aget_state(config())).next == ("gate_acc01",)
    assert [s.version for s in store.rows if isinstance(s, AcceptanceScorecard)] == [1]
    assert [r.id for r in store.rows if isinstance(r, ProcedureRun)] == ["RUN-ACC-04-1"]
    assert runner.run_error(EID) is None


async def test_recover_skips_gate_wait_and_end() -> None:
    """验收 1: 停在闸门前, 停在节点内等复核, 已结束的项目都不动: 不持锁, 检查点不变."""
    gate, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT, SECOND]))
    await gate.start(EID)
    await wait_idle(gate, EID)
    await GraphDecisionSubmit(gate.app, gate.deps, gate).submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(gate, EID)  # 拒绝承接, 走到 declined 结束
    await gate.start(SECOND.id)
    await wait_idle(gate, SECOND.id)  # 停在 ACC-01 闸门
    waiting, _, _ = await failed_at_p0()  # ACC-04 失败, 停在 p0_intel 节点内等人
    for runner, eids in ((gate, [EID, SECOND.id]), (waiting, [EID])):
        before = [(await runner.app.aget_state(config(e))).config for e in eids]

        runner = restart(runner)
        await runner.recover()

        assert not any(runner.is_running(e) for e in eids)
        assert [(await runner.app.aget_state(config(e))).config for e in eids] == before
        assert all(runner.run_error(e) is None for e in eids)


async def test_recover_error_is_isolated_per_engagement() -> None:
    """验收 3: 一个项目恢复出错 (没有检查点, 采用的画像版本不存在) 记入其 run_error, 不影响另一个项目继续执行."""
    broken = SECOND.model_copy(update={"client_profile_version": 9})
    first, _ = await killed_in_p0([ENGAGEMENT, broken])

    runner = restart(first)
    await runner.recover()
    await wait_idle(runner, EID)

    assert "client profile CLIENT-001 v9 not found" in (runner.run_error(broken.id) or "")
    assert not runner.is_running(broken.id)
    assert (await runner.app.aget_state(config())).next == ("gate_acc01",)
    assert runner.run_error(EID) is None


async def test_recover_without_checkpoint_starts_from_initial_state() -> None:
    """W6: start 写了固定集, 第一步还没落盘就中断 (没有检查点): recover 以初始状态重新推进, 固定集不重复写."""
    runner, store = make_runner()
    await store.put(EID, fixed_procedures(runner.deps.library), actor="A1")
    rows = len(store.rows)

    await runner.recover()
    await wait_idle(runner, EID)

    assert (await runner.app.aget_state(config())).next == ("gate_acc01",)
    assert [p.version for p in store.rows[:rows]] == [1] * rows
    assert sum(isinstance(r, Procedure) and r.version == 1 for r in store.rows) == rows


async def test_resume_retries_after_run_error() -> None:
    """验收 4: 后台执行出错后 resume 从检查点继续, 成功后 run_error 清空."""
    runner, _ = make_runner(output_store=FailOnceOutputStore())
    await runner.start(EID)
    await wait_idle(runner, EID)
    assert "Store 暂时不可用" in (runner.run_error(EID) or "")

    await runner.resume(EID)

    assert runner.is_running(EID)
    await wait_idle(runner, EID)
    assert (await runner.app.aget_state(config())).next == ("gate_acc01",)
    assert runner.run_error(EID) is None


async def test_resume_rejects_when_busy() -> None:
    """验收 4: 项目锁被占时 ENGAGEMENT_BUSY, 不动原持有者的锁."""
    runner, _ = make_runner()
    runner.acquire(EID)

    with pytest.raises(DecisionRejected) as e:
        await runner.resume(EID)

    assert e.value.code == "ENGAGEMENT_BUSY"
    assert runner.is_running(EID)


async def test_resume_at_gate_does_nothing() -> None:
    """验收 4: 停在闸门前且无待补做: 不改检查点, 不持锁."""
    runner, _ = make_runner()
    await runner.start(EID)
    await wait_idle(runner, EID)
    before = (await runner.app.aget_state(config())).config

    await runner.resume(EID)

    assert not runner.is_running(EID)
    assert (await runner.app.aget_state(config())).config == before


async def test_resume_at_second_wait_does_not_advance() -> None:
    """节点内第 2 次等待 (ACC-04 重跑后又失败, next 为空): resume 不调 ainvoke(None), 否则第 2 个 interrupt()
    会沿用上次的恢复值 D-1, 凭空多出一个决定."""
    runner, store, registry = await failed_at_p0(failures=2)
    await preset_rerun(store, (await store.latest(EID, ReviewTask))[0], "D-1")
    await runner.app.ainvoke(Command(resume="D-1"), config())
    snapshot = await runner.app.aget_state(config())
    assert snapshot.next == () and [i.value["run_id"] for t in snapshot.tasks for i in t.interrupts] == ["RUN-ACC-04-2"]

    await runner.resume(EID)

    assert not runner.is_running(EID)
    assert (await runner.app.aget_state(config())).config == snapshot.config
    assert registry.calls == 2


async def test_store_ahead_of_checkpoint_is_not_skipped_by_phase() -> None:
    """验收 5: 只看检查点, 不看阶段: 决定已提交 (项目 Declined 且 locked) 而检查点仍停在 ACC-01 闸门时不跳过,
    交给补做 (尚未实现, 记入 run_error); 总览提示须 resume. 已走到 END 的项目跳过."""
    runner, store = make_runner()
    await runner.start(EID)
    await wait_idle(runner, EID)
    decision = acc01_decision(GateVerdict.reject)
    async with store.transaction(EID):  # 只写 Store, 检查点没跟上
        await store.put(EID, [decision], actor="U-1")
        await runner.deps.engagement_store.update(EID, phase=Phase.Declined, locked=True)
    overview = await GraphStateQuery(runner.app, runner.deps, runner).overview(EID)
    assert (
        overview.run_error is not None and "RESUME_REQUIRED" in overview.run_error and decision.id in overview.run_error
    )

    runner = restart(runner)
    await runner.recover()

    assert not runner.is_running(EID)
    error = runner.run_error(EID)
    assert error is not None and "恢复补做尚未实现" in error and decision.id in error


@pytest.mark.parametrize("pause_kind", [Procedure, GateDecision])
async def test_overview_does_not_misreport_concurrent_submit(
    monkeypatch: pytest.MonkeyPatch, pause_kind: type[Any]
) -> None:
    """W5: 总览读取期间一个决定正常提交完成 (Store 与检查点都已跟上): 不误报 RESUME_REQUIRED.
    在总览读程序状态时 (尚未判断) 或判断补做读决定时让出, 两种时序."""
    runner, store = make_runner()
    await runner.start(EID)
    await wait_idle(runner, EID)
    query = GraphStateQuery(runner.app, runner.deps, runner)
    entered, proceed = asyncio.Event(), asyncio.Event()
    latest = store.latest
    overview: asyncio.Task[Any]

    async def paused_latest(eid: str, kind: type[Any], **filters: Any) -> list[Any]:
        if asyncio.current_task() is overview and kind is pause_kind and not entered.is_set():
            entered.set()
            await proceed.wait()
        return await latest(eid, kind, **filters)

    monkeypatch.setattr(store, "latest", paused_latest)
    overview = asyncio.create_task(query.overview(EID))
    await asyncio.wait_for(entered.wait(), 2)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(
        EID, acc01_decision(GateVerdict.insufficient_info)
    )
    await wait_idle(runner, EID)
    proceed.set()

    assert (await asyncio.wait_for(overview, 2)).run_error is None


async def test_recover_busy_project_keeps_owner_lock() -> None:
    """recover 取不到某项目的锁 (正被占用): 记入其 run_error, 不释放原持有者的锁, 继续处理其他项目."""
    first, _ = await killed_in_p0([ENGAGEMENT, SECOND])
    runner = restart(first)
    runner.acquire(EID)

    await runner.recover()

    assert runner.is_running(EID) and "ENGAGEMENT_BUSY" in (runner.run_error(EID) or "")
    await wait_idle(runner, SECOND.id)
    assert (await runner.app.aget_state(config(SECOND.id))).next == ("gate_acc01",)
    runner.release(EID)


async def test_missing_decision_detected_before_second_wait_is_skipped() -> None:
    """节点内第 2 次等待 (next 为空) 时 Store 已有检查点里没有的决定 D-2: 先查补做 (尚未实现, 抛出), 不按等待跳过."""
    first, store, registry = await failed_at_p0(failures=2)
    await preset_rerun(store, (await store.latest(EID, ReviewTask))[0], "D-1")
    await first.app.ainvoke(Command(resume="D-1"), config())
    second = max(await store.latest(EID, ReviewTask), key=lambda t: t.id)
    await preset_rerun(store, second, "D-2")
    runner = restart(first)

    with pytest.raises(NotImplementedError, match="D-2"):
        await runner.resume(EID)

    assert not runner.is_running(EID) and registry.calls == 2


async def test_advance_writes_checkpoints_synchronously(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 6: 后台推进图时 ainvoke 带 durability="sync"."""
    runner, _ = make_runner()
    calls: list[dict[str, Any]] = []

    async def ainvoke(graph_input: Any, cfg: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(runner.app, "ainvoke", ainvoke)
    await runner.start(EID)
    await wait_idle(runner, EID)

    assert calls == [{"durability": "sync"}]


async def test_lifespan_calls_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    """应用启动时调 recover; 项目列表尚未实现 (NotImplementedError) 时不阻断启动."""
    called: list[bool] = []
    original = GraphRunner.recover

    async def recover(self: GraphRunner) -> None:
        called.append(True)
        await original(self)  # Postgres 项目存储的 list 尚未实现

    monkeypatch.setattr(GraphRunner, "recover", recover)
    async with lifespan(FastAPI()):
        pass

    assert called == [True]


# ── R32: version_snapshot 冻结 ──

SNAPSHOT_KEYS = {"procedure_library_commit", "llm_mode", "model", "prompts_sha256", "frozen_at"}


async def test_start_freezes_version_snapshot() -> None:
    """验收 1: start 冻结版本快照 (mock 模式模型名为 mock), 与固定集在同一次提交, 排在固定集之后;
    图的初始状态与项目表中的快照相同."""
    runner, store = make_runner()

    await runner.start(EID)
    await wait_idle(runner, EID)

    *pack, write = store.commits[0]
    assert all(isinstance(p, Procedure) for p in pack)
    assert isinstance(write, EngagementWrite) and (write.op, write.target) == ("update", EID)
    snapshot = write.changes["version_snapshot"]
    assert set(snapshot) == SNAPSHOT_KEYS
    library = runner.deps.library
    assert (snapshot["procedure_library_commit"], snapshot["prompts_sha256"]) == (
        library.commit,
        library.prompts_sha256(),
    )
    assert (snapshot["llm_mode"], snapshot["model"]) == ("mock", "mock")
    assert datetime.fromisoformat(snapshot["frozen_at"]).tzinfo is not None
    engagement = await runner.deps.engagement_store.get(EID)
    assert engagement is not None and engagement.version_snapshot == snapshot
    history = [s async for s in runner.app.aget_state_history(config())]
    assert history[-2].values["version_snapshot"] == snapshot  # 输入写入后, p0_intel 之前


async def test_existing_snapshot_is_not_overwritten() -> None:
    """验收 4: 项目已有版本快照 (如没有检查点时 recover 重新推进): 不覆盖, 初始状态沿用它."""
    frozen = {"procedure_library_commit": "abc", "llm_mode": "mock", "model": "mock"}
    engagement = ENGAGEMENT.model_copy(update={"version_snapshot": frozen})
    runner, store = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [engagement]))

    await runner.recover()  # 没有检查点, 以初始状态推进
    await wait_idle(runner, EID)

    assert not any(isinstance(w, EngagementWrite) for w in store.commits[0])
    assert (await runner.deps.engagement_store.get(EID)).version_snapshot == frozen  # type: ignore[union-attr]
    history = [s async for s in runner.app.aget_state_history(config())]
    assert history[-2].values["version_snapshot"] == frozen


def test_llm_version_by_mode() -> None:
    """验收 1: live 模式模型名取 LLM_MODEL, mock 模式为 mock."""
    assert llm_version(Settings(LLM_MODE="live", LLM_MODEL="model-x")) == {"llm_mode": "live", "model": "model-x"}
    assert llm_version(Settings(LLM_MODE="mock", LLM_MODEL="model-x")) == {"llm_mode": "mock", "model": "mock"}


def test_prompts_sha256_changes_with_any_prompt(tmp_path: Path) -> None:
    """验收 2: prompts/*.j2 按文件名排序后内容的 sha256; 改任一 prompt 后变化."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    library = ProcedureLibrary(root)
    prompts = sorted((root / "prompts").glob("*.j2"), key=lambda p: p.name)
    assert library.prompts_sha256() == hashlib.sha256(b"".join(p.read_bytes() for p in prompts)).hexdigest()
    before = library.prompts_sha256()

    prompts[-1].write_text(prompts[-1].read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert library.prompts_sha256() != before


def git(path: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_library_commit_dirty_and_unknown(tmp_path: Path) -> None:
    """验收 3: 干净的工作区为 HEAD; 有未提交改动 (含未跟踪文件) 加 -dirty; 不在 git 仓库中为 unknown."""
    repo = tmp_path / "repo"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    head = git(repo, "rev-parse", "HEAD")
    assert ProcedureLibrary(repo).commit == head

    git(repo, "config", "status.showUntrackedFiles", "no")  # 仓库配置不显示未跟踪文件时同样算改动
    (repo / "prompts" / "NEW.md.j2").write_text("x", encoding="utf-8")
    assert ProcedureLibrary(repo).commit == f"{head}-dirty"

    plain = tmp_path / "plain"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, plain)
    assert ProcedureLibrary(plain).commit == "unknown"
