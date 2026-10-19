"""需求 R270: 演示数据导入的接口 (POST /data/demo-import) 与概览的 demo 一项. 在 R269 的 Xero 假存储与离线主图上,
经 HTTP 调接口; 不连库. 读快照与补造目录, 标 external."""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient

from backend.contracts.models import Engagement
from backend.services.demo_import import DemoImport
from tests.fakes import api_client
from tests.test_demo_import import ACTOR, EID, Env, env
from tests.xero_fakes import XERO_DATASET, xero_engagement
from tests.xero_flow import approve_acceptance, drain, to_acceptance_gate

pytestmark = pytest.mark.external
BASE = f"/api/v1/engagements/{EID}/data"


async def demo(client: AsyncClient) -> dict[str, Any]:
    response = await client.get(f"{BASE}/summary")
    assert response.status_code == 200
    return dict(response.json()["demo"])


async def call(e: Env, *requests: tuple[str, dict[str, Any] | None]) -> list[Any]:
    """在 e 上依次发请求 (GET 时 body 为空), 返回各响应."""
    async with api_client(e.runner) as client:
        client._transport.app.state.data_service = e.service  # type: ignore[attr-defined]
        found = []
        for path, body in requests:
            found.append(await (client.post(path, json=body) if body is not None else client.get(path)))
        return found


async def test_import_from_the_data_page_follows_the_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Xero 演示项目: 停在 ACC-01 闸门时概览建议 acceptance; 发起导入 202 (started, 要上传的文件), 作业期间
    processing 不为 0; 做完后 stages_done 记下 acceptance, 不再建议 (仍在闸门), last 为这次的结果; 过了 ACC-01 等数据
    时建议 fieldwork."""
    e = env()
    await to_acceptance_gate(e.runner, EID)
    gate, run = asyncio.Event(), DemoImport.run

    async def held(self: DemoImport, *args: Any) -> Any:  # 作业先停住, 好在作业期间读概览 (不靠时序)
        await gate.wait()
        return await run(self, *args)

    monkeypatch.setattr(DemoImport, "run", held)
    async with api_client(e.runner) as client:
        client._transport.app.state.data_service = e.service  # type: ignore[attr-defined]
        assert await demo(client) == {"available": True, "stages_done": [], "suggested": "acceptance", "last": None}
        started = await client.post(f"{BASE}/demo-import", json={"stage": "acceptance"})
        assert started.status_code == 202
        assert (started.json()["status"], started.json()["uploaded"]) == (
            "started",
            ["appointment.pdf", "engagement_letter.pdf"],
        )
        assert (await client.get(f"{BASE}/summary")).json()["processing"] == 2  # 两份承接文件
        gate.set()
        await drain(e.service)
        after = await demo(client)
        assert (after["stages_done"], after["suggested"]) == (["acceptance"], None)
        assert (after["last"]["stage"], after["last"]["status"], after["last"]["data_version"]) == (
            "acceptance",
            "imported",
            1,
        )
        await approve_acceptance(e.runner, EID, ACTOR)
        assert (await demo(client))["suggested"] == "fieldwork"


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
    update: dict[str, Any], stage: str, status: int, code: str
) -> None:
    """其余数据集 422 且概览 available 为假 (不显示按钮); 阶段不认识 422; 锁定 423."""
    engagement: Engagement = xero_engagement().model_copy(update=update)
    e = env(engagement=engagement)
    posted, summary = await call(e, (f"{BASE}/demo-import", {"stage": stage}), (f"{BASE}/summary", None))
    assert (posted.status_code, posted.json()["detail"]["code"]) == (status, code)
    available = engagement.dataset == XERO_DATASET
    assert summary.json()["demo"] == {
        "available": available,
        "stages_done": [],
        "suggested": None,  # 项目还没启动: 不在任何一个导入的位置
        "last": None,
    }


async def test_busy_is_409() -> None:
    e = env()
    e.runner.acquire(EID)
    [posted] = await call(e, (f"{BASE}/demo-import", {"stage": "acceptance"}))
    assert (posted.status_code, posted.json()["detail"]["code"]) == (409, "ENGAGEMENT_BUSY")
