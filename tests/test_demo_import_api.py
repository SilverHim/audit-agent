"""需求 R270: 演示数据导入的接口 (POST /data/demo-import) 的拒绝与概览的 demo 一项. 在 R269 的 Xero 假存储上, 经 HTTP
调接口; 不连库. 读快照与补造目录, 标 external. 停在 ACC-01 闸门经接口导入承接与各停点的 demo 在 test_demo_import 的
模块流程里."""

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.xero_supplements import STAGE_REQUESTS
from backend.contracts.data import DataRequest, RequestStatus
from backend.contracts.models import DocumentType, Engagement
from backend.persistence.stores import XeroDatasetLoader
from tests.external import supplements_dir
from tests.fakes import NOW, api_client
from tests.test_demo_import import EID, Env, env
from tests.xero_fakes import PLANNING, SNAPSHOT_NAME, SNAPSHOT_REPO, XERO_DATASET, snapshot_root, xero_engagement

pytestmark = pytest.mark.external
BASE = f"/api/v1/engagements/{EID}/data"


async def call(e: Env, *requests: tuple[str, dict[str, Any] | None]) -> list[Any]:
    """在 e 上依次发请求 (GET 时 body 为空), 返回各响应."""
    async with api_client(e.runner) as client:
        client._transport.app.state.data_service = e.service  # type: ignore[attr-defined]
        found = []
        for path, body in requests:
            found.append(await (client.post(path, json=body) if body is not None else client.get(path)))
        return found


@pytest.mark.parametrize(
    ("update", "stage", "status", "code"),
    [
        ({"dataset": "demo"}, "acceptance", 422, "INVALID_VALUE"),  # Numbat
        ({"dataset": None}, "acceptance", 422, "INVALID_VALUE"),  # 没有数据集的新客户
        ({"dataset": "xero/20261017-mock"}, "acceptance", 422, "INVALID_VALUE"),  # 没有补造目录
        ({}, "manual", 422, "INVALID_VALUE"),
        ({"locked": True}, "acceptance", 423, "ENGAGEMENT_LOCKED"),
    ],
)
async def test_rejected_requests_and_unavailable_summary(
    update: dict[str, Any], stage: str, status: int, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """其余数据集 422 且概览 available 为假 (不显示按钮), 不读检查点; 阶段不认识 422; 锁定 423. 演示数据集的概览只读
    一次检查点. 防每次轮询概览都为没有演示资料的项目读检查点, 或一次概览读两次."""
    engagement: Engagement = xero_engagement().model_copy(update=update)
    e = env(engagement=engagement)
    reads, state = Counter[str](), e.runner.app.aget_state

    async def counted(*args: Any, **kwargs: Any) -> Any:
        reads["state"] += 1
        return await state(*args, **kwargs)

    monkeypatch.setattr(e.runner.app, "aget_state", counted)
    posted, summary = await call(e, (f"{BASE}/demo-import", {"stage": stage}), (f"{BASE}/summary", None))
    assert (posted.status_code, posted.json()["detail"]["code"]) == (status, code)
    available = engagement.dataset == XERO_DATASET
    assert summary.json()["demo"] == {
        "available": available,
        "stages_done": [],
        "suggested": None,  # 项目还没启动: 不在任何一个导入的位置
        "last": None,
    }
    assert reads["state"] == (1 if available else 0)


@pytest.mark.parametrize(
    ("status", "busy", "suggested"),
    [
        ("open", False, "statements"),
        ("received", False, None),  # 审计师已在补交卡片上交了报表草稿
        ("unavailable", False, None),  # 标了客户无此类资料
        ("open", True, None),  # 图在后台跑 (持项目锁, 不是数据作业): 流程没停在任何位置, 这时导入报 409
    ],
)
async def test_statements_are_suggested_only_for_an_open_request_while_idle(
    status: RequestStatus, busy: bool, suggested: str | None
) -> None:
    """待审财报的补交请求为 open 且图没在后台跑时才建议 statements. 防请求已收到或标了没有时仍建议, 或图在跑时按钮可点
    却 409."""
    e = env()
    request_id = STAGE_REQUESTS["statements"]
    e.data.data_requests[request_id] = DataRequest(
        id=request_id,
        kind="document",
        document_type=DocumentType.financial_statements,
        title="待审财务报表",
        reason_code="not_provided",
        status=status,
        created_at=NOW,
    )
    if busy:
        e.runner.acquire(EID)
    assert (await e.service.demo_summary(EID)).suggested == suggested


def test_supplement_needs_the_snapshot_too(tmp_path: Path) -> None:
    """有没有演示资料看快照目录与补造目录是否都在 (不核对哈希): 快照不在时为假. 防只看补造目录, 显示了按钮而导入时
    才报错."""
    supplements = supplements_dir(SNAPSHOT_NAME).parent  # 补造根, 经 tests.external 核对
    found = XeroDatasetLoader(snapshot_root(SNAPSHOT_REPO).parent, PLANNING, supplements=supplements)
    missing = XeroDatasetLoader(tmp_path, PLANNING, supplements=supplements)
    assert (found.has_supplement(XERO_DATASET), missing.has_supplement(XERO_DATASET)) == (True, False)


async def test_busy_is_409() -> None:
    e = env()
    e.runner.acquire(EID)
    [posted] = await call(e, (f"{BASE}/demo-import", {"stage": "acceptance"}))
    assert (posted.status_code, posted.json()["detail"]["code"]) == (409, "ENGAGEMENT_BUSY")
