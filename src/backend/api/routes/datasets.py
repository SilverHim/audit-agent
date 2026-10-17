"""GET /datasets: 新建项目时可选的客户 (数据集), 前端做成下拉选择."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps
from backend.api.schemas import DatasetSummary

router = APIRouter(prefix="/datasets", tags=["datasets"], dependencies=[Depends(current_user)])


@router.get("")
def list_datasets(deps: Annotated[AgentDeps, Depends(get_deps)]) -> list[DatasetSummary]:
    """DatasetLoader.datasets 列出的数据集, 名称与 ABN 取各自的客户画像; 画像读不了的跳过. 按数据集名排序."""
    summaries: list[DatasetSummary] = []
    for name in deps.datasets.datasets():
        try:
            profile = deps.datasets.client_profile(name)
        except (OSError, ValueError):  # 画像文件缺失或不合格 (ValidationError 是 ValueError 的子类)
            continue
        summaries.append(DatasetSummary(dataset=name, legal_name=profile.legal_name, abn=profile.abn))
    return summaries
