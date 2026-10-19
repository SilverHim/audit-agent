"""稍后处理: 审计师把暂时没法决定的复核任务, 阶段决定, 补交资料请求或 Xero 组织信息核对 (需求 R291) 标为"稍后处理",
标记只记在 deferral 留痕里.

规则见 开发接口文档.md 第 10 节. 标记不改任务, 不动编排状态; 被标记的事项照常可以决定 (或补交), 决定后 (请求转为
已收到后) 自动不再算有效标记.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Literal

from backend.contracts.data import DataRequest
from backend.contracts.models import GateDecision, GateId, ReviewTask, TaskStatus, TrailEntry

# 标记对象: 复核任务, 闸门 (阶段决定), 补交资料请求, Xero 组织信息与客户画像的核对 (target_id 为核对的 key)
Target = Literal["review_task", "gate", "data_request", "xero_profile"]


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
    requests: Mapping[str, DataRequest] | None = None,
    xero_check: str | None = None,
) -> bool:
    """某目标最近的一条留痕是否仍算有效标记.

    须为 deferred=true; 复核任务须最新版本仍待决; 闸门须是当前停留的闸门, 且标记之后这道闸门没有新的决定
    (信息不足后原地重新呈报的, 标记随那次决定失效); 补交请求须仍待补交, 且标记在这次提出之后 (收到后又重新打开的,
    之前的标记失效); Xero 核对须是当前待处理的那一次 (key 相同: 作出选择, 或 Xero 的值, 画像变了即失效).
    tasks: 任务 ID -> 最新版本; gate_decisions: 闸门决定; requests: 请求 ID -> 请求; xero_check: 待处理核对的 key.
    """
    if entry.payload.get("deferred") is not True:
        return False
    target_type, target_id = target_of(entry)
    if target_type == "review_task":
        task = tasks.get(target_id)
        return task is not None and task.status is TaskStatus.pending
    if target_type == "data_request":
        request = (requests or {}).get(target_id)
        return request is not None and request.status == "open" and deferred_at(entry) >= request.created_at
    if target_type == "xero_profile":
        return xero_check is not None and target_id == xero_check
    if current_gate is None or current_gate.value != target_id:
        return False
    marked = deferred_at(entry)
    return not any(d.gate is current_gate and d.decided_at > marked for d in gate_decisions)


def active_deferrals(
    entries: Iterable[TrailEntry],
    tasks: Mapping[str, ReviewTask],
    current_gate: GateId | None,
    gate_decisions: Sequence[GateDecision],
    requests: Mapping[str, DataRequest] | None = None,
    xero_check: str | None = None,
) -> list[TrailEntry]:
    """当前有效的稍后处理标记: 每个目标取最近一条, 按 is_active 过滤, 按标记时间升序. 纯函数."""
    latest = latest_entries(entries).values()
    active = [e for e in latest if is_active(e, tasks, current_gate, gate_decisions, requests, xero_check)]
    return sorted(active, key=deferred_at)
