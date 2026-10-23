"""事件游标必须补齐跨页事件; 并行程序的步骤不能串到其他项目或执行."""

import asyncio

from backend.api.routes.execution import events_since
from backend.contracts.models import TrailEntry, TrailKind
from backend.services.execution import execution_scope, execution_step
from tests.fakes import FakeTrail


async def test_reconnect_replays_more_than_one_page_without_duplicates() -> None:
    trail = FakeTrail()
    ids = [
        await trail.record(TrailEntry(engagement_id="E", kind=TrailKind.execution, actor="A6", summary=str(i)))
        for i in range(235)
    ]
    assert [e.id for e in await events_since(trail, "E", ids[3])] == ids[4:]
    assert len(await events_since(trail, "E", None)) == 100
    assert await events_since(trail, "E", ids[-1]) == []
    assert await events_since(trail, "another", None) == []


async def test_parallel_step_context_is_isolated_and_failures_are_terminal() -> None:
    trail = FakeTrail()

    async def run(eid: str, fail: bool) -> None:
        with execution_scope(trail, eid, "REV-02", f"RUN-{eid}", "A6"):
            async with execution_step("model", subject_id=eid):
                await asyncio.sleep(0)
                if fail:
                    raise ValueError("private model response must not appear in progress")

    result = await asyncio.gather(run("E1", False), run("E2", True), return_exceptions=True)
    assert result[0] is None and isinstance(result[1], ValueError)
    for eid, status in [("E1", "completed"), ("E2", "failed")]:
        entries = await trail.list(eid, TrailKind.execution)
        assert [e.payload["status"] for e in entries] == ["running", status]
        assert len({e.payload["step_id"] for e in entries}) == 1
        assert all(e.run_id == f"RUN-{eid}" and e.payload["subject_id"] == eid for e in entries)
        assert all("private" not in str(e.payload) for e in entries)


async def test_data_job_history_survives_active_board_recreation() -> None:
    from backend.services.activities import ActivityBoard

    trail = FakeTrail()
    board = ActivityBoard(trail=trail)
    board.start("E", "import", "demo_import", done=0, total=2)
    board.update("E", "import", done=1, current="contract.pdf")
    board.fail("E", "demo_import", "Injected failure")
    board.finish("E", "import")
    await board.settle()
    assert ActivityBoard(trail=trail).activities("E") == []
    entries = await events_since(trail, "E", None)
    assert [e.payload["status"] for e in entries] == ["running", "running", "failed"]
    assert len({e.payload["step_id"] for e in entries}) == 1
    assert entries[-1].payload["done"] == 1
