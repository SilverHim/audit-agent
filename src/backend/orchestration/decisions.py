"""DecisionSubmit (A 负责, M2 实现): 取锁 → 校验 → 一个事务内写入决定及其效果 → 后台恢复流程.

校验顺序, 必填表, 决定的效果与恢复机制见 开发接口文档.md 第 7 节.
"""

from backend.contracts.models import (
    ClientProfile,
    GateDecision,
    GateVerdict,
    Phase,
    Procedure,
    ProcedureStatus,
    ReviewRecord,
)
from backend.contracts.state import StatePatch
from backend.orchestration.nodes import update_procedure


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


class GraphDecisionSubmit:
    """DecisionSubmit 的实现: 审计师所有决定的唯一写入口."""

    async def submit(self, eid: str, decision: ReviewRecord | GateDecision) -> None:
        raise NotImplementedError

    async def update_client_profile(self, eid: str, profile: ClientProfile) -> None:
        """更新客户资料: 取锁 → 校验停在 ACC-01 闸门, profile.id 为项目 client_id, version 为最新 +1 →
        保存新版本画像并写 decision 留痕 → aupdate_state 写 client_profile_version (as_node="gate_acc01") →
        后台恢复, 路由回 p0_intel 重跑 ACC-04 后重新呈报."""
        raise NotImplementedError
