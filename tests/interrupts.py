"""重启测试共用的中断手法 (普通辅助模块, 不是测试模块): 进程被杀, 执行中卡住, 决定写进 Store 后中断.

- kill: 取消项目的后台任务, 不做任何收尾 (模拟进程被杀).
- BlockingIngest: DOC-06 第一次执行在调大模型之前卡住 (entered 置位), 等着被杀; 之后照常 (R57, R262 的 "P2 发函后").
- CrashingTrail: fail_next 为真时, 下一条 decision 留痕写入即抛 RuntimeError(CRASH) (只一次): 决定的业务事务已提交,
  之后的留痕, 检查点与交给后台都没做, 模拟进程在此中断 (两轮错报闭环的图测试, R262 的闸门与复核决定).
用的有 tests/test_e2e_restart (R57), test_evidence_gen08, test_p3_loop 与 test_e2e_xero_restart (R262).
"""

import asyncio

from backend.agents.base import AgentDeps, RunContext
from backend.agents.ingest import Extracted, IngestAgent
from backend.contracts.models import TrailEntry, TrailKind
from backend.orchestration.runner import GraphRunner
from tests.fakes import FakeTrail

CRASH = "留痕库断开"  # CrashingTrail 抛出的异常


async def kill(runner: GraphRunner, eid: str) -> None:
    """模拟进程被杀: 取消该项目的后台任务, 不做任何收尾."""
    task = runner.tasks[eid]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class BlockingIngest(IngestAgent):
    """DOC-06 第一次执行在调大模型之前卡住 (entered 置位), 之后照常; 其余程序照常."""

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        self.entered = asyncio.Event()  # 第一次 DOC-06 已进入执行

    async def execute(self, ctx: RunContext) -> list[Extracted]:
        if ctx.procedure.id == "DOC-06" and not self.entered.is_set():
            self.entered.set()
            await asyncio.Event().wait()
        return await super().execute(ctx)


class CrashingTrail(FakeTrail):
    """fail_next 为真时, 下一条 decision 留痕写入即抛 RuntimeError(CRASH) (只一次): 决定的业务事务已提交, 之后什么
    都没做 (留痕, 迁移留痕, 交给后台), 模拟进程在此中断."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def record(self, entry: TrailEntry, *, independent: bool = True) -> str:
        if self.fail_next and entry.kind is TrailKind.decision:
            self.fail_next = False
            raise RuntimeError(CRASH)
        return await super().record(entry, independent=independent)
