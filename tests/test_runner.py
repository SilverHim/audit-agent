"""EngagementRunner: 项目锁, 后台推进, 启动时固定集写入 Store. 依赖用 tests/fakes.py 的假实现."""

import asyncio
import inspect
from typing import Any

import pytest
from fastapi import FastAPI

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import AbnRecord
from backend.main import lifespan
from backend.orchestration import runner as runner_module
from backend.orchestration.runner import GraphRunner, fixed_procedures
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeRegistry,
    NoTaskAsyncio,
    make_runner,
    wait_idle,
)

EID = ENGAGEMENT.id


class BrokenRegistry(FakeRegistry):
    """ABN Lookup 不可用: 查询即抛错, 用来制造后台执行失败."""

    async def abn(self, abn: str) -> AbnRecord | None:
        raise RuntimeError("ABN Lookup 不可用")


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

    first = store.commits[0]  # start 写入的那一批, 在 p0_intel 之前
    assert [p.id for p in first] == [p.id for p in fixed_procedures(runner.deps.library)]
    assert all(p.version == 1 for p in first)


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
    runner, _ = make_runner(registry=BrokenRegistry())

    await runner.start(EID)
    await wait_idle(runner, EID)  # 后台任务不把异常抛给等待者

    assert not runner.is_running(EID)
    error = runner.run_error(EID)
    assert error is not None
    assert "ABN Lookup 不可用" in error


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
