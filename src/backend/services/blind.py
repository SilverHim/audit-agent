"""盲判过滤的空实现 (contracts/ports.BlindFilter, 接口文档第 4 节): 原样转成视图, 不隐藏任何字段.

判定哪些判断与留痕须隐藏, 以及遮盖规则 (先答后看, 争议项不开盲判), 由降级段的需求实现.
"""

from backend.contracts.models import Judgment, TrailEntry
from backend.contracts.views import JudgmentView, TrailEntryView


class PassThroughBlindFilter:
    """不隐藏: 判断与留痕原样转成视图, hidden_fields 为空."""

    async def judgment(self, eid: str, j: Judgment) -> JudgmentView:
        return JudgmentView.model_validate(j.model_dump())

    async def trail(self, eid: str, entry: TrailEntry) -> TrailEntryView:
        return TrailEntryView.model_validate(entry.model_dump())
