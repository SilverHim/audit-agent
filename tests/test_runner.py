"""EngagementRunner: 项目锁, 后台推进, 启动时固定集写入 Store. 依赖用 tests/fakes.py 的假实现."""

import pytest

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import AbnRecord
from backend.orchestration.runner import GraphRunner, fixed_procedures
from tests.fakes import ENGAGEMENT, NUMBAT_PROFILE, FakeEngagementStore, FakeRegistry, make_runner, wait_idle

EID = ENGAGEMENT.id


class BrokenRegistry(FakeRegistry):
    """ABN Lookup 不可用: 查询即抛错, 用来制造后台执行失败."""

    async def abn(self, abn: str) -> AbnRecord | None:
        raise RuntimeError("ABN Lookup 不可用")


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

    with pytest.raises(DecisionRejected) as e:
        await runner.start(EID)
    assert e.value.code == "ENGAGEMENT_BUSY"

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
