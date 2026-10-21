"""需求 R361: 系统忙时的当前活动 (services.activities.ActivityBoard 与项目总览的 activities, last_failure).

登记规则直接测; 总览经 GraphStateQuery 读: 空闲为空, 程序执行中有一项 (P0 的 ACC-04 经 attempt; P2 并行的见 test_query
的 R325 用例), 节点出错后残留的由后台推进结束时清掉; 数据作业逐份推进 (载入演示资料的外勤 19 份, 读补造目录, 标
external), 结束后为空; 后台作业失败给原因, 同种作业再开始时清除. 为看进度, 用替身让每份文件 (每次 Agent 调用) 停住,
由测试逐个放行, 不靠时序.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.adapters.xero_supplements import plan
from backend.agents.ingest import DocumentIntake
from backend.contracts.models import AgentId, DocumentRef
from backend.contracts.views import Activity, ActivityFailure
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.activities import ActivityBoard
from backend.services.data_service import DataService
from backend.services.demo_import import DemoImport
from tests.external import needs
from tests.fakes import ENGAGEMENT, NUMBAT_PROFILE, FakeEngagementStore, make_runner, wait_idle
from tests.test_demo_import import ACTOR, env
from tests.test_documents_upload import setup as upload_setup
from tests.xero_fakes import XERO_DATASET
from tests.xero_flow import demo_source, drain

EID = ENGAGEMENT.id
T0 = datetime(2026, 10, 21, 9, tzinfo=UTC)
WAIT = 10  # 等替身停住的上限 (秒); 正常立即到, 只防卡死


def clock() -> tuple[ActivityBoard, Callable[[], None]]:
    """时钟可拨的登记处: 从 T0 起, tick 一次拨快一分钟."""
    now = [T0]

    def tick() -> None:
        now[0] += timedelta(minutes=1)

    return ActivityBoard(lambda: now[0]), tick


def test_board_starts_updates_finishes_and_clears() -> None:
    """开始时执行者与描述取种类的缺省 (给了的照给), 按开始的先后列出; 只有进度 (done, total, current) 变了 updated_at
    才动; 同键再开始即替换, 位置不变; 结束与按种类清掉只动该项目的."""
    board, tick = clock()
    board.start(EID, "a", "demo_import", subject_id="fieldwork")
    tick()
    board.start(EID, "b", "procedure", actor=AgentId.A6, description="执行 REV-08", current="REV-08")
    board.start("ENG-OTHER", "c", "xero_sync")
    assert [(a.kind, a.actor, a.description) for a in board.activities(EID)] == [
        ("demo_import", AgentId.A3, "载入演示资料"),
        ("procedure", AgentId.A6, "执行 REV-08"),
    ]
    tick()
    board.update(EID, "a", done=3, total=19, current="x.pdf")
    progressed = board.get(EID, "a")
    tick()
    board.update(EID, "a", subject_id="fieldwork")  # 不是进度, updated_at 不动
    board.update(EID, "missing", done=1)  # 没有这一项, 不做什么
    assert (
        progressed
        == board.get(EID, "a")
        == Activity(
            kind="demo_import",
            actor=AgentId.A3,
            description="载入演示资料",
            done=3,
            total=19,
            current="x.pdf",
            subject_id="fieldwork",
            started_at=T0,
            updated_at=T0 + timedelta(minutes=2),
        )
    )
    board.start(EID, "a", "import_batch", subject_id="BAT-1")
    assert [a.kind for a in board.activities(EID)] == ["import_batch", "procedure"]
    board.clear(EID, "procedure")
    board.finish(EID, "a")
    board.finish(EID, "a")
    assert board.activities(EID) == [] and [a.kind for a in board.activities("ENG-OTHER")] == ["xero_sync"]


def test_failure_kept_until_same_kind_starts_again() -> None:
    """最近一次失败取代此前的; 重试入口按种类 (没有的为空); 别的种类开始不清, 同种作业再开始时清除."""
    board, _ = clock()
    board.fail(EID, "document_intake", "RuntimeError: boom")
    assert board.last_failure(EID) == ActivityFailure(
        kind="document_intake", actor=AgentId.A3, reason="RuntimeError: boom", failed_at=T0
    )
    board.fail(EID, "import_batch", "LookupError: x", subject_id="BAT-1")
    board.start(EID, "k", "document_intake")
    failure = board.last_failure(EID)
    assert failure is not None and (failure.kind, failure.retry, failure.subject_id) == (
        "import_batch",
        "import_batch",
        "BAT-1",
    )
    board.start(EID, "k", "import_batch", subject_id="BAT-1")
    assert board.last_failure(EID) is None and board.last_failure("ENG-OTHER") is None


class Hold:
    """把一个异步方法换成先停住的替身: 进来时把 key(参数) 放进 entered, 等 proceed 放行一次再调原方法."""

    def __init__(self, original: Callable[..., Any], key: Callable[..., str]) -> None:
        self.original, self.key = original, key
        self.entered: asyncio.Queue[str] = asyncio.Queue()
        self.proceed: asyncio.Queue[None] = asyncio.Queue()

    def stand_in(self) -> Callable[..., Any]:
        async def held(*args: Any, **kwargs: Any) -> Any:
            await self.entered.put(self.key(*args))
            await self.proceed.get()
            return await self.original(*args, **kwargs)

        return held

    async def next(self) -> str:
        return await asyncio.wait_for(self.entered.get(), WAIT)


def overview_of(runner: GraphRunner) -> Any:
    return GraphStateQuery(runner.app, runner.deps, runner).overview(EID)


async def test_program_execution_is_an_activity_until_it_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 1, 2: 新项目启动后 ACC-04 调 Agent 时停住: 总览有一项程序执行 (执行者 A2, 当前为 ACC-04, 本阶段 0/2,
    描述含程序名与进度), 正在运行的程序同此; 放行后停在 ACC-01 闸门, 活动为空, 没有失败."""
    hold = Hold(GraphNodes.run_agent, lambda nodes, agent, ctx: ctx.run.procedure_id)
    monkeypatch.setattr(GraphNodes, "run_agent", hold.stand_in())
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]))
    await runner.start(EID)

    assert await hold.next() == "ACC-04"
    during = await overview_of(runner)
    name = runner.deps.library.load("ACC-04").name
    [activity] = during.activities
    assert (activity.kind, activity.actor, activity.current, activity.done, activity.total) == (
        "procedure",
        AgentId.A2,
        "ACC-04",
        0,
        2,
    )
    assert activity.description == f"执行 ACC-04 {name} (本阶段已完成 0/2)"
    assert during.running is True and during.progress.running == ["ACC-04"]
    await hold.proceed.put(None)
    await wait_idle(runner, EID)

    after = await overview_of(runner)
    assert (after.running, after.activities, after.last_failure, after.progress.running) == (False, [], None, [])
    assert after.current_gate is not None


async def test_activity_left_by_a_node_error_is_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行的终态写入后写结束留痕之前节点出错: 后台推进结束时清掉残留的程序执行, 错误照旧在 run_error."""

    async def broken(*args: Any) -> Any:
        raise RuntimeError("trail down")

    monkeypatch.setattr(GraphNodes, "run_finished_entry", broken)
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]))
    await runner.start(EID)
    await wait_idle(runner, EID)

    after = await overview_of(runner)
    assert after.activities == [] and after.run_error == "RuntimeError: trail down"


def file_name(intake: DocumentIntake, eid: str, document: DocumentRef, *args: Any) -> str:
    return document.file_name


@needs("q1_snapshot", "q1_supplements")
async def test_demo_import_advances_file_by_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 1, 3: 载入外勤演示资料 (Xero 演示项目, 还没有数据): 逐份处理 19 份文件时总览有一项 demo_import (执行者
    A3, 作业对象 fieldwork), done 为处理完的份数, total 为 19, current 为在处理的文件名, 按清单顺序推进; 做完后活动
    为空, 没有失败."""
    hold = Hold(DocumentIntake.resume, file_name)
    monkeypatch.setattr(DocumentIntake, "resume", hold.stand_in())
    e = env()
    names = [u.file_name for u in DemoImport(e.service, plan(demo_source(), XERO_DATASET)).uploads("fieldwork")]
    assert len(names) == 19

    await e.service.demo_import(EID, "fieldwork", ACTOR)
    for done, name in enumerate(names):
        assert await hold.next() == name
        [activity] = (await overview_of(e.runner)).activities
        assert (activity.kind, activity.actor, activity.subject_id) == ("demo_import", AgentId.A3, "fieldwork")
        assert (activity.done, activity.total, activity.current) == (done, 19, name)
        await hold.proceed.put(None)
    await drain(e.service)
    await wait_idle(e.runner, EID)

    after = await overview_of(e.runner)
    assert (after.activities, after.last_failure) == ([], None)


@needs("q1_snapshot", "q1_supplements")
async def test_demo_import_failure_is_reported_until_imported_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 2: 载入演示资料的作业中途出错 (按清单改类型时): 活动为空, 最近一次失败为 demo_import (原因原文, 作业对象
    fieldwork, 重试入口 demo_import); 再载入一次 (同种作业开始) 即清除, 做完后也没有失败."""
    retype = DemoImport.retype

    async def broken(*args: Any) -> Any:
        raise RuntimeError("retype broke")

    monkeypatch.setattr(DemoImport, "retype", broken)
    e = env()
    await e.service.demo_import(EID, "fieldwork", ACTOR)
    await drain(e.service)

    failed = await overview_of(e.runner)
    assert failed.activities == [] and failed.last_failure is not None
    assert failed.last_failure.model_dump(exclude={"failed_at"}) == {
        "kind": "demo_import",
        "actor": AgentId.A3,
        "reason": "RuntimeError: retype broke",
        "subject_id": "fieldwork",
        "retry": "demo_import",
    }
    monkeypatch.setattr(DemoImport, "retype", retype)
    await e.service.demo_import(EID, "fieldwork", ACTOR)
    assert (await overview_of(e.runner)).last_failure is None
    await drain(e.service)
    assert (await overview_of(e.runner)).last_failure is None


async def test_upload_failure_has_no_retry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """后台的文件处理作业在体检时漏出异常: 总览给原因 (document_intake, 执行者 A3, 没有现成的重试入口), 锁已放."""
    runner, service, *_ = upload_setup()

    async def boom(eid: str) -> None:
        raise OSError("checkup down")

    monkeypatch.setattr(service, "checkup", boom)
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await asyncio.gather(*list(service.tasks))

    overview = await overview_of(runner)
    assert overview.activities == [] and not runner.is_running(EID)
    assert overview.last_failure is not None
    assert (overview.last_failure.kind, overview.last_failure.reason, overview.last_failure.retry) == (
        "document_intake",
        "OSError: checkup down",
        None,
    )


def test_services_share_the_board() -> None:
    """数据管理服务, 节点与总览都经同一个 deps 取登记处 (dataclasses.replace 换端口时沿用)."""
    runner, service, *_ = upload_setup()
    assert isinstance(service, DataService) and service.deps.activities is runner.deps.activities
