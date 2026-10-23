"""持久化的执行步骤: 记录可核验的操作状态, 不记录模型内部思维.

运行上下文通过 ContextVar 传播到同一次程序派生的并行任务, 步骤各有独立编号.
每个开始/终态均先写留痕再返回, 连接中断不丢历史; 失败只记录类别, 原因由原业务错误渠道呈报.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from backend.contracts.models import TrailEntry, TrailKind
from backend.contracts.ports import AuditTrail


@dataclass(frozen=True)
class ExecutionContext:
    trail: AuditTrail
    eid: str
    procedure_id: str
    run_id: str
    actor: str


CURRENT: ContextVar[ExecutionContext | None] = ContextVar("execution_context", default=None)


@contextmanager
def execution_scope(trail: AuditTrail, eid: str, procedure_id: str, run_id: str, actor: str) -> Iterator[None]:
    token = CURRENT.set(ExecutionContext(trail, eid, procedure_id, run_id, actor))
    try:
        yield
    finally:
        CURRENT.reset(token)


@asynccontextmanager
async def execution_step(
    stage: str, *, subject_id: str | None = None, attempt: int | None = None
) -> AsyncIterator[None]:
    """上下文之外不写步骤, 供同一引擎在独立校验等用途复用."""
    ctx = CURRENT.get()
    if ctx is None:
        yield
        return
    step_id = f"STEP-{uuid4()}"
    started = datetime.now(UTC)

    async def record(status: str) -> None:
        await ctx.trail.record(
            TrailEntry(
                engagement_id=ctx.eid,
                kind=TrailKind.execution,
                actor=ctx.actor,
                procedure_id=ctx.procedure_id,
                run_id=ctx.run_id,
                summary=f"{ctx.procedure_id}: {stage} ({status})",
                payload={
                    "step_id": step_id,
                    "stage": stage,
                    "status": status,
                    "subject_id": subject_id,
                    "attempt": attempt,
                    "started_at": started.isoformat(),
                    "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
                },
            )
        )

    await record("running")
    try:
        yield
    except BaseException:
        await record("failed")
        raise
    else:
        await record("completed")
