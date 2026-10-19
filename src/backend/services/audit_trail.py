"""Append-only audit trail, always committed independently of business writes."""

from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import insert, literal, select, tuple_

from backend.contracts.models import TrailEntry, TrailKind
from backend.persistence.db import transaction
from backend.persistence.models import audit_trail
from backend.persistence.stores import Store, now


class PostgresAuditTrail(Store):
    async def record(self, entry: TrailEntry) -> str:
        item = entry.model_copy(update={"id": entry.id or f"TR-{uuid4()}", "created_at": entry.created_at or now()})
        async with transaction(self.engine, entry.engagement_id, independent=True) as conn:
            await conn.execute(insert(audit_trail).values(**item.model_dump()))
        assert item.id is not None
        return item.id

    async def get(self, eid: str, id: str) -> TrailEntry | None:
        rows = await self.rows(eid, select(audit_trail).where(audit_trail.c.id == id))
        return TrailEntry.model_validate(rows[0]) if rows else None

    async def find_success(self, eid: str, idempotency_key: str) -> TrailEntry | None:
        rows = await self.rows(
            eid,
            select(audit_trail)
            .where(audit_trail.c.idempotency_key == idempotency_key)
            .order_by(audit_trail.c.created_at, audit_trail.c.id),
        )
        for row in rows:
            payload = row["payload"]
            if (
                not payload.get("error")
                and payload.get("success") is not False
                and payload.get("status") not in {"failed", "error"}
            ):
                return TrailEntry.model_validate(row)
        return None

    async def list(
        self,
        eid: str,
        kind: TrailKind | None = None,
        limit: int = 100,
        offset: int = 0,
        *,
        newest: bool = False,
        before: str | None = None,
    ) -> list[TrailEntry]:
        if limit < 0 or offset < 0:
            raise ValueError("Negative pagination")
        order = (audit_trail.c.created_at, audit_trail.c.id)
        stmt = select(audit_trail)
        if kind is not None:
            stmt = stmt.where(audit_trail.c.kind == kind)
        if before is not None:
            # 游标: 排序键 (created_at, id) 小于游标那条的; 游标不是本项目的 (行级安全下查不到) 时比较为空, 一条不取
            at = select(audit_trail.c.created_at).where(audit_trail.c.id == before).scalar_subquery()
            stmt = stmt.where(tuple_(*order) < tuple_(at, literal(before)))
        if newest:
            stmt = stmt.order_by(*(c.desc() for c in order)).limit(limit).offset(offset)
            return [TrailEntry.model_validate(r) for r in reversed(await self.rows(eid, stmt))]
        stmt = stmt.order_by(*order).limit(limit).offset(offset)
        return [TrailEntry.model_validate(r) for r in await self.rows(eid, stmt)]

    async def last_of(self, eid: str, kind: TrailKind, actions: Sequence[str]) -> TrailEntry | None:
        if not actions:
            return None
        stmt = (
            select(audit_trail)
            .where(audit_trail.c.kind == kind, audit_trail.c.payload["action"].astext.in_(list(actions)))
            .order_by(audit_trail.c.created_at.desc(), audit_trail.c.id.desc())
            .limit(1)
        )
        rows = await self.rows(eid, stmt)
        return TrailEntry.model_validate(rows[0]) if rows else None
