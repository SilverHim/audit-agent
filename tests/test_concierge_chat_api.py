"""审计助理对话接口 (api/routes/conversation.py)."""

import asyncio
import json
from typing import Any

import pytest
from starlette.requests import ClientDisconnect

from backend.adapters.llm import LLMTransportError
from backend.agents.concierge_chat.rule_model import RuleChatModel
from backend.api.routes.conversation import stream_concierge
from backend.contracts.conversation import ConciergeQuestion
from backend.contracts.models import Phase, TrailKind
from backend.core.security import create_access_token
from backend.main import create_app
from tests.chat_fakes import ScriptedChatModel, chat_client, chat_deps, make_chat, parse_sse
from tests.fakes import ENGAGEMENT, NUMBAT_AUDITOR, NUMBAT_PROFILE, FakeEngagementStore, FakeTrail
from tests.test_concierge_chat_service import StallingModel

EID = ENGAGEMENT.id
BASE = f"/api/v1/engagements/{EID}/conversation"


async def test_ask_then_list_history() -> None:
    chat = make_chat(RuleChatModel())
    async with chat_client(chat) as client:
        assert (await client.get(BASE)).json() == []
        response = await client.post(BASE, json={"question": "现在到哪一步了？", "lang": "zh"})
        assert response.status_code == 200
        answer = response.json()
        assert answer["answer"].startswith("项目目前处于执行阶段")
        [turn] = (await client.get(BASE)).json()
    assert turn["question"] == "现在到哪一步了？" and turn["answer"] == answer["answer"]
    assert turn["trail_id"] == answer["trail_id"] and turn["lang"] == "zh" and turn["aborted"] is False
    assert turn["created_at"] is not None


async def test_stream_returns_events_and_same_answer_as_history() -> None:
    chat = make_chat(RuleChatModel())
    async with chat_client(chat) as client:
        response = await client.post(f"{BASE}/stream", json={"question": "重要性是多少？", "lang": "zh"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)
        [turn] = (await client.get(BASE)).json()
    names = [name for name, _ in events]
    assert names[0] == "tool" and names[-2:] == ["citations", "done"]
    assert "".join(data["text"] for name, data in events if name == "delta") == turn["answer"]


async def test_question_validation() -> None:
    async with chat_client(make_chat(RuleChatModel())) as client:
        too_long = await client.post(BASE, json={"question": "问" * 2001, "lang": "zh"})
        blank = await client.post(f"{BASE}/stream", json={"question": "   ", "lang": "zh"})
        bad_lang = await client.post(BASE, json={"question": "hi", "lang": "fr"})
    assert (too_long.status_code, blank.status_code, bad_lang.status_code) == (422, 422, 422)


async def test_unknown_engagement_is_404() -> None:
    async with chat_client(make_chat(RuleChatModel())) as client:
        response = await client.post("/api/v1/engagements/ENG-NOPE/conversation", json={"question": "hi", "lang": "en"})
    assert response.status_code == 404


async def test_busy_engagement_is_409_before_streaming() -> None:
    model = StallingModel()
    chat = make_chat(model)
    turn = chat.begin(EID, "U-1", ConciergeQuestion(question="第一问", lang="zh"))
    async with chat_client(chat) as client:
        stream = await client.post(f"{BASE}/stream", json={"question": "第二问", "lang": "zh"})
        plain = await client.post(BASE, json={"question": "第二问", "lang": "zh"})
    assert stream.status_code == plain.status_code == 409
    assert stream.json()["detail"]["code"] == "CONVERSATION_BUSY"
    model.release.set()
    await turn.result()


async def test_signed_engagement_can_still_be_asked() -> None:
    locked = ENGAGEMENT.model_copy(update={"locked": True, "phase": Phase.Archived})
    deps = chat_deps(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [locked]))
    async with chat_client(make_chat(RuleChatModel(), deps)) as client:
        response = await client.post(BASE, json={"question": "重要性是多少？", "lang": "zh"})
    assert response.status_code == 200


async def test_upstream_failure_maps_to_502() -> None:
    chat = make_chat(ScriptedChatModel([LLMTransportError("HTTP 500 boom")]))
    async with chat_client(chat) as client:
        response = await client.post(BASE, json={"question": "你好", "lang": "zh"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "LLM_UNAVAILABLE"


async def test_asgi_24_send_failure_stops_the_turn() -> None:
    """ASGI 2.4 下 Starlette 不监听断开, 而是 send 抛 OSError; 它不关闭事件生成器, 要靠 TurnStream 收尾."""
    trail = FakeTrail()
    model = StallingModel()
    chat = make_chat(model, chat_deps(trail))
    response = await stream_concierge(ConciergeQuestion(question="第一问", lang="zh"), ENGAGEMENT, NUMBAT_AUDITOR, chat)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("connection closed")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}
    with pytest.raises((OSError, ClientDisconnect)):
        await response(scope, receive, send)
    await chat.settle()
    assert EID not in chat.busy and model.closed
    [entry] = [e for e in trail.entries if e.kind is TrailKind.conversation]
    assert entry.payload["aborted"] is True


async def test_client_disconnect_mid_stream_records_aborted_turn() -> None:
    """手写 ASGI 驱动: 收到第一段 delta 后发 http.disconnect (httpx 的 ASGITransport 会缓冲整段响应, 模拟不了)."""
    trail = FakeTrail()
    model = StallingModel()
    chat = make_chat(model, chat_deps(trail))
    app = create_app()
    app.state.deps = chat.deps
    app.state.concierge = chat
    body = json.dumps({"question": "现在到哪一步了？", "lang": "zh"}).encode()
    gone = asyncio.Event()
    requested = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and b"delta" in message.get("body", b""):
            gone.set()

    path = f"{BASE}/stream"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {create_access_token('U-1')}".encode()),
        ],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), 5)
    await chat.settle()  # 响应结束时已通知取消; aborted 留痕由独立任务写
    [entry] = [e for e in trail.entries if e.kind is TrailKind.conversation]
    assert entry.payload["aborted"] is True and entry.payload["answer"] == "项目目前"
    assert model.closed and EID not in chat.busy
    assert sent[0]["status"] == 200
