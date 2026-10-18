"""数据管理接口 (需求 R10-R16): /engagements/{eid}/data 下的概览, 导入, 补交请求与就绪确认."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.deps import current_user, get_data_service, owned_engagement
from backend.api.errors import catch_api
from backend.contracts.data import DataReadiness, DataSummary
from backend.contracts.models import Engagement, User
from backend.services.data_service import DataService

router = APIRouter(prefix="/engagements/{eid}/data", tags=["data"])


@router.get("/summary")
async def summary(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DataSummary:
    """数据管理页的概览. processing 不为 0 时前端每 3 秒轮询."""
    async with catch_api():
        return await service.summary(engagement.id)


@router.post("/ready", status_code=202)
async def ready(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DataReadiness:
    """确认数据已齐, 越过 P1 开始前的等待. 不在等数据 409 NOT_AWAITING_DATA; 缺必需表 409 DATA_NOT_READY
    (detail.missing_tables); 项目锁定 423, 忙 409 ENGAGEMENT_BUSY. 返回确认后的就绪状态, 后台已在推进."""
    async with catch_api():
        return await service.confirm_ready(engagement.id, user.id)
