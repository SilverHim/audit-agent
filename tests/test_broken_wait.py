"""等复核的节点出错后 (恢复用的决定 ID 不在 Store 中): 新的复核决定被拒, 按异常信息恢复后正常提交."""

from langgraph.types import Command

from backend.contracts.models import TrailEntry, TrailKind
from tests.fakes import FakeTrail, wait_idle
from tests.test_await_events import confirm, waiting_for_review
from tests.test_decisions import Started
from tests.test_review_decisions import CONFIG, EID, assert_rejected, record


def decision_entries(s: Started) -> list[TrailEntry]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.decision]


async def broken_wait(s: Started) -> None:
    """经后台入口以不在 Store 中的 D-404 恢复: await_events 抛错, 记入 run_error, 锁释放; 节点保留等待与错误."""
    s.runner.acquire(EID)
    s.runner.launch(EID, Command(resume="D-404"))
    await wait_idle(s.runner, EID)
    error = s.runner.run_error(EID)
    assert error is not None and "D-404" in error
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert [(t.name, bool(t.interrupts), t.error is not None) for t in snapshot.tasks] == [("await_events", True, True)]


async def test_decision_rejected_while_waiting_node_broken() -> None:
    """等复核的节点出错后, 它每次恢复都取回 D-404 再出错: 新决定被拒 (说明出错原因与须先恢复), 不写 Store, 不留痕."""
    runner, store = await waiting_for_review()
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    await broken_wait(s)
    trail_before = len(decision_entries(s))

    await assert_rejected(s, record(), "NOT_AWAITING_REVIEW", "D-404")
    assert len(decision_entries(s)) == trail_before


async def test_recovery_steps_from_error_message() -> None:
    """按异常信息恢复: 只重写检查点时还没有新的等待, 提交被拒; 再让图继续执行一步, 重新停到等复核,
    已完成的程序不重跑, Store 不变; 之后以写入 Store 的决定恢复, 正常读回."""
    runner, store = await waiting_for_review()
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    await broken_wait(s)
    values, rows = (await runner.app.aget_state(CONFIG)).values, list(store.rows)

    await runner.app.aupdate_state(CONFIG, {}, as_node="fan_in")
    await assert_rejected(s, record(), "NOT_AWAITING_REVIEW")
    runner.acquire(EID)
    runner.launch(EID, None)
    await wait_idle(runner, EID)

    snapshot = await runner.app.aget_state(CONFIG)
    assert runner.run_error(EID) is None
    assert snapshot.values == values and store.rows == rows
    assert [(t.name, t.error) for t in snapshot.tasks if t.interrupts] == [("await_events", None)]
    decision = await confirm(store, 1)  # 决定写入 Store 的对象 (与 DecisionSubmit 写的一致)
    runner.acquire(EID)
    runner.launch(EID, Command(resume=decision.id))
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    assert decision in (await runner.app.aget_state(CONFIG)).values["review_records"]
