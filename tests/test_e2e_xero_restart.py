"""需求 R262 (按 Q32 改写为演示配置): 演示走通 (tests/test_demo_walkthrough 的 walk, 只经 HTTP 照页面走) 中途中断后,
用同一检查点与存储起新进程 (新的主图, runner, 数据服务与接口客户端, 经组装根的启动恢复 main.recover), 照页面接着走到
归档; 终态, 计数与留痕同不中断时 (assert_end_state).

三处中断 (仿 R57, 都在演示配置下有; 手法取 tests/interrupts):
- P2 发函后: 第 0 层汇合进检查点后 DOC-06 执行中 (BlockingIngest 卡住) 取消后台任务 (kill).
- 计划闸门: PLN-15 批准写进 Store 后, 越过闸门 (写检查点) 前中断 (CrashingTrail).
- 复核: BA-091 的认定错报决定写进 Store 后, 交给等待的节点前中断 (CrashingTrail).
决定写进 Store 后中断时, 检查点没跟上: 恢复前再提交报 RESUME_REQUIRED, 恢复补做 (补写留痕, 越过闸门或交给等待的
节点). 三种都: 函证只发 2 次, 第 0 层程序都只有 RUN-x-1, llm_call 与 external_call 的行数同不中断时 (含在
assert_end_state 里). 被杀时 DOC-06 已进入执行而没调大模型, 恢复后重做 (同一执行编号, 允许重跑, R57-01): Agent 多调
一次, 不计入 assert_end_state 的执行次数.

读快照与补造目录, 标 external; 离线 (内存检查点与假存储, 同走通的 walked).
"""

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest

from backend.agents.base import AgentDeps, ProcedureAgent
from backend.main import recover
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner, resume_required
from backend.services.data_service import DataService
from tests.fakes import NUMBAT_AUDITOR, FakeConfirmationChannel, api_client
from tests.fixture_sets import XERO
from tests.interrupts import CRASH, BlockingIngest, CrashingTrail, kill
from tests.test_demo_walkthrough import BA091, Session, Walk, assert_end_state, walk
from tests.xero_flow import API, demo_offline, drain, xero_link
from tests.xero_harness import LayeredMockLLMClient

pytestmark = pytest.mark.external
LAYER0 = ["CSH-04", "CSH-05", "GEN-05", "REV-03", "REV-08"]  # 演示配置下 P2 的第 0 层 (DOC-06 在其后)
Crash = Callable[[str, dict[str, Any]], bool]  # 哪个决定写进 Store 后中断: (接口路径, 请求体) → 是否


@dataclass
class Restarting(Session):
    """会被中断的走通进程: block 为卡住的 DOC-06 (卡住时杀掉), crash 选出写进 Store 后即中断的那个决定; 两者都只用
    一次, 中断后由 restart 换新进程接着走. layer0 记下被杀时检查点中第 0 层的执行, crashed 记下中断的决定."""

    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    block: BlockingIngest | None = None
    crash: Crash | None = None
    layer0: list[str] = field(default_factory=list)
    crashed: list[str] = field(default_factory=list)
    restarts: int = 0

    async def settle(self, eid: str) -> None:
        """同 Session.settle; DOC-06 卡住时 (第 0 层已汇合) 杀掉进程, 换新进程后再等它停下."""
        await drain(self.service)
        task = self.runner.tasks.get(eid)
        if task is None:
            return
        if self.block is not None:
            entered = asyncio.ensure_future(self.block.entered.wait())
            await asyncio.wait({task, entered}, return_when=asyncio.FIRST_COMPLETED)
            entered.cancel()
            if self.block.entered.is_set():
                values = (await self.runner.app.aget_state(GraphRunner.config(eid))).values
                self.layer0 = sorted(r.id for r in values["procedure_runs"] if r.procedure_id in LAYER0)
                await kill(self.runner, eid)
                self.block = None
                await self.restart()
                return await self.settle(eid)
        await task

    async def submit(self, path: str, body: dict[str, Any]) -> None:
        """同 Session.submit; crash 选中的决定写进 Store 后中断 (decision 留痕抛错): 检查点没跟上, 恢复要补做的正是它,
        这时再提交报 RESUME_REQUIRED; 换新进程 (恢复补做) 后不再提交, 走法接着等它停下."""
        if self.crash is None or not self.crash(path, body):
            return await super().submit(path, body)
        self.crash = None
        deps = self.runner.deps
        trail = cast(CrashingTrail, deps.trail)
        trail.fail_next = True
        with pytest.raises(RuntimeError, match=CRASH):
            await self.client.post(path, json=body)
        eid = path.removeprefix(f"{API}/engagements/").split("/")[0]
        engagement = await deps.engagement_store.get(eid)
        assert engagement is not None
        pending = await resume_required(deps, engagement, await self.runner.app.aget_state(GraphRunner.config(eid)))
        assert pending is not None and await trail.find_success(eid, pending) is None
        again = await self.client.post(path, json=body)
        assert (again.status_code, again.json()["detail"]["code"]) == (409, "RESUME_REQUIRED")
        self.crashed.append(pending)
        await self.restart()

    async def restart(self) -> None:
        """进程重启: 同一检查点与存储上新编主图 (真实 Agent), 新 runner 与数据服务, 经组装根的启动恢复 (main.recover:
        先补做文件处理, 再按 recover 规则拉起图), 接口客户端换成新进程的."""
        deps = self.runner.deps
        runner = GraphRunner(compile_graph(GraphNodes(deps, 2), self.runner.app.checkpointer), deps)
        service = DataService(deps, runner, demo=self.service.demo)
        await recover(runner, service)
        client = api_client(runner, NUMBAT_AUDITOR.id, xero=xero_link(deps, service))
        self.client = await self.stack.enter_async_context(client)
        self.runner, self.service = runner, service
        self.restarts += 1


async def interrupted_walk(*, block: bool = False, crash: Crash | None = None) -> tuple[Walk, Restarting]:
    """离线走通 (同 walked 的装配, 工作语言 en), 留痕为 CrashingTrail; block 时 DOC-06 换成 BlockingIngest."""
    XERO.ready()
    llm = LayeredMockLLMClient([XERO.runtime])
    blocking: list[BlockingIngest] = []

    def agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        blocking.append(BlockingIngest(deps))
        return {"DOC-06": blocking[0]}

    async with AsyncExitStack() as stack:
        offline = demo_offline(llm, trail=CrashingTrail(), agents=agents if block else None)
        runner, service, client = await stack.enter_async_context(offline)
        session = Restarting(client, runner, service, stack, blocking[0] if block else None, crash)
        return await walk(session, llm), session


async def assert_recovered(w: Walk, s: Restarting) -> None:
    """三种中断共同的: 只重启一次; 函证共发 2 次; 检查点中第 0 层程序都只有第 1 次执行; 其余同不中断时
    (assert_end_state, 含 llm_call 与 external_call 的行数)."""
    assert s.restarts == 1
    channel = s.runner.deps.confirmations
    assert isinstance(channel, FakeConfirmationChannel) and len(channel.submits) == 2
    values = (await s.runner.app.aget_state(GraphRunner.config(w.x.eid))).values
    assert sorted(r.id for r in values["procedure_runs"] if r.procedure_id in LAYER0) == [f"RUN-{p}-1" for p in LAYER0]
    await assert_end_state(w)


async def test_killed_during_doc06_recovers() -> None:
    """P2 发函后: 第 0 层已汇合进检查点 (函证已发 2 次), DOC-06 执行中被杀; 新进程恢复后接着走到归档, 结果同不中断时.
    被杀的那次 DOC-06 只进了 Agent, 没调大模型, 恢复后由真实的 IngestAgent 重做."""
    w, s = await interrupted_walk(block=True)
    assert s.layer0 == [f"RUN-{p}-1" for p in LAYER0]
    assert [(pid, name) for pid, name, _ in w.x.agents if pid == "DOC-06"] == [
        ("DOC-06", "BlockingIngest"),
        ("DOC-06", "IngestAgent"),
    ]
    w.x = replace(w.x, agents=[a for a in w.x.agents if a[1] != "BlockingIngest"])
    await assert_recovered(w, s)


def plan_approval(path: str, body: dict[str, Any]) -> bool:
    return path.endswith("/gate-decisions") and (body["gate"], body["decision"]) == ("PLN-15", "approve")


def ba091_misstatement(path: str, body: dict[str, Any]) -> bool:
    return path.endswith("/review-records") and body["task_id"] == f"T-{BA091}-v1"


@pytest.mark.parametrize("crash", [plan_approval, ba091_misstatement], ids=["plan_approval", "review"])
async def test_decision_in_store_only_recovers(crash: Crash) -> None:
    """计划闸门 PLN-15 的批准, 或 BA-091 的认定错报, 写进 Store 后即中断: 再提交报 RESUME_REQUIRED; 新进程恢复补做
    (补写留痕, 越过闸门或交给等待的节点) 后接着走到归档, 结果同不中断时."""
    w, s = await interrupted_walk(crash=crash)
    assert len(s.crashed) == 1
    await assert_recovered(w, s)
