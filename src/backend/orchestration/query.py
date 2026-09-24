"""StateQuery（A 负责，M2 实现）：总览与闸门材料。任务与程序状态读 AuditOutputStore。"""

from backend.contracts.views import EngagementOverview, GateView


class GraphStateQuery:
    async def overview(self, eid: str) -> EngagementOverview:
        raise NotImplementedError

    async def gate(self, eid: str) -> GateView | None:
        raise NotImplementedError
