"""A10 派发人工程序 (需求 R93, 接口文档第 6 节): engine manual 且不是闸门的程序, 执行即派一条 manual 任务给人.

材料由模板 owner 的 ManualHandler.prepare 生成, 任务的说明, 结果模型与证据下限取模板 manual 段 (ReviewQueue.make_task).
本次执行记录置 succeeded; 节点按通用等待停在节点内, 人提交 manual_result 后由节点调 owner.collect 整理结果.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from backend.agents.base import AgentDeps, ManualHandler, RunContext, finish_run
from backend.contracts.models import TaskKind
from backend.contracts.state import StatePatch

Prepare = Callable[[AgentDeps, RunContext], Awaitable[dict[str, Any]]]  # 执行: 取派发材料
Dispatch = Callable[[RunContext, dict[str, Any]], StatePatch]  # 解读: 执行记录置 succeeded, 建 manual 任务


def dispatcher(owner: ManualHandler, deps: AgentDeps) -> tuple[Prepare, Dispatch]:
    """某人工程序的 (执行, 解读): 执行调 owner.prepare 一次取材料, 解读把本次执行置 succeeded 并建 manual 任务.
    返回值的形式同 agent.HANDLERS 的一行."""

    async def prepare(_: AgentDeps, ctx: RunContext) -> dict[str, Any]:
        return owner.prepare(ctx)

    def dispatch(ctx: RunContext, material: dict[str, Any]) -> StatePatch:
        now = datetime.now(UTC)
        run = finish_run(ctx.run, now)
        task = deps.queue.make_task(TaskKind.manual, run, ctx.procedure.id, run.id, now, material=material)
        return {"procedure_runs": [run], "review_tasks": [task]}

    return prepare, dispatch
