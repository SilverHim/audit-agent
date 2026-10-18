"""状态迁移留痕 (A 负责): 项目阶段与程序状态每变一次, 在业务事务提交后写一行 transition (Demo需求文档第 4.2 节).

防重复键 (idempotency_key):
- 程序状态: {eid}:transition:procedure:{程序编号}:v{新版本号}
- 项目阶段: {eid}:transition:engagement:{eid}:{新阶段}
已有同键的留痕即跳过, 节点重放与恢复补做时不重复写. 首版与状态未变的新版本不写.
"""

from collections.abc import Iterable

from backend.contracts.data import DataReadiness
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


def data_ready_entry(eid: str, readiness: DataReadiness) -> TrailEntry:
    """确认数据已齐的 data 留痕 (需求 R10): actor 为确认人 (数据集载入为 "dataset"). payload 为 {action: ready},
    确认记了理由时另带 reason, 有未通过项时另带 failed_checks (需求 R273). 防重复键 {eid}:data:ready, 有未通过项时为
    {eid}:data:ready:checks_failed: 第一次确认时体检全过, 之后补记了理由与未通过项 (DataStore.confirm_ready) 的,
    补记的另成一条, 不因第一条已写而跳过.

    确认接口写, 恢复补做 (确认已写入, 留痕或恢复没跟上) 时也写; 同键已有的跳过 (record_transitions)."""
    failed = [check.value for check in readiness.ready_failed_checks]
    payload: dict[str, object] = {"action": "ready"}
    if readiness.ready_reason is not None:
        payload["reason"] = readiness.ready_reason
    if failed:
        payload["failed_checks"] = failed
    if readiness.ready_by == "dataset":
        summary = "数据集已载入, 视同确认数据已齐"
    elif failed:
        summary = f"体检 {len(failed)} 项不通过, 写明理由后确认数据已齐, 开始计划阶段分析"
    else:
        summary = "确认数据已齐, 开始计划阶段分析"
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=readiness.ready_by or AgentId.A1,
        summary=summary,
        idempotency_key=f"{eid}:data:ready:checks_failed" if failed else f"{eid}:data:ready",
        payload=payload,
    )


async def record_transitions(trail: AuditTrail, entries: Iterable[TrailEntry]) -> None:
    """依次写入留痕, 同键已有的跳过. transition 与确认数据已齐的 data 留痕共用."""
    for entry in entries:
        assert entry.idempotency_key is not None  # 这里写的留痕一律带键
        if await trail.find_success(entry.engagement_id, entry.idempotency_key) is None:
            await trail.record(entry)
