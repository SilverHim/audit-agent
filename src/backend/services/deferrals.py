"""稍后处理: 审计师把暂时没法决定的复核任务或阶段决定标为"稍后处理", 标记只记在 deferral 留痕里.

规则见 开发接口文档.md 第 10 节. 标记不改任务, 不动编排状态; 被标记的事项照常可以决定, 决定后自动不再算有效标记.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Literal

from backend.contracts.models import GateDecision, GateId, ReviewTask, TaskStatus, TrailEntry

Target = Literal["review_task", "gate"]  # 标记对象: 复核任务或闸门 (阶段决定)


def target_of(entry: TrailEntry) -> tuple[str, str]:
    """一条 deferral 留痕的目标 (target_type, target_id), 取自 payload."""
    return str(entry.payload["target_type"]), str(entry.payload["target_id"])


def latest_entries(entries: Iterable[TrailEntry]) -> dict[tuple[str, str], TrailEntry]:
    """每个目标最近的一条 deferral 留痕. entries 按写入顺序, 后写的为准."""
    latest: dict[tuple[str, str], TrailEntry] = {}
    for entry in entries:
        latest[target_of(entry)] = entry
    return latest


def deferred_at(entry: TrailEntry) -> datetime:
    """标记时间, 即留痕的写入时间 (写入时由调用方填好)."""
    assert entry.created_at is not None  # 读回的留痕都有写入时间
    return entry.created_at


def is_active(
    entry: TrailEntry,
    tasks: Mapping[str, ReviewTask],
    current_gate: GateId | None,
    gate_decisions: Sequence[GateDecision],
) -> bool:
    """某目标最近的一条留痕是否仍算有效标记.

    须为 deferred=true; 复核任务须最新版本仍待决; 闸门须是当前停留的闸门, 且标记之后这道闸门没有新的决定
    (信息不足后原地重新呈报的, 标记随那次决定失效). tasks: 任务 ID -> 最新版本; gate_decisions: 闸门决定.
    """
    if entry.payload.get("deferred") is not True:
        return False
    target_type, target_id = target_of(entry)
    if target_type == "review_task":
        task = tasks.get(target_id)
        return task is not None and task.status is TaskStatus.pending
    if current_gate is None or current_gate.value != target_id:
        return False
    marked = deferred_at(entry)
    return not any(d.gate is current_gate and d.decided_at > marked for d in gate_decisions)


def active_deferrals(
    entries: Iterable[TrailEntry],
    tasks: Mapping[str, ReviewTask],
    current_gate: GateId | None,
    gate_decisions: Sequence[GateDecision],
) -> list[TrailEntry]:
    """当前有效的稍后处理标记: 每个目标取最近一条, 按 is_active 过滤, 按标记时间升序. 纯函数."""
    latest = latest_entries(entries).values()
    active = [e for e in latest if is_active(e, tasks, current_gate, gate_decisions)]
    return sorted(active, key=deferred_at)
