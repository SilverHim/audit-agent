"""DecisionSubmit（A 负责，M2 实现）：取锁 → 校验 → 一个事务内写入决定及其效果 → 后台恢复流程。

校验顺序、必填表、决定的效果与恢复机制见 开发接口文档.md 第 7 节。
"""

from backend.contracts.models import GateDecision, ReviewRecord


class GraphDecisionSubmit:
    async def submit(self, eid: str, decision: ReviewRecord | GateDecision) -> None:
        raise NotImplementedError
