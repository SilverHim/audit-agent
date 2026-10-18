"""恢复补做 (A 负责): Store 已提交而检查点没跟上的闸门决定, 客户画像更新与复核决定, 在 recover 与 resume 时补进检查点
(接口文档第 7 节 recover 规则第 2 条, 恢复机制).

决定提交时先在一个事务内写 Store, 再写留痕, 最后以闸门节点身份写检查点. 进程在两者之间中断, 或检查点写入失败,
Store 就领先检查点. 补做不重算效果, 只把 Store 中已写成的对象搬进检查点: 程序, 风险清单, 审计目标与报告取 Store
最新版, 项目阶段取 engagements 表; 只在图状态中的重要性三项按决定与重要性候选重算, 意见类型取决定. 补做不写 Store
(项目可能已锁定), 只补写缺失的留痕.
"""

from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from backend.agents.base import AgentDeps
from backend.contracts.models import (
    AgentId,
    Decision,
    Engagement,
    GateDecision,
    GateId,
    GateVerdict,
    Procedure,
    ProcedureRun,
    ReviewRecord,
    ReviewTask,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import EngagementState, StatePatch, fill_defaults
from backend.orchestration.decisions import review_trail
from backend.orchestration.gates import current_gate, current_spec, latest_in_order, spec_of
from backend.orchestration.nodes import NODE_PROCEDURES
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.orchestration.trail import phase_transition, procedure_transitions, record_transitions


async def gate_values(
    deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot, decision: GateDecision
) -> StatePatch:
    """一个闸门决定要写进检查点的状态片段, 读 Store 与 engagements 表重建 (同 DecisionSubmit 越过闸门时写的字段):
    决定, 程序包与阶段的最新版; 通过决定另由登记表的 rebuild 补专属状态 (重要性三项, 意见类型, 签字版);
    再写决定绑定的材料."""
    eid = engagement.id
    store = deps.output_store
    values = fill_defaults(snapshot.values)
    spec = spec_of(decision.gate)
    patch: StatePatch = {"gate_decisions": [decision]}
    pack = latest_in_order(values["procedure_pack"], await store.latest(eid, Procedure))
    if pack != values["procedure_pack"]:
        patch["procedure_pack"] = pack
    if engagement.phase is not values["phase"]:
        patch["phase"] = engagement.phase
    if decision.decision is GateVerdict.approve and spec.rebuild is not None:
        patch.update(await spec.rebuild(deps, eid, decision, values))
    # 决定绑定的材料: 路由按它判断决定是否过期 (RPT-01 签字时已写签字版, 路由另行放行)
    if spec.material_field not in patch:
        material = await store.get(eid, spec.material_model, decision.target_id, decision.target_version)
        if material is None:
            raise LookupError(f"{decision.id} 绑定的 {decision.target_id} v{decision.target_version} 不在 Store 中")
        patch[spec.material_field] = [material] if spec.is_list else material
    return patch


async def record_once(deps: AgentDeps, entry: TrailEntry) -> None:
    """按防重复键补写一行留痕, 已有的跳过."""
    assert entry.idempotency_key is not None  # 补写的留痕一律带键
    if await deps.trail.find_success(entry.engagement_id, entry.idempotency_key) is None:
        await deps.trail.record(entry)


async def backfill_transitions(
    deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot, procedure_id: str | None
) -> None:
    """补写决定写出的状态迁移留痕 (同键跳过): Store 中不在检查点程序包里的程序版本, 以及从检查点阶段到项目表阶段."""
    eid = engagement.id
    values = fill_defaults(snapshot.values)
    before = {(p.id, p.version) for p in values["procedure_pack"]}
    changed = [p for p in await deps.output_store.latest(eid, Procedure) if (p.id, p.version) not in before]
    entries = await procedure_transitions(deps.output_store, eid, changed)
    entries += phase_transition(eid, values["phase"], engagement.phase, procedure_id)
    await record_transitions(deps.trail, entries)


async def catch_up_gates(
    app: CompiledStateGraph[EngagementState], deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot
) -> StateSnapshot:
    """把 Store 中检查点没有的闸门决定逐个补进检查点 (按决定时间): 每个先补写缺失的留痕, 再以该闸门节点身份
    aupdate_state.
    决定须针对图当前停着的闸门, 否则抛 RuntimeError (Store 与检查点对不上, 须人工处理). 返回补做后的检查点."""
    eid = engagement.id
    config = GraphRunner.config(eid)
    known = {d.id for d in fill_defaults(snapshot.values)["gate_decisions"]}
    missing = [d for d in await deps.output_store.latest(eid, GateDecision) if d.id not in known]
    for decision in sorted(missing, key=lambda d: d.decided_at):
        spec = current_spec(snapshot.next, fill_defaults(snapshot.values))
        if spec is None or spec.gate is not decision.gate:
            at = spec.gate if spec is not None else snapshot.next
            raise RuntimeError(f"{decision.id} 是 {decision.gate} 的决定, 检查点却停在 {at}, 无法补做")
        values = await gate_values(deps, engagement, snapshot, decision)
        procedure_id = spec.procedure_id
        # 先补留痕再写检查点: 留痕写失败时决定仍不在检查点里, 下次恢复照样补做; 反过来决定已进检查点,
        # 再也发现不了缺的留痕
        await record_once(
            deps,
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.decision,
                actor=decision.decided_by,
                procedure_id=procedure_id,
                summary=f"{decision.gate.value} {decision.decision.value}",
                idempotency_key=decision.id,
                payload={"decision_id": decision.id, "reason": decision.reason},
            ),
        )
        await backfill_transitions(deps, engagement, snapshot, procedure_id)
        await app.aupdate_state(config, values, as_node=spec.node)
        snapshot = await app.aget_state(config)
    return snapshot


async def catch_up_profile(
    app: CompiledStateGraph[EngagementState], deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot
) -> StateSnapshot:
    """项目采用的画像版本高于检查点的 (更新客户资料已提交, 检查点没跟上): 先补写 decision 留痕, 再以 ACC-01 闸门身份
    写入新版本号, ACC-01 的路由随即选 rerun 回 p0_intel. 不在 ACC-01 闸门时抛 RuntimeError. 返回补做后的检查点."""
    version = engagement.client_profile_version
    if version <= fill_defaults(snapshot.values)["client_profile_version"]:
        return snapshot
    if current_gate(snapshot.next, fill_defaults(snapshot.values)) is not GateId.ACC_01:
        raise RuntimeError(f"客户画像第 {version} 版未进检查点, 检查点却停在 {snapshot.next}, 无法补做")
    # 先补留痕再写检查点 (理由同 catch_up_gates). 画像不记更新人, 补写的留痕记 A1
    await record_once(
        deps,
        TrailEntry(
            engagement_id=engagement.id,
            kind=TrailKind.decision,
            actor=AgentId.A1,
            procedure_id="ACC-01",
            summary=f"客户资料更新至第 {version} 版 (恢复时补写, 更新人未记录)",
            idempotency_key=f"{engagement.client_id}-v{version}",
            payload={"client_id": engagement.client_id, "version": version},
        ),
    )
    config = GraphRunner.config(engagement.id)
    await app.aupdate_state(config, {"client_profile_version": version}, as_node=spec_of(GateId.ACC_01).node)
    return await app.aget_state(config)


def closed_by(
    record: ReviewRecord, task: ReviewTask, tasks: list[ReviewTask], records: list[ReviewRecord]
) -> list[str]:
    """rerun 决定随同关闭的同组任务 ID (决定提交时写进留痕, 补写时从 Store 推回): 与被决定任务同程序, 同一次执行
    的 failed 任务中, 已 decided 而没有自己的复核决定的. tasks 与 records 为 Store 中各自的最新版本."""
    if record.decision is not Decision.rerun:
        return []
    targeted = {r.task_id for r in records}
    return sorted(
        t.id
        for t in tasks
        if t.kind is TaskKind.failed
        and t.procedure_id == task.procedure_id
        and t.run_id == task.run_id
        and t.id != task.id
        and t.status is TaskStatus.decided
        and t.id not in targeted
    )


async def catch_up_review(
    app: CompiledStateGraph[EngagementState], deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot
) -> Command[Any] | None:
    """Store 中有检查点里没有的复核决定时, 补写它们的留痕, 按节点读回的顺序交给节点: 前面的各以
    ainvoke(Command(resume=决定 ID)) 交出 (每交一个停在下一次等待), 返回交最后一个的 Command; 没有返回 None.

    节点内等待载荷 records 列出的本节点已读回的决定不算缺. 读回顺序不靠时间推断, 按 (程序在该节点内的执行次序
    (nodes.NODE_PROCEDURES), 0, 任务 ID) 排 rerun 以外的决定 (节点每次等待前按 Store 判断还要不要等, 读回后从 Store
    整体并入, 交回顺序不影响等待次数与结果, 见 nodes.still_waiting), 按
    (次序, 1, 所针对任务所属执行的 run_no) 排 rerun 决定 (Q17 一轮失败算一组, 整组重跑: 同一程序每轮只对应一个 rerun
    决定, 下一轮由它触发). 同一程序同一轮有两个 rerun 决定说明不变式被破坏, 抛 RuntimeError.

    等待中的节点带着错误 (读回某个决定后出错, 如重跑的执行提交后写检查点前中断) 时, 检查点里留着那次运行的错误与
    恢复值, 节点之后再停下等待时错误仍在, DecisionSubmit 会把它当成卡住的等待, 拒绝后续决定. 此时先复制检查点
    (as_node="__copy__", 丢掉这些待写记录), 节点从头执行, 本节点已读回的决定也一并按上述顺序交回. 顺序每次从 Store
    重算, 复制后或只交回一部分时中断, 下次补做看到的是节点待执行或停在中间某次等待, 照样接着交.

    P2 的等复核 (await_events) 每次读回一个决定后即从 Store 整体读回, 与交哪个, 按什么顺序无关: 只交一个, 不复制.
    图停在闸门, 不在等待也没有待执行的节点, 或节点不在 NODE_PROCEDURES 中时抛 RuntimeError.
    """
    eid = engagement.id
    store = deps.output_store
    in_state = {r.id for r in fill_defaults(snapshot.values)["review_records"]}
    waits = waiting_payloads(snapshot)
    read = {rid for w in waits if isinstance(w, dict) for rid in w.get("records", [])}  # 本节点已读回的
    all_records = await store.latest(eid, ReviewRecord)
    records = [r for r in all_records if r.id not in in_state]
    missing = [r for r in records if r.id not in read]
    if not missing:
        return None
    waiting = [t.name for t in snapshot.tasks if t.interrupts]
    node = waiting[0] if waiting else (snapshot.next[0] if snapshot.next else None)
    if current_gate(snapshot.next, fill_defaults(snapshot.values)) is not None or node is None:
        ids = ", ".join(sorted(r.id for r in missing))
        raise RuntimeError(f"复核决定 {ids} 未进检查点, 图却不在等复核 ({snapshot.next}), 无法补做")
    tasks = {t.id: t for t in await store.latest(eid, ReviewTask)}
    for record in missing:
        task = tasks[record.task_id]
        closed = closed_by(record, task, list(tasks.values()), all_records)
        await record_once(deps, review_trail(eid, record, task, closed))
    await backfill_transitions(deps, engagement, snapshot, None)  # 复核决定不改阶段
    if node == "await_events":
        return Command(resume=min(r.id for r in missing))
    if node not in NODE_PROCEDURES:
        raise RuntimeError(f"复核决定未进检查点, 节点 {node} 不是等复核的节点, 无法补做")
    order = NODE_PROCEDURES[node]

    async def key(record: ReviewRecord) -> tuple[int, int, int, str]:
        task = tasks[record.task_id]
        run = await store.get(eid, ProcedureRun, task.run_id)
        if task.procedure_id not in order or run is None:
            raise RuntimeError(f"复核决定 {record.id} 针对的任务 {task.id} 不属于节点 {node} 的执行, 无法补做")
        if record.decision is Decision.rerun:
            return order.index(task.procedure_id), 1, run.run_no, ""
        return order.index(task.procedure_id), 0, 0, task.id

    keys = {r.id: await key(r) for r in records}
    reruns = [k for r in records if r.decision is Decision.rerun for k in [keys[r.id]]]
    if len(set(reruns)) < len(reruns):
        raise RuntimeError(f"节点 {node} 有多个 rerun 决定针对同一程序的同一轮执行, 无法确定读回顺序: {keys}")
    config = GraphRunner.config(eid)
    sequence = sorted(missing, key=lambda r: keys[r.id])
    if any(t.interrupts and t.error for t in snapshot.tasks):
        await app.aupdate_state(config, None, as_node="__copy__")
        sequence = sorted(records, key=lambda r: keys[r.id])
    for record in sequence[:-1]:
        await app.ainvoke(Command(resume=record.id), config, durability="sync")
    return Command(resume=sequence[-1].id)


async def catch_up(
    app: CompiledStateGraph[EngagementState], deps: AgentDeps, engagement: Engagement
) -> Command[Any] | None:
    """补做 Store 已提交而检查点没跟上的内容, 依次: 闸门决定, 客户画像版本, 复核决定. 前两者直接写进检查点;
    复核决定由停着等待的节点读回, 返回 Command(resume=决定 ID) 作继续执行的输入, 其余返回 None (从检查点继续).
    补做不写 Store, 只补写缺失的留痕; 再补做一次不改检查点与 Store."""
    snapshot = await app.aget_state(GraphRunner.config(engagement.id))
    snapshot = await catch_up_gates(app, deps, engagement, snapshot)
    snapshot = await catch_up_profile(app, deps, engagement, snapshot)
    return await catch_up_review(app, deps, engagement, snapshot)
