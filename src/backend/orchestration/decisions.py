"""DecisionSubmit (A 负责): 审计师决定的唯一写入口. 取锁 → 校验 → 一个事务内写入决定及其效果 → 越过闸门 → 后台继续.

校验顺序, 必填表, 决定的效果与恢复机制见 开发接口文档.md 第 7 节. 已实现 ACC-01 闸门决定; PLN-01/03 已校验,
效果待实现.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

from backend.agents.base import AgentDeps
from backend.contracts.errors import DecisionRejected, VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    ClientProfile,
    Engagement,
    GateDecision,
    GateEdit,
    GateId,
    GateVerdict,
    MaterialityCandidates,
    MaterialityChoice,
    Phase,
    Procedure,
    ProcedureStatus,
    ReviewRecord,
    RiskItem,
    TrailEntry,
    TrailKind,
    Versioned,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.nodes import save_outputs, update_procedure
from backend.orchestration.query import current_gate
from backend.orchestration.runner import GraphRunner

GATE_SPECIFIC = ("materiality", "edits", "disposition", "annotations")  # 只在某道闸门的某种决定下可填的字段


def acc01_effects(pack: list[Procedure], decision: GateDecision) -> StatePatch:
    """ACC-01 闸门决定的效果, 由 aupdate_state(as_node="gate_acc01") 写入状态.

    pack 为当前程序包. 项目 locked (reject 时) 写 engagements 表, 不在状态里, 由 submit 处理.
    """
    match decision.decision:
        case GateVerdict.insufficient_info:
            return {"gate_decisions": [decision]}  # 留在闸门重新呈报, 程序包不动
        case GateVerdict.approve:
            phase = Phase.P1_Planning
        case GateVerdict.reject:
            phase = Phase.Declined
    return {
        "gate_decisions": [decision],
        "procedure_pack": update_procedure(pack, "ACC-01", status=ProcedureStatus.completed),
        "phase": phase,
    }


def required_fields(decision: GateDecision) -> list[str]:
    """闸门决定的必填字段, 即第 7 节必填表: 通过以外都须填 reason, FIN-01 一律须填; PLN-01/03 通过须填 materiality;
    FIN-01 通过须填 disposition; RPT-01 驳回须填 annotations."""
    approve = decision.decision is GateVerdict.approve
    required = ["reason"] if not approve or decision.gate is GateId.FIN_01 else []
    if approve and decision.gate is GateId.PLN_01_03:
        required.append("materiality")
    if approve and decision.gate is GateId.FIN_01:
        required.append("disposition")
    if decision.decision is GateVerdict.reject and decision.gate is GateId.RPT_01:
        required.append("annotations")
    return required


def allowed_fields(decision: GateDecision) -> set[str]:
    """闸门决定可填的闸门专用字段 (GATE_SPECIFIC 中的), 即必填表 "只允许在此填" 一列; 其余专用字段须为空."""
    approve = decision.decision is GateVerdict.approve
    if approve and decision.gate is GateId.PLN_01_03:
        return {"materiality", "edits"}
    if approve and decision.gate is GateId.FIN_01:
        return {"disposition"}
    if decision.decision is GateVerdict.reject and decision.gate is GateId.RPT_01:
        return {"annotations"}
    return set()


def check_fields(decision: GateDecision) -> None:
    """闸门决定的字段校验: 必填的缺了报 MISSING_FIELD, 不该填的填了报 FIELD_NOT_ALLOWED. None 与空列表都视为未填."""
    missing = [name for name in required_fields(decision) if not getattr(decision, name)]
    if missing:
        raise DecisionRejected("MISSING_FIELD", ", ".join(missing))
    allowed = allowed_fields(decision)
    filled = [name for name in GATE_SPECIFIC if getattr(decision, name) and name not in allowed]
    if filled:
        raise DecisionRejected("FIELD_NOT_ALLOWED", ", ".join(filled))


def to_cents(amount: Decimal) -> int:
    """金额四舍五入到整数分 (0.5 进位)."""
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def materiality_amounts(candidates: MaterialityCandidates, choice: MaterialityChoice) -> tuple[int, int, int]:
    """按审计师的选择算出 (整体重要性, 执行重要性, 明显微小门槛), 单位分.

    整体重要性 = 所选基准金额 × pct; 执行重要性 = 整体重要性 × performance_pct; 明显微小 = 整体重要性 ×
    clearly_trivial_pct. 比例按十进制计算 (Decimal(str(x)), 避免二进制浮点误差), 每项各自四舍五入到整数分.
    例: 收入基准 1200000000, {0.01, 0.75, 0.05} → (12000000, 9000000, 600000). basis 须在候选中 (check_materiality).
    """
    candidate = next(c for c in candidates.candidates if c.basis == choice.basis)
    overall = to_cents(Decimal(candidate.base_amount_cents) * Decimal(str(choice.pct)))
    performance = to_cents(Decimal(overall) * Decimal(str(choice.performance_pct)))
    trivial = to_cents(Decimal(overall) * Decimal(str(choice.clearly_trivial_pct)))
    return overall, performance, trivial


def check_materiality(candidates: MaterialityCandidates, choice: MaterialityChoice) -> None:
    """重要性选择的取值校验, 不过报 INVALID_VALUE: basis 在候选中; pct 在该候选 [pct_low, pct_high] 内;
    performance_pct 在 performance_pct_range 内 (闭区间); clearly_trivial_pct 大于 0 且不超过候选给的比例 (Q8)."""
    candidate = next((c for c in candidates.candidates if c.basis == choice.basis), None)
    if candidate is None:
        raise DecisionRejected("INVALID_VALUE", f"materiality.basis {choice.basis} 不在候选中")
    if not candidate.pct_low <= choice.pct <= candidate.pct_high:
        raise DecisionRejected("INVALID_VALUE", f"materiality.pct 应在 [{candidate.pct_low}, {candidate.pct_high}] 内")
    low, high = candidates.performance_pct_range
    if not low <= choice.performance_pct <= high:
        raise DecisionRejected("INVALID_VALUE", f"materiality.performance_pct 应在 [{low}, {high}] 内")
    if not 0 < choice.clearly_trivial_pct <= candidates.clearly_trivial_pct:
        raise DecisionRejected(
            "INVALID_VALUE", f"materiality.clearly_trivial_pct 应在 (0, {candidates.clearly_trivial_pct}] 内"
        )


def check_edits(edits: list[GateEdit], risks: dict[str, RiskItem]) -> None:
    """风险等级修改的取值校验, 不过报 INVALID_VALUE: target_id 在风险清单中, 同一风险项只改一次."""
    unknown = [e.target_id for e in edits if e.target_id not in risks]
    if unknown:
        raise DecisionRejected("INVALID_VALUE", f"edits 的 target_id 不在风险清单中: {', '.join(unknown)}")
    ids = [e.target_id for e in edits]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise DecisionRejected("INVALID_VALUE", f"同一风险项改了多次: {', '.join(repeated)}")


class GraphDecisionSubmit:
    """DecisionSubmit 的实现: 审计师所有决定的唯一写入口.

    写入顺序: 先在一个事务内写 Store (决定, 程序新版本, 项目阶段), 提交后写 decision 留痕 (独立事务),
    再以闸门节点身份写入检查点, 最后交给 runner 在后台继续执行. 项目锁从校验前一直持有到后台执行停下.
    """

    def __init__(self, app: CompiledStateGraph[EngagementState], deps: AgentDeps, runner: GraphRunner) -> None:
        self.app = app  # 编译好的主图, 用于读检查点与 aupdate_state
        self.deps = deps  # 外部依赖
        self.runner = runner  # 项目锁与后台执行

    async def _engagement(self, eid: str) -> Engagement:
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        return engagement

    async def _check_resume_required(self, engagement: Engagement, values: dict[str, Any]) -> None:
        """Store 已写入, 检查点却没跟上 (上次写完 Store 后中断), 须先 resume 补做, 不接受新的决定.

        两种情况: Store 中有检查点里没有的决定; 项目采用的画像版本 (Engagement.client_profile_version) 高于检查点的.
        比的是本项目采用的版本, 不是全局最新画像: 同客户的其他项目改画像不应让本项目须 resume.
        """
        eid = engagement.id
        stored = {d.id for d in await self.deps.output_store.latest(eid, GateDecision)}
        stored |= {r.id for r in await self.deps.output_store.latest(eid, ReviewRecord)}
        in_state = {d.id for d in values.get("gate_decisions", [])} | {r.id for r in values.get("review_records", [])}
        missing = stored - in_state
        if missing:
            raise DecisionRejected("RESUME_REQUIRED", ", ".join(sorted(missing)))
        version_in_state = values.get("client_profile_version")  # 未启动的项目没有
        if version_in_state is not None and engagement.client_profile_version > version_in_state:
            raise DecisionRejected(
                "RESUME_REQUIRED", f"本项目采用的客户画像第 {engagement.client_profile_version} 版未写入检查点"
            )

    @asynccontextmanager
    async def _deciding(self, eid: str) -> AsyncIterator[tuple[Engagement, StateSnapshot]]:
        """submit 与 update_client_profile 共用的外壳, 用法: async with self._deciding(eid) as (engagement, snapshot).

        进入时: 查项目锁定, 取项目锁, 重读项目再查一次锁定, 读检查点, 查 RESUME_REQUIRED.
        块内出错: 释放项目锁后抛出 (写入时的 VersionConflict 转为 DecisionRejected("VERSION_MISMATCH")).
        块正常结束: 交给 runner 从检查点在后台继续执行, 锁由后台任务结束时释放.
        """
        engagement = await self._engagement(eid)
        if engagement.locked:  # 第 7 节校验顺序: 锁定先于项目锁 (ENGAGEMENT_BUSY)
            raise DecisionRejected("ENGAGEMENT_LOCKED", eid)
        self.runner.acquire(eid)  # 取不到抛 ENGAGEMENT_BUSY
        try:
            # 取锁前读到的项目可能已过时: 读完到取锁之间, 另一个决定可能刚拒绝承接并锁定了项目
            engagement = await self._engagement(eid)
            if engagement.locked:
                raise DecisionRejected("ENGAGEMENT_LOCKED", eid)
            snapshot = await self.app.aget_state(GraphRunner.config(eid))
            await self._check_resume_required(engagement, snapshot.values)
            yield engagement, snapshot
        except VersionConflict as e:
            self.runner.release(eid)
            raise DecisionRejected("VERSION_MISMATCH", str(e)) from e
        except BaseException:
            self.runner.release(eid)  # 后台任务尚未启动, 须在此释放
            raise
        self.runner.launch(eid, None)

    async def gate_target(self, eid: str, gate: GateId) -> Versioned | None:
        """闸门呈报材料在 Store 中的当前版本, 决定须绑定它 (与 GraphStateQuery.gate 的 target 一致); 没有返回 None."""
        store = self.deps.output_store
        if gate is GateId.ACC_01:
            return await store.get(eid, AcceptanceScorecard, "SCORECARD")
        if gate is GateId.PLN_01_03:
            return await store.get(eid, MaterialityCandidates, "MATERIALITY")
        raise NotImplementedError(f"{gate} 的闸门决定尚未实现")

    async def check_gate_decision(self, eid: str, decision: GateDecision) -> Versioned:
        """第 7 节校验顺序中 NOT_AT_GATE 之后的部分: 材料与修改项的版本, 字段, 取值, 决定 ID 未用过.

        返回决定绑定的材料 (Store 中的当前版本). 不过抛 DecisionRejected.
        """
        store = self.deps.output_store
        # 决定须绑定材料的当前版本 (读 Store): 审计师看材料期间材料可能已重跑出新版
        target = await self.gate_target(eid, decision.gate)
        if target is None or (decision.target_id, decision.target_version) != (target.id, target.version):
            raise DecisionRejected("VERSION_MISMATCH", f"{decision.gate} 的呈报材料已更新, 请刷新后重新决定")
        risks = {r.id: r for r in await store.latest(eid, RiskItem)} if decision.edits else {}
        for edit in decision.edits:  # 不存在的风险项留给下面的 INVALID_VALUE
            risk = risks.get(edit.target_id)
            if risk is not None and risk.version != edit.target_version:
                raise DecisionRejected("VERSION_MISMATCH", f"风险项 {risk.id} 已是第 {risk.version} 版")
        check_fields(decision)
        # materiality 只在 PLN-01/03 通过时可填 (上一步已查), 此时材料即重要性候选
        if decision.materiality is not None and isinstance(target, MaterialityCandidates):
            check_materiality(target, decision.materiality)
        check_edits(decision.edits, risks)
        # 同一决定 ID 再提交 (内容可能不同) 时, put 会按 (id, version) 返回旧决定, 效果却按新内容算: 一律拒绝
        if await store.get(eid, GateDecision, decision.id) is not None:
            raise DecisionRejected("INVALID_VALUE", f"决定 ID {decision.id} 已用过")
        return target

    async def submit(self, eid: str, decision: ReviewRecord | GateDecision) -> None:
        """提交一个决定: 校验通过则写入并在后台继续执行, 立即返回; 校验不过抛 DecisionRejected, 什么都不写."""
        if isinstance(decision, ReviewRecord):
            raise NotImplementedError("复核决定随 P2 实现")
        async with self._deciding(eid) as (_, snapshot):
            if current_gate(snapshot.next) is not decision.gate:
                raise DecisionRejected("NOT_AT_GATE", f"当前不在 {decision.gate} 闸门")
            await self.check_gate_decision(eid, decision)
            if decision.gate is not GateId.ACC_01:
                raise NotImplementedError(f"{decision.gate} 的闸门决定已校验, 效果尚未实现")

            effects = acc01_effects(snapshot.values["procedure_pack"], decision)
            values = await self._write_acc01(eid, decision, effects)
            # 以闸门节点身份写入: 检查点越过闸门, 下一步由 gate_route 按刚写入的决定选出口
            await self.app.aupdate_state(GraphRunner.config(eid), values, as_node="gate_acc01")

    async def _write_acc01(self, eid: str, decision: GateDecision, effects: StatePatch) -> StatePatch:
        """在一个事务内写入 ACC-01 决定及其效果, 提交后写 decision 留痕.

        返回要写入状态的片段 (决定换成 Store 返回的对象).
        """
        output_store = self.deps.output_store
        async with output_store.transaction(eid):
            stored = await save_outputs(output_store, eid, {"gate_decisions": [decision]}, actor=decision.decided_by)
            if "procedure_pack" in effects:  # approve, reject: ACC-01 出 completed 新版本
                acc01 = next(p for p in effects["procedure_pack"] if p.id == "ACC-01")
                await output_store.put(eid, [acc01], actor=AgentId.A1)
            if "phase" in effects:  # 阶段变化同步到 engagements 表; 拒绝承接时最后置 locked
                declined = effects["phase"] is Phase.Declined
                await self.deps.engagement_store.update(eid, phase=effects["phase"], locked=True if declined else None)
        # 留痕是独立事务, 不随业务回滚: 提交成功后再写, 才不会留下业务已回滚的决定.
        # 防重复键为决定 ID. 写失败时 Store 已提交而检查点未跟上: 再提交报 RESUME_REQUIRED
        # (拒绝承接已置 locked 的报 ENGAGEMENT_LOCKED), 由 resume/recover 补做并补写留痕
        await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.decision,
                actor=decision.decided_by,
                procedure_id="ACC-01",
                summary=f"ACC-01 {decision.decision.value}",
                idempotency_key=decision.id,
                payload={"decision_id": decision.id, "reason": decision.reason},
            )
        )
        return {**effects, **stored}

    async def update_client_profile(self, eid: str, profile: ClientProfile, *, updated_by: str) -> None:
        """更新客户资料: 只在停在 ACC-01 闸门时可用. 保存新版本画像后, 路由回 p0_intel 重跑 ACC-04 再重新呈报.

        profile.id 须为项目 client_id, version 须为该客户画像的最新版本 +1 (画像跨项目共用, 版本号全局递增).
        保存画像与改项目采用的版本在一个事务内, 提交后写 decision 留痕. updated_by 为操作用户 ID, 写入留痕.
        """
        async with self._deciding(eid) as (engagement, snapshot):
            if current_gate(snapshot.next) is not GateId.ACC_01:
                raise DecisionRejected("NOT_AT_GATE", "只能在 ACC-01 闸门更新客户资料")
            # 第 7 节校验顺序: VERSION_MISMATCH 先于 INVALID_VALUE. 版本号按项目客户的最新画像算, 不按 profile.id
            latest = await self.deps.engagement_store.client_profile(engagement.client_id)
            expected = latest.version + 1 if latest is not None else 1
            if profile.version != expected:
                raise DecisionRejected("VERSION_MISMATCH", f"客户画像应为第 {expected} 版")
            if profile.id != engagement.client_id:
                raise DecisionRejected("INVALID_VALUE", f"profile.id 应为 {engagement.client_id}")

            async with self.deps.output_store.transaction(eid):  # 画像新版本与本项目采用的版本一起提交
                saved = await self.deps.engagement_store.save_profile(profile)
                await self.deps.engagement_store.update(eid, client_profile_version=saved.version)
            await self.deps.trail.record(  # 提交后写, 理由同 _write_acc01
                TrailEntry(
                    engagement_id=eid,
                    kind=TrailKind.decision,
                    actor=updated_by,
                    procedure_id="ACC-01",
                    summary=f"客户资料更新至第 {saved.version} 版",
                    idempotency_key=f"{saved.id}-v{saved.version}",
                    payload={"client_id": saved.id, "version": saved.version},
                )
            )
            # 只写新版本号: ACC-01 的路由发现评分卡依据的版本落后, 选 rerun 回 p0_intel
            await self.app.aupdate_state(
                GraphRunner.config(eid), {"client_profile_version": saved.version}, as_node="gate_acc01"
            )
