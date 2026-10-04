"""状态迁移留痕 (A 负责): 项目阶段与程序状态每变一次, 在业务事务提交后写一行 transition (Demo需求文档第 4.2 节).

防重复键 (idempotency_key):
- 程序状态: {eid}:transition:procedure:{程序编号}:v{新版本号}
- 项目阶段: {eid}:transition:engagement:{eid}:{新阶段}
已有同键的留痕即跳过, 节点重放与恢复补做时不重复写. 首版与状态未变的新版本不写.
"""

from collections.abc import Iterable

from backend.contracts.models import AgentId, Phase, Procedure, TrailEntry, TrailKind
from backend.contracts.ports import AuditOutputStore, AuditTrail


def transition(
    eid: str, obj: str, oid: str, before: str, after: str, suffix: str, version: int | None, procedure_id: str | None
) -> TrailEntry:
    """一行 transition 留痕. obj: procedure 或 engagement; suffix: 防重复键的末段."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.transition,
        actor=AgentId.A1,
        procedure_id=procedure_id,
        summary=f"{oid} {before} → {after}",
        idempotency_key=f"{eid}:transition:{obj}:{oid}:{suffix}",
        payload={"object": obj, "id": oid, "from": before, "to": after, "version": version},
    )


def phase_transition(eid: str, before: Phase, after: Phase, procedure_id: str | None = None) -> list[TrailEntry]:
    """项目阶段从 before 变为 after 的留痕; 没变时为空. procedure_id: 引起变化的程序 (如闸门决定的程序)."""
    if before is after:
        return []
    return [transition(eid, "engagement", eid, before.value, after.value, after.value, None, procedure_id)]


async def procedure_transitions(store: AuditOutputStore, eid: str, written: Iterable[object]) -> list[TrailEntry]:
    """written 中各程序新版本相对其上一版的状态变化留痕. 上一版读 Store, 首版与状态未变的不写.

    在业务事务提交后调用, written 为 put 返回的对象 (重放时为 Store 已存的那一版, 键与首次相同).
    """
    entries = []
    for obj in written:
        if not isinstance(obj, Procedure) or obj.version == 1:
            continue
        previous = await store.get(eid, Procedure, obj.id, obj.version - 1)
        if previous is not None and previous.status != obj.status:
            entries.append(
                transition(
                    eid,
                    "procedure",
                    obj.id,
                    previous.status.value,
                    obj.status.value,
                    f"v{obj.version}",
                    obj.version,
                    obj.id,
                )
            )
    return entries


async def record_transitions(trail: AuditTrail, entries: Iterable[TrailEntry]) -> None:
    """依次写入留痕, 同键已有的跳过."""
    for entry in entries:
        assert entry.idempotency_key is not None  # transition 一律带键
        if await trail.find_success(entry.engagement_id, entry.idempotency_key) is None:
            await trail.record(entry)
