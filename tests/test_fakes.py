"""测试替身自身的契约: 假存储须忠实于开发接口文档第 4 节, 否则依赖它们的测试会掩盖真实 Store 下的错误."""

import asyncio

import pytest

from backend.contracts.errors import VersionConflict
from backend.contracts.models import Phase
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    EngagementWrite,
    FakeEngagementStore,
    FakeOutputStore,
    bind_stores,
    make_procedure,
)

EID = ENGAGEMENT.id
CLIENT = NUMBAT_PROFILE.id
PROFILE_V2 = NUMBAT_PROFILE.model_copy(update={"version": 2, "legal_name": "Numbat Electronics Pty Ltd"})


def bound_stores() -> tuple[FakeEngagementStore, FakeOutputStore]:
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    output_store = FakeOutputStore()
    bind_stores(engagement_store, output_store)
    return engagement_store, output_store


async def test_engagement_writes_staged_then_rolled_back() -> None:
    """事务块内改项目与存画像: 块内读得到, 块内抛错后项目, 画像与提交日志都恢复原样."""
    engagement_store, output_store = bound_stores()

    with pytest.raises(RuntimeError, match="提交前出错"):
        async with output_store.transaction(EID):
            await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
            await engagement_store.update(EID, phase=Phase.P1_Planning, locked=True)
            await engagement_store.save_profile(PROFILE_V2)
            staged = await engagement_store.get(EID)
            assert staged is not None and staged.phase is Phase.P1_Planning and staged.locked
            assert await engagement_store.client_profile(CLIENT) == PROFILE_V2
            raise RuntimeError("提交前出错")

    assert await engagement_store.get(EID) == ENGAGEMENT
    assert await engagement_store.client_profile(CLIENT) == NUMBAT_PROFILE
    assert engagement_store.profiles == [NUMBAT_PROFILE]
    assert output_store.rows == []
    assert output_store.commits == []


async def test_engagement_writes_committed_with_outputs() -> None:
    """块正常结束: 项目与画像改动生效, 与审计产出按调用顺序记进同一个提交."""
    engagement_store, output_store = bound_stores()
    procedure = make_procedure("ACC-04")

    async with output_store.transaction(EID):
        await output_store.put(EID, [procedure], actor="A1")
        await engagement_store.save_profile(PROFILE_V2)
        await engagement_store.update(EID, phase=Phase.P1_Planning)

    engagement = await engagement_store.get(EID)
    assert engagement is not None and engagement.phase is Phase.P1_Planning
    assert await engagement_store.client_profile(CLIENT) == PROFILE_V2
    assert output_store.commits == [
        [
            procedure,
            EngagementWrite("save_profile", CLIENT, {"version": 2}),
            EngagementWrite("update", EID, {"phase": Phase.P1_Planning}),
        ]
    ]


async def test_writes_outside_transaction_take_effect_at_once() -> None:
    engagement_store, output_store = bound_stores()

    await engagement_store.update(EID, phase=Phase.P1_Planning)

    engagement = await engagement_store.get(EID)
    assert engagement is not None and engagement.phase is Phase.P1_Planning
    assert output_store.commits == []  # 不在事务里的项目写入不记提交日志


async def test_child_task_is_not_in_parent_transaction() -> None:
    """事务块内新建的子任务继承了 contextvar, 但不在父任务的事务里: 读不到暂存, 自己的写入立即提交."""
    engagement_store, output_store = bound_stores()

    async def child() -> None:
        assert await output_store.get(EID, type(make_procedure("ACC-04")), "ACC-04") is None
        assert await engagement_store.client_profile(CLIENT) == NUMBAT_PROFILE
        await output_store.put(EID, [make_procedure("ACC-01")], actor="A1")

    async with output_store.transaction(EID):
        await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
        await engagement_store.save_profile(PROFILE_V2)
        await asyncio.create_task(child())
        assert [p.id for p in output_store.rows] == ["ACC-01"]  # 子任务的写入已提交, 父任务的还在暂存

    assert [p.id for p in output_store.rows] == ["ACC-01", "ACC-04"]


async def test_late_child_does_not_join_finished_transaction() -> None:
    """子任务等外层事务结束后才开事务: 开的是自己的新事务, 写入生效, 旧事务的提交日志不变."""
    _, output_store = bound_stores()
    outer_done = asyncio.Event()

    async def late_child() -> None:
        await outer_done.wait()
        async with output_store.transaction(EID):
            await output_store.put(EID, [make_procedure("ACC-01")], actor="A1")

    async with output_store.transaction(EID):
        await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
        child = asyncio.create_task(late_child())
    outer_commit = list(output_store.commits[0])
    outer_done.set()
    await child

    assert [p.id for p in output_store.rows] == ["ACC-04", "ACC-01"]
    assert output_store.commits[0] == outer_commit
    assert len(output_store.commits) == 2


async def test_nested_transaction_joins_outer() -> None:
    _, output_store = bound_stores()

    async with output_store.transaction(EID):
        await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
        async with output_store.transaction(EID):
            await output_store.put(EID, [make_procedure("ACC-01")], actor="A1")

    assert len(output_store.commits) == 1
    assert [p.id for p in output_store.commits[0]] == ["ACC-04", "ACC-01"]


async def test_outputs_isolated_by_engagement() -> None:
    """两个项目各有同 ID 的程序: 互不可见, 版本号各自从 1 起."""
    output_store = FakeOutputStore()

    await output_store.put("ENG-A", [make_procedure("ACC-04")], actor="A1")
    await output_store.put("ENG-B", [make_procedure("ACC-04")], actor="A1")

    assert [p.id for p in output_store.rows_of("ENG-A")] == ["ACC-04"]
    assert [p.id for p in output_store.rows_of("ENG-B")] == ["ACC-04"]
    assert await output_store.get("ENG-C", type(make_procedure("ACC-04")), "ACC-04") is None
    with pytest.raises(VersionConflict):
        await output_store.put("ENG-A", [make_procedure("ACC-04", version=3)], actor="A1")


async def test_writing_other_engagement_inside_transaction_is_an_error() -> None:
    _, output_store = bound_stores()

    with pytest.raises(AssertionError, match="ENG-OTHER"):
        async with output_store.transaction(EID):
            await output_store.put("ENG-OTHER", [make_procedure("ACC-04")], actor="A1")
