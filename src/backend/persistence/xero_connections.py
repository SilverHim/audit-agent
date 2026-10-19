"""XeroConnectionStore 的 PostgreSQL 实现 (表 xero_connections, 需求 R279): 每个项目最多一行, 令牌只存密文."""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from backend.contracts.data import XeroConnection
from backend.persistence import models as m
from backend.persistence.db import transaction
from backend.persistence.stores import Store

TOKENS = "tokens_encrypted"  # 密文列, 不进 XeroConnection


class PostgresXeroConnectionStore(Store):
    """项目的 Xero 连接. 行级安全按 app.engagement_id 隔离 (transaction 绑定); 不查项目锁定 (见端口说明)."""

    async def get(self, eid: str) -> XeroConnection | None:
        t = m.xero_connections
        columns = [t.c[name] for name in XeroConnection.model_fields]
        rows = await self.rows(eid, select(*columns).where(t.c.engagement_id == eid))
        return XeroConnection.model_validate(rows[0]) if rows else None

    async def tokens(self, eid: str) -> str | None:
        t = m.xero_connections
        rows = await self.rows(eid, select(t.c[TOKENS]).where(t.c.engagement_id == eid))
        return str(rows[0][TOKENS]) if rows else None

    async def put(self, eid: str, connection: XeroConnection, tokens_encrypted: str) -> None:
        values = self.values(connection) | {TOKENS: tokens_encrypted}
        t = m.xero_connections
        statement = insert(t).values(engagement_id=eid, **values)
        statement = statement.on_conflict_do_update(index_elements=[t.c.engagement_id], set_=values)
        async with transaction(self.engine, eid) as conn:
            await conn.execute(statement)

    async def update(self, eid: str, connection: XeroConnection) -> None:
        await self.change(eid, self.values(connection))

    async def set_tokens(self, eid: str, tokens_encrypted: str, refreshed_at: datetime) -> None:
        await self.change(eid, {TOKENS: tokens_encrypted, "refreshed_at": refreshed_at})

    async def delete(self, eid: str) -> bool:
        t = m.xero_connections
        async with transaction(self.engine, eid) as conn:
            result = await conn.execute(delete(t).where(t.c.engagement_id == eid))
        return bool(result.rowcount)

    async def change(self, eid: str, values: dict[str, Any]) -> None:
        """改该项目连接行的几列; 没有行抛 LookupError."""
        t = m.xero_connections
        async with transaction(self.engine, eid) as conn:
            result = await conn.execute(update(t).where(t.c.engagement_id == eid).values(**values))
        if not result.rowcount:
            raise LookupError(f"{eid} 没有 Xero 连接")

    @staticmethod
    def values(connection: XeroConnection) -> dict[str, Any]:
        """连接对象 → 列值 (JSON 列存成列表)."""
        return connection.model_dump(mode="json") | {
            "connected_at": connection.connected_at,
            "refreshed_at": connection.refreshed_at,
            "last_synced_at": connection.last_synced_at,
        }
