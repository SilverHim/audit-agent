"""需求 R270: 演示数据导入的接口 (POST /data/demo-import) 的拒绝与概览的 demo 一项. 在 R269 的 Xero 假存储上, 经 HTTP
调接口; 不连库. 读快照与补造目录, 标 external. 停在 ACC-01 闸门经接口导入承接与各停点的 demo 在 test_demo_import 的
模块流程里."""

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.contracts.data import DataRequest, DemoImportResult
from backend.contracts.models import DocumentType, Engagement, TrailEntry, TrailKind
from backend.persistence.stores import XeroDatasetLoader
from backend.services.demo_import import DEMO_JOB
from tests.external import supplements_dir
from tests.fakes import NOW, api_client
from tests.test_demo_import import EID, Env, env
from tests.xero_fakes import PLANNING, SNAPSHOT_NAME, SNAPSHOT_REPO, XERO_DATASET, snapshot_root, xero_engagement
from tests.xero_flow import to_acceptance_gate

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


async def test_old_statements_trail_and_request_are_left_out() -> None:
    """Issue #46 前的项目: 留痕在承接之后还有 statements 阶段的 demo_import, 报表草稿请求仍 open. 概览不报错,
    stages_done 不列 statements, last 跳过那条取承接的结果, 也不因那条请求建议导入. 防删去阶段后读旧留痕报错, last
    变空, 或仍建议已没有的阶段."""
    e = env()
    acceptance = DemoImportResult(stage="acceptance", status="imported", uploaded=["appointment.pdf"], data_version=1)
    old = {"stage": "statements", "status": "imported", "uploaded": ["fs_v1.pdf"], "data_version": 11}
    for n, result in enumerate([acceptance.model_dump(mode="json"), old]):
        entry = TrailEntry(
            engagement_id=EID,
            kind=TrailKind.data,
            actor="U-1",
            summary="演示数据导入",
            payload={"action": DEMO_JOB, **result},
            created_at=NOW + timedelta(seconds=n),
        )
        await e.trail.record(entry)
    e.data.data_requests["REQ-financial_statements"] = DataRequest(
        id="REQ-financial_statements",
        kind="document",
        document_type=DocumentType.financial_statements,
        title="财务报表草稿",
        reason_code="not_provided",
        created_at=NOW,
    )

    summary = await e.service.demo_summary(EID)

    assert (summary.available, summary.stages_done, summary.suggested, summary.last) == (
        True,
        ["acceptance"],
        None,
        acceptance,
    )


def test_supplement_needs_the_snapshot_too(tmp_path: Path) -> None:
    """有没有演示资料看快照目录与补造目录是否都在 (不核对哈希): 快照不在时为假. 防只看补造目录, 显示了按钮而导入时
    才报错."""
    supplements = supplements_dir(SNAPSHOT_NAME).parent  # 补造根, 经 tests.external 核对
    found = XeroDatasetLoader(snapshot_root(SNAPSHOT_REPO).parent, PLANNING, supplements=supplements)
    missing = XeroDatasetLoader(tmp_path, PLANNING, supplements=supplements)
    assert (found.has_supplement(XERO_DATASET), missing.has_supplement(XERO_DATASET)) == (True, False)


async def test_busy_is_409() -> None:
    """停在 ACC-01 时建议导入承接资料; 图在后台跑 (runner 持锁) 时概览不建议, 导入报 409. 防图在跑时仍显示建议,
    点了才报忙."""
    e = env()
    await to_acceptance_gate(e.runner, EID)
    assert (await e.service.demo_summary(EID)).suggested == "acceptance"
    e.runner.acquire(EID)
    assert (await e.service.demo_summary(EID)).suggested is None
    [posted] = await call(e, (f"{BASE}/demo-import", {"stage": "acceptance"}))
    assert (posted.status_code, posted.json()["detail"]["code"]) == (409, "ENGAGEMENT_BUSY")
