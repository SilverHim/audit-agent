"""GET /datasets: 新建项目时可选的客户 (数据集), 前端做成下拉选择."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps
from backend.api.schemas import DatasetSummary
from backend.core.config import get_settings

router = APIRouter(prefix="/datasets", tags=["datasets"], dependencies=[Depends(current_user)])


@router.get("")
def list_datasets(deps: Annotated[AgentDeps, Depends(get_deps)]) -> list[DatasetSummary]:
    """DatasetLoader.datasets 列出的数据集, 名称与 ABN 取各自的客户画像, 期间取 DatasetLoader.period; 画像或期间读不了
    的跳过. 数据集名等于配置项 DEMO_DATASET 的一行 default 为真 (不在清单时都为假). 按数据集名排序."""
    default = get_settings().DEMO_DATASET
    summaries: list[DatasetSummary] = []
    for name in deps.datasets.datasets():
        try:
            profile = deps.datasets.client_profile(name)
            period = deps.datasets.period(name)
        except (OSError, ValueError):  # 文件缺失或不合格 (ValidationError 是 ValueError 的子类)
            continue
        start, end = period if period is not None else (None, None)
        summaries.append(
            DatasetSummary(
                dataset=name,
                legal_name=profile.legal_name,
                abn=profile.abn,
                period_start=start,
                period_end=end,
                default=name == default,
            )
        )
    return summaries
