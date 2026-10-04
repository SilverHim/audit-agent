"""恢复补做 (A 负责): Store 已提交而检查点没跟上的闸门决定, 在 recover 与 resume 时补进检查点 (接口文档第 7 节
recover 规则第 2 条, 恢复机制).

决定提交时先在一个事务内写 Store, 再写留痕, 最后以闸门节点身份写检查点. 进程在两者之间中断, 或检查点写入失败,
Store 就领先检查点. 补做不重算效果, 只把 Store 中已写成的对象搬进检查点: 程序, 风险清单, 审计目标与报告取 Store
最新版, 项目阶段取 engagements 表; 只在图状态中的重要性三项按决定与重要性候选重算, 意见类型取决定. 补做不写 Store
(项目可能已锁定), 只补写缺失的留痕.
"""

from typing import TypeVar

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

from backend.agents.base import AgentDeps
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditObjective,
    AuditReportVersion,
    Engagement,
    GateDecision,
    GateId,
    GateVerdict,
    MaterialityCandidates,
    Procedure,
    RiskItem,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
    Versioned,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.decisions import GATE_MATERIAL, GATE_PROCEDURE, materiality_amounts
from backend.orchestration.graph import GATE_NODES
from backend.orchestration.query import current_gate
from backend.orchestration.runner import GraphRunner
from backend.orchestration.trail import phase_transition, procedure_transitions, record_transitions

MATERIAL_MODELS: dict[GateId, type[Versioned]] = {  # 闸门呈报材料的类型, 按决定绑定的 (id, version) 从 Store 取
    GateId.ACC_01: AcceptanceScorecard,
    GateId.PLN_01_03: MaterialityCandidates,
    GateId.FIN_01: UncorrectedSchedule,
    GateId.RPT_01: AuditReportVersion,
}


V = TypeVar("V", bound=Versioned)


def latest_in_order(current: list[V], latest: list[V]) -> list[V]:
    """按 current 的顺序换成 Store 最新版; Store 有而 current 没有的接在后面."""
    by_id = {o.id: o for o in latest}
    known = {o.id for o in current}
    return [by_id.get(o.id, o) for o in current] + [o for o in latest if o.id not in known]


async def gate_values(
    deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot, decision: GateDecision
) -> StatePatch:
    """一个闸门决定要写进检查点的状态片段, 读 Store 与 engagements 表重建 (同 DecisionSubmit 越过闸门时写的字段)."""
    eid = engagement.id
    store = deps.output_store
    values = snapshot.values
    patch: StatePatch = {"gate_decisions": [decision]}
    pack = latest_in_order(values["procedure_pack"], await store.latest(eid, Procedure))
    if pack != values["procedure_pack"]:
        patch["procedure_pack"] = pack
    if engagement.phase is not values["phase"]:
        patch["phase"] = engagement.phase
    if decision.decision is GateVerdict.approve:
        if decision.gate is GateId.PLN_01_03:
            candidates = await store.get(eid, MaterialityCandidates, decision.target_id, decision.target_version)
            assert candidates is not None and decision.materiality is not None  # 提交时已校验
            overall, performance, trivial = materiality_amounts(candidates, decision.materiality)
            patch["materiality_cents"] = overall
            patch["performance_materiality_cents"] = performance
            patch["clearly_trivial_cents"] = trivial
            patch["risk_register"] = latest_in_order(values["risk_register"], await store.latest(eid, RiskItem))
            patch["audit_objectives"] = latest_in_order(
                values["audit_objectives"], await store.latest(eid, AuditObjective)
            )
        elif decision.gate is GateId.FIN_01:
            patch["opinion_route"] = decision.disposition
        elif decision.gate is GateId.RPT_01:
            signed = await store.get(eid, AuditReportVersion, decision.target_id)  # 签字版, 比决定绑定的高一版
            assert signed is not None
            patch["report_versions"] = [signed]
    # 决定绑定的材料: 路由按它判断决定是否过期 (RPT-01 签字时已写签字版, 路由另行放行)
    field = GATE_MATERIAL[decision.gate]
    if field not in patch:
        material = await store.get(eid, MATERIAL_MODELS[decision.gate], decision.target_id, decision.target_version)
        if material is None:
            raise LookupError(f"{decision.id} 绑定的 {decision.target_id} v{decision.target_version} 不在 Store 中")
        patch[field] = [material] if field == "report_versions" else material
    return patch


async def backfill_trail(
    deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot, decision: GateDecision
) -> None:
    """补写决定提交后应写的留痕, 已有的跳过: decision 留痕 (键为决定 ID), 程序状态与阶段的 transition 留痕."""
    eid = engagement.id
    procedure_id = GATE_PROCEDURE[decision.gate]
    if await deps.trail.find_success(eid, decision.id) is None:
        await deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.decision,
                actor=decision.decided_by,
                procedure_id=procedure_id,
                summary=f"{decision.gate.value} {decision.decision.value}",
                idempotency_key=decision.id,
                payload={"decision_id": decision.id, "reason": decision.reason},
            )
        )
    before = {(p.id, p.version) for p in snapshot.values["procedure_pack"]}
    changed = [p for p in await deps.output_store.latest(eid, Procedure) if (p.id, p.version) not in before]
    entries = await procedure_transitions(deps.output_store, eid, changed)
    entries += phase_transition(eid, snapshot.values["phase"], engagement.phase, procedure_id)
    await record_transitions(deps.trail, entries)


async def catch_up(app: CompiledStateGraph[EngagementState], deps: AgentDeps, engagement: Engagement) -> None:
    """把 Store 中检查点没有的闸门决定逐个补进检查点 (按决定时间): 每个先补写缺失的留痕, 再以该闸门节点身份
    aupdate_state, 之后由调用方从检查点继续执行. 决定须针对图当前停着的闸门, 否则抛 RuntimeError
    (Store 与检查点对不上, 须人工处理).
    """
    eid = engagement.id
    config = GraphRunner.config(eid)
    snapshot = await app.aget_state(config)
    known = {d.id for d in snapshot.values.get("gate_decisions", [])}
    missing = [d for d in await deps.output_store.latest(eid, GateDecision) if d.id not in known]
    for decision in sorted(missing, key=lambda d: d.decided_at):
        gate = current_gate(snapshot.next)
        if gate is not decision.gate:
            raise RuntimeError(
                f"{decision.id} 是 {decision.gate} 的决定, 检查点却停在 {gate or snapshot.next}, 无法补做"
            )
        values = await gate_values(deps, engagement, snapshot, decision)
        # 先补留痕再写检查点: 留痕写失败时决定仍不在检查点里, 下次恢复照样补做; 反过来决定已进检查点,
        # 再也发现不了缺的留痕
        await backfill_trail(deps, engagement, snapshot, decision)
        await app.aupdate_state(config, values, as_node=GATE_NODES[gate])
        snapshot = await app.aget_state(config)
