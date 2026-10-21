"""状态迁移留痕 (A 负责): 项目阶段与程序状态每变一次, 在业务事务提交后写一行 transition (Demo需求文档第 4.2 节);
程序每次执行另写开始与结束两行 run 留痕 (需求 R325).

防重复键 (idempotency_key):
- 程序状态: {eid}:transition:procedure:{程序编号}:v{新版本号}
- 项目阶段: {eid}:transition:engagement:{eid}:{新阶段}; 过期重跑使阶段回到执行阶段后 (第 n 次重跑请求起) 再经过
  各阶段时为 {eid}:transition:engagement:{eid}:{新阶段}:{n}, n 为当时状态中的重跑请求数 (需求 R274)
- 程序执行: {eid}:run:{run_id}:started 与 {eid}:run:{run_id}:finished
已有同键的留痕即跳过, 节点重放与恢复补做时不重复写. 首版与状态未变的新版本不写 transition.
"""

import logging
from collections.abc import Iterable
from typing import Any, Literal

from jinja2 import UndefinedError

from backend.contracts.data import DataReadiness
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import AgentId, Phase, Procedure, ProcedureRun, RunStatus, TrailEntry, TrailKind
from backend.contracts.ports import AuditOutputStore, AuditTrail
from backend.services.procedure_library import render_text

logger = logging.getLogger(__name__)


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


def phase_transition(
    eid: str, before: Phase, after: Phase, procedure_id: str | None = None, reruns: int = 0
) -> list[TrailEntry]:
    """项目阶段从 before 变为 after 的留痕; 没变时为空. procedure_id: 引起变化的程序 (如闸门决定的程序).
    reruns: 当时状态中的过期重跑请求数 (重跑请求写出的回退计入它自己), 不为 0 时并入防重复键: 重跑后再次经过
    同一阶段也留痕, 重放与补做时同键跳过."""
    if before is after:
        return []
    suffix = after.value if reruns == 0 else f"{after.value}:{reruns}"
    return [transition(eid, "engagement", eid, before.value, after.value, suffix, None, procedure_id)]


async def procedure_transitions(store: AuditOutputStore, eid: str, written: Iterable[object]) -> list[TrailEntry]:
    """written 中各程序新版本相对其上一版的状态变化留痕. 上一版读 Store, 首版与状态未变的不写. 新版本带
    data_request_ids (等待数据, 未执行 (无数据), 需求 R272) 时 payload 另带它.

    在业务事务提交后调用, written 为 put 返回的对象 (重放时为 Store 已存的那一版, 键与首次相同).
    """
    entries = []
    for obj in written:
        if not isinstance(obj, Procedure) or obj.version == 1:
            continue
        previous = await store.get(eid, Procedure, obj.id, obj.version - 1)
        if previous is not None and previous.status != obj.status:
            before, after = previous.status.value, obj.status.value
            entry = transition(eid, "procedure", obj.id, before, after, f"v{obj.version}", obj.version, obj.id)
            if obj.data_request_ids:
                payload = {**entry.payload, "data_request_ids": obj.data_request_ids}
                entry = entry.model_copy(update={"payload": payload})
            entries.append(entry)
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


def run_entry(
    eid: str, template: ProcedureTemplate, run: ProcedureRun, action: Literal["started", "finished"], summary: str
) -> TrailEntry:
    """一行 run 留痕 (需求 R325), 写入者 A1. payload: action, procedure_id, run_id, run_no, trigger, phase (程序所属
    阶段), scope (见 run_scope)."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.run,
        actor=AgentId.A1,
        procedure_id=run.procedure_id,
        run_id=run.id,
        summary=summary,
        idempotency_key=f"{eid}:run:{run.id}:{action}",
        payload={
            "action": action,
            "procedure_id": run.procedure_id,
            "run_id": run.id,
            "run_no": run.run_no,
            "trigger": run.trigger.value,
            "phase": template.phase.value,
            "scope": run_scope(template, run),
        },
    )


def run_scope(template: ProcedureTemplate, run: ProcedureRun) -> dict[str, str] | None:
    """程序的范围说明按本次的参数快照渲染, {zh, en}; 模板没写为空. 快照里缺占位用的参数 (程序库之后给 scope 加了参数,
    重放升级前的执行) 时也为空, 记一行 warning (执行 ID 与缺的参数), 不让节点报错 (留痕在终态提交之后写)."""
    if template.scope is None:
        return None
    try:
        return render_text(template.scope, run.params_snapshot).model_dump()
    except UndefinedError as e:
        logger.warning("执行 %s 的范围说明渲染不出, scope 记为空: %s", run.id, e)
        return None


def run_started(eid: str, template: ProcedureTemplate, run: ProcedureRun) -> TrailEntry:
    """开始执行的留痕: 建好执行记录之后, 调 Agent 之前写 (运行中的执行记录不写 Store, 这一行就是开始的记录)."""
    return run_entry(eid, template, run, "started", f"开始执行 {run.procedure_id} {template.name}")


def run_finished(eid: str, template: ProcedureTemplate, run: ProcedureRun, result: dict[str, Any] | None) -> TrailEntry:
    """执行结束的留痕: 执行记录终态提交之后写. payload 另带 status (执行状态 succeeded 或 failed, 同 ProcedureRun),
    failure_reason (同执行记录) 与 result (结果摘要, services.run_summary.run_result; 失败时为空)."""
    if run.status is RunStatus.failed:
        summary = f"{run.procedure_id} {template.name} 执行失败: {run.failure_reason}"
    else:
        summary = f"{run.procedure_id} {template.name} 完成"
    entry = run_entry(eid, template, run, "finished", summary)
    payload = {**entry.payload, "status": run.status.value, "failure_reason": run.failure_reason, "result": result}
    return entry.model_copy(update={"payload": payload})


async def record_transitions(trail: AuditTrail, entries: Iterable[TrailEntry]) -> None:
    """依次写入留痕, 同键已有的跳过. transition, run 与确认数据已齐的 data 留痕共用."""
    for entry in entries:
        assert entry.idempotency_key is not None  # 这里写的留痕一律带键
        if await trail.find_success(entry.engagement_id, entry.idempotency_key) is None:
            await trail.record(entry)
