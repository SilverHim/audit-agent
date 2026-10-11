"""Append-only audit trail, always committed independently of business writes."""

from uuid import uuid4

from sqlalchemy import insert, select

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
        self, eid: str, kind: TrailKind | None = None, limit: int = 100, offset: int = 0
    ) -> list[TrailEntry]:
        if limit < 0 or offset < 0:
            raise ValueError("Negative pagination")
        stmt = select(audit_trail).order_by(audit_trail.c.created_at, audit_trail.c.id).limit(limit).offset(offset)
        if kind is not None:
            stmt = stmt.where(audit_trail.c.kind == kind)
        return [TrailEntry.model_validate(r) for r in await self.rows(eid, stmt)]
