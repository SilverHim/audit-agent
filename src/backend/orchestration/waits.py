"""节点内等待的载荷 (interrupt 的值, 接口文档第 7 节恢复机制): 种类登记, 载荷构造, 以及等待能否接受对某个任务的决定.

载荷存在检查点里, 字符串值与已有检查点一致: P2 的等复核 {awaiting: review, phase}, P2 以外节点等人
{awaiting: human, procedure_id, run_id, records}, P1 开始前等数据就绪 {awaiting: data}, 执行阶段程序等数据
{awaiting: inputs, procedure_ids}. 种类只在这里登记: runner 按 kind_of 区分等数据与等复核, DecisionSubmit 按 accepts
判断决定能否送进当前的等待.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from backend.contracts.models import Phase, Procedure, ReviewTask


class WaitKind(StrEnum):
    """节点内等待的种类 (载荷的 awaiting)."""

    review = "review"  # P2 的等复核 (await_events): 收 P2 程序的复核决定
    human = "human"  # P2 以外的节点等人: 只收本程序任务的决定 (失败后 rerun, 或对其判断与例外的复核)
    correction = "correction"  # 等待更正 (错报段 MD-8 用); 本条只登记, 不收复核决定
    data = "data"  # P1 开始前等财务数据就绪 (p1_await_data): 不收复核决定, 确认数据已齐后以 DATA_READY 恢复
    # 执行阶段程序等数据 (await_data, 需求 R272): 不收复核决定; 数据写操作结束时以 INPUTS_CHANGED 恢复, 节点重查
    inputs = "inputs"


DATA_WAITS = frozenset({WaitKind.data, WaitKind.inputs})  # 等数据的种类 (不是等人)


def kind_of(payload: object) -> WaitKind | None:
    """载荷的种类; 不是 dict 或 awaiting 认不出时为 None."""
    awaiting = payload.get("awaiting") if isinstance(payload, dict) else None
    return next((kind for kind in WaitKind if kind == awaiting), None)


def review_payload(phase: Phase = Phase.P2_Executing) -> dict[str, Any]:
    """等复核的载荷: phase 为所收任务的程序所在阶段."""
    return {"awaiting": WaitKind.review.value, "phase": phase.value}


def human_payload(procedure_id: str, run_id: str, records: Sequence[str]) -> dict[str, Any]:
    """P2 以外节点等人的载荷: 本程序, 本轮执行, 本节点已读回的决定 (在节点返回前不进状态)."""
    return {"awaiting": WaitKind.human.value, "procedure_id": procedure_id, "run_id": run_id, "records": list(records)}


def data_payload() -> dict[str, Any]:
    """等财务数据就绪的载荷 (p1_await_data)."""
    return {"awaiting": WaitKind.data.value}


def inputs_payload(procedure_ids: Sequence[str]) -> dict[str, Any]:
    """执行阶段程序等数据的载荷 (await_data): 等待数据的程序."""
    return {"awaiting": WaitKind.inputs.value, "procedure_ids": list(procedure_ids)}


def accepts(payload: object, task: ReviewTask, pack: Sequence[Procedure]) -> bool:
    """该等待能否接受对 task 的决定.

    review: 任务的程序所在阶段须为载荷的 phase (旧检查点不带 phase, 按 P2); human: 任务须属于载荷的程序;
    correction, data, inputs 与认不出的载荷一律不收. pack 为检查点中的程序包, 用来查任务所属程序的阶段.
    """
    if not isinstance(payload, dict):
        return False
    kind = kind_of(payload)
    if kind is WaitKind.review:
        program = next((p for p in pack if p.id == task.procedure_id), None)
        return program is not None and program.phase.value == payload.get("phase", Phase.P2_Executing.value)
    if kind is WaitKind.human:
        return bool(payload.get("procedure_id") == task.procedure_id)
    return False
