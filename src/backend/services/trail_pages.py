"""按类型读完一个项目的留痕: AuditTrail.list 每次最多返回 limit 条, 这里逐页取到不足一页为止."""

from backend.contracts.models import TrailEntry, TrailKind
from backend.contracts.ports import AuditTrail

PAGE = 500  # 每页条数


async def all_of_kind(trail: AuditTrail, eid: str, kind: TrailKind) -> list[TrailEntry]:
    """项目 eid 中 kind 类的全部留痕, 按写入顺序 (created_at, id)."""
    entries: list[TrailEntry] = []
    while True:
        page = await trail.list(eid, kind, limit=PAGE, offset=len(entries))
        entries += page
        if len(page) < PAGE:
            return entries
