"""数据库连接. 每个事务开头设 app.engagement_id, 行级安全按它过滤."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from backend.core.config import Settings, get_settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.sqlalchemy_url, pool_pre_ping=True)


async def bind_engagement(conn: AsyncConnection, eid: str) -> None:
    await conn.execute(text("SELECT set_config('app.engagement_id', :eid, true)"), {"eid": eid})


# The task identity prevents child tasks from inheriting a parent's live connection.
_active: ContextVar[tuple[AsyncEngine, str, AsyncConnection, object] | None] = ContextVar(
    "database_transaction", default=None
)


@lru_cache
def default_engine() -> AsyncEngine:
    return make_engine(get_settings())


@asynccontextmanager
async def transaction(engine: AsyncEngine, eid: str, *, independent: bool = False) -> AsyncIterator[AsyncConnection]:
    active = _active.get()
    if not independent and active and active[0] is engine and active[3] is asyncio.current_task():
        if active[1] != eid:
            raise ValueError("Cannot switch engagement within a transaction")
        async with active[2].begin_nested():
            yield active[2]
        return
    async with engine.begin() as conn:
        await bind_engagement(conn, eid)
        token = _active.set((engine, eid, conn, asyncio.current_task()))
        try:
            yield conn
        finally:
            _active.reset(token)


async def writable(conn: AsyncConnection, eid: str) -> None:
    from backend.contracts.errors import EngagementLocked

    row = (
        await conn.execute(text("SELECT locked FROM engagements WHERE id=:eid FOR NO KEY UPDATE"), {"eid": eid})
    ).first()
    if row is None:
        raise LookupError(eid)
    if row.locked:
        raise EngagementLocked(eid)
