"""更正通知与可载入的更正分录 (需求 R163): POST correction-notices, GET adjustment-entries."""

import asyncio
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_decisions, get_deps, owned_engagement
from backend.api.errors import catch_api
from backend.api.schemas import AdjustmentEntryView, CorrectionNoticeIn, IdBody
from backend.contracts.models import CorrectionNotice, Engagement, User
from backend.orchestration.decisions import GraphDecisionSubmit

router = APIRouter(prefix="/engagements/{eid}", tags=["corrections"])


@router.post("/correction-notices", status_code=202)
async def submit_correction_notice(
    body: CorrectionNoticeIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> IdBody:
    """停在等待更正时提交更正通知 (第 7 节 "更正通知"): id 为 D-{uuid4}, 提交人与时刻由服务端填写; handler 直接
    await submit. 不在等更正 409 NOT_AWAITING_CORRECTION, 其余校验同其他决定."""
    notice = CorrectionNotice.model_validate(
        {**body.model_dump(), "id": f"D-{uuid4()}", "decided_by": user.id, "decided_at": datetime.now(UTC)}
    )
    async with catch_api():
        await decisions.submit(engagement.id, notice)
    return IdBody(id=notice.id)


@router.get("/adjustment-entries")
async def list_adjustment_entries(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[AdjustmentEntryView]:
    """项目数据集可载入的更正分录 (DatasetLoader.adjustment_entries, 按凭证号), loaded 为总账里已有该凭证号的行;
    没有数据集或数据集没有更正分录 (如 Numbat) 为空."""
    dataset = engagement.dataset
    if dataset is None:
        return []
    async with catch_api():
        entries = await asyncio.to_thread(deps.datasets.adjustment_entries, dataset)
        lines = await deps.financial_store.journal_entries(engagement.id, entry_nos=[e.entry_no for e in entries])
    loaded = {line.entry_no for line in lines}
    return [AdjustmentEntryView(**entry.model_dump(), loaded=entry.entry_no in loaded) for entry in entries]
