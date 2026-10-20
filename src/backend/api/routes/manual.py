"""人工队列 (需求 R94): 人工程序派给人的 manual 任务. 证据文件经已有的 POST /documents 上传, 结果经已有的
POST /review-records 以 manual_result 提交."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.agents.base import AgentDeps
from backend.api.deps import get_deps, owned_engagement
from backend.api.errors import catch_api
from backend.api.schemas import ManualTaskView
from backend.contracts.manual_results import result_model
from backend.contracts.models import Engagement, ReviewTask, TaskKind, TaskStatus
from backend.services.judgment_engine import DOCUMENT_EVIDENCE

router = APIRouter(prefix="/engagements/{eid}", tags=["manual"])


@router.get("/manual-tasks")
async def list_manual_tasks(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[ManualTaskView]:
    """只列 manual 任务 (各取最新版), 待决的在前, 两组各按创建时间升序. result_schema 为任务请求的结果模型的 JSON
    schema (contracts/manual_results), 没有结果模型或模型名已不在登记里 (派发后改了名, 提交时拒绝) 时为空; documents
    为本项目已登记且可作证据的文件 (类型在判断引擎的文件指针映射内, 同提交时的核对), evidence_types 为它们各自作证据
    时的指针类型. 项目不存在 404."""
    async with catch_api():
        tasks = [t for t in await deps.output_store.latest(engagement.id, ReviewTask) if t.kind is TaskKind.manual]
        documents = [d for d in await deps.financial_store.documents(engagement.id) if d.type in DOCUMENT_EVIDENCE]
        evidence_types = {d.id: DOCUMENT_EVIDENCE[d.type] for d in documents}
        views: list[ManualTaskView] = []
        for task in sorted(tasks, key=lambda t: (t.status is not TaskStatus.pending, t.created_at)):
            request = task.manual_request
            assert request is not None  # ReviewTask 的模型校验保证 manual 任务带请求
            template = deps.library.load(task.procedure_id)
            model = result_model(request.result_model) if request.result_model is not None else None
            schema = model.model_json_schema() if model is not None else None
            views.append(
                ManualTaskView(
                    task=task,
                    procedure_name=template.name,
                    owner=template.owner,
                    request=request,
                    result_schema=schema,
                    documents=documents,
                    evidence_types=evidence_types,
                )
            )
    return views
