"""DecisionSubmit (A 负责): 审计师决定的唯一写入口. 取锁 → 校验 → 一个事务内写入决定及其效果 → 越过闸门 → 后台继续.

校验顺序, 必填表, 决定的效果与恢复机制见 开发接口文档.md 第 7 节. 本版先实现 ACC-01 闸门决定.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    GateId,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureStatus,
    ReviewRecord,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.nodes import save_outputs, update_procedure
from backend.orchestration.query import current_gate
from backend.orchestration.runner import GraphRunner

ACC01_NOT_ALLOWED = ("materiality", "edits", "disposition", "annotations")  # 其他闸门专用字段, ACC-01 决定不得填写


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


def check_acc01_fields(decision: GateDecision) -> None:
    """ACC-01 决定的字段校验: reject 与 insufficient_info 须填 reason; 其他闸门专用字段须为空."""
    if decision.decision is not GateVerdict.approve and not decision.reason:
        raise DecisionRejected("MISSING_FIELD", "reason")
    # None 与空列表都视为未填
    filled = [name for name in ACC01_NOT_ALLOWED if getattr(decision, name)]
    if filled:
        raise DecisionRejected("FIELD_NOT_ALLOWED", ", ".join(filled))


class GraphDecisionSubmit:
    """DecisionSubmit 的实现: 审计师所有决定的唯一写入口.

    写入顺序: 先在一个事务内写 Store (决定, 程序新版本, 留痕, 项目阶段), 再以闸门节点身份写入检查点,
    最后交给 runner 在后台继续执行. 项目锁从校验前一直持有到后台执行停下.
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

        两种情况: Store 中有检查点里没有的决定; 客户画像最新版本高于检查点的 client_profile_version.
        """
        eid = engagement.id
        stored = {d.id for d in await self.deps.output_store.latest(eid, GateDecision)}
        stored |= {r.id for r in await self.deps.output_store.latest(eid, ReviewRecord)}
        in_state = {d.id for d in values.get("gate_decisions", [])} | {r.id for r in values.get("review_records", [])}
        missing = stored - in_state
        if missing:
            raise DecisionRejected("RESUME_REQUIRED", ", ".join(sorted(missing)))
        version_in_state = values.get("client_profile_version")  # 未启动的项目没有
        latest = await self.deps.engagement_store.client_profile(engagement.client_id)
        if version_in_state is not None and latest is not None and latest.version > version_in_state:
            raise DecisionRejected("RESUME_REQUIRED", f"客户画像第 {latest.version} 版未写入检查点")

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

    async def submit(self, eid: str, decision: ReviewRecord | GateDecision) -> None:
        """提交一个决定: 校验通过则写入并在后台继续执行, 立即返回; 校验不过抛 DecisionRejected, 什么都不写."""
        if isinstance(decision, ReviewRecord):
            raise NotImplementedError("复核决定随 P2 实现")
        async with self._deciding(eid) as (_, snapshot):
            if current_gate(snapshot.next) is not decision.gate:
                raise DecisionRejected("NOT_AT_GATE", f"当前不在 {decision.gate} 闸门")
            if decision.gate is not GateId.ACC_01:
                raise NotImplementedError(f"{decision.gate} 的闸门决定尚未实现")

            # 决定须绑定材料的当前版本 (读 Store): 审计师看材料期间评分卡可能已重跑出新版
            scorecard = await self.deps.output_store.get(eid, AcceptanceScorecard, "SCORECARD")
            if scorecard is None or (decision.target_id, decision.target_version) != (scorecard.id, scorecard.version):
                raise DecisionRejected("VERSION_MISMATCH", "评分卡已更新, 请刷新后重新决定")
            check_acc01_fields(decision)

            effects = acc01_effects(snapshot.values["procedure_pack"], decision)
            values = await self._write_acc01(eid, decision, effects)
            # 以闸门节点身份写入: 检查点越过闸门, 下一步由 gate_route 按刚写入的决定选出口
            await self.app.aupdate_state(GraphRunner.config(eid), values, as_node="gate_acc01")

    async def _write_acc01(self, eid: str, decision: GateDecision, effects: StatePatch) -> StatePatch:
        """在一个事务内写入 ACC-01 决定及其效果, 返回要写入状态的片段 (决定换成 Store 返回的对象)."""
        output_store = self.deps.output_store
        async with output_store.transaction(eid):
            stored = await save_outputs(output_store, eid, {"gate_decisions": [decision]}, actor=decision.decided_by)
            if "procedure_pack" in effects:  # approve, reject: ACC-01 出 completed 新版本
                acc01 = next(p for p in effects["procedure_pack"] if p.id == "ACC-01")
                await output_store.put(eid, [acc01], actor=AgentId.A1)
            await self.deps.trail.record(
                TrailEntry(
                    engagement_id=eid,
                    kind=TrailKind.decision,
                    actor=decision.decided_by,
                    procedure_id="ACC-01",
                    summary=f"ACC-01 {decision.decision.value}",
                    payload={"decision_id": decision.id, "reason": decision.reason},
                )
            )
            if "phase" in effects:  # 阶段变化同步到 engagements 表; 拒绝承接时最后置 locked
                declined = effects["phase"] is Phase.Declined
                await self.deps.engagement_store.update(eid, phase=effects["phase"], locked=True if declined else None)
        return {**effects, **stored}

    async def update_client_profile(self, eid: str, profile: ClientProfile, *, updated_by: str) -> None:
        """更新客户资料: 只在停在 ACC-01 闸门时可用. 保存新版本画像后, 路由回 p0_intel 重跑 ACC-04 再重新呈报.

        profile.id 须为项目 client_id, version 须为最新版本 +1. updated_by 为操作用户 ID, 写入留痕.
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

            async with self.deps.output_store.transaction(eid):  # save_profile 与留痕加入同一事务
                saved = await self.deps.engagement_store.save_profile(profile)
                await self.deps.trail.record(
                    TrailEntry(
                        engagement_id=eid,
                        kind=TrailKind.decision,
                        actor=updated_by,
                        procedure_id="ACC-01",
                        summary=f"客户资料更新至第 {saved.version} 版",
                        payload={"client_id": saved.id, "version": saved.version},
                    )
                )
            # 只写最新画像版本号: ACC-01 的路由发现评分卡依据的版本落后, 选 rerun 回 p0_intel
            await self.app.aupdate_state(
                GraphRunner.config(eid), {"client_profile_version": saved.version}, as_node="gate_acc01"
            )
