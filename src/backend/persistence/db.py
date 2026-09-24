"""数据库连接。每个事务开头设 app.engagement_id，行级安全按它过滤。"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from backend.core.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.sqlalchemy_url, pool_pre_ping=True)


async def bind_engagement(conn: AsyncConnection, eid: str) -> None:
    await conn.execute(text("SELECT set_config('app.engagement_id', :eid, true)"), {"eid": eid})
