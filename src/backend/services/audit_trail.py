"""审计留痕（B 负责）：只追加，独立事务写入。"""

from backend.contracts.models import TrailEntry, TrailKind


class PostgresAuditTrail:
    async def record(self, entry: TrailEntry) -> str:
        raise NotImplementedError

    async def get(self, eid: str, id: str) -> TrailEntry | None:
        raise NotImplementedError

    async def find_success(self, eid: str, idempotency_key: str) -> TrailEntry | None:
        raise NotImplementedError

    async def list(
        self, eid: str, kind: TrailKind | None = None, limit: int = 100, offset: int = 0
    ) -> list[TrailEntry]:
        raise NotImplementedError
