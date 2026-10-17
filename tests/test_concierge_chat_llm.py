"""审计助理的流式对话模型 (adapters/chat_llm.py): 用假 HTTP 传输, 不连真实服务商."""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from backend.adapters.chat_llm import (
    ContextOverflow,
    LiveChatModel,
    LiveChatSource,
    SecretEcho,
    StreamBroken,
)
from backend.adapters.llm import LLMTransportError
from backend.contracts.conversation import AssistantTurn, ChatEvent, ChatMessage, TextDelta, ToolCall, ToolSpec
from backend.contracts.models import LlmProvider

KEY = "sk-test-SECRET-123456"  # 假密钥
URL = "https://llm.test/v1"
MESSAGES = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="现在到哪一步了?")]
TOOLS = [ToolSpec(name="get_overview", description="项目总览", parameters={"type": "object", "properties": {}})]


def chunk(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    body: dict[str, Any] = {"model": "gpt-served", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        body = {"model": "gpt-served", "choices": [], "usage": usage}
    return body


def sse(*chunks: dict[str, Any], done: bool = True) -> bytes:
    text = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


async def no_sleep(seconds: float) -> None:
    return None


def make(
    handler: Callable[[httpx.Request], httpx.Response], provider: LlmProvider = "openai", key: str = KEY
) -> LiveChatModel:
    return LiveChatModel(
        key, "gpt-test", URL, provider=provider, transport=httpx.MockTransport(handler), sleep=no_sleep
    )


async def collect(model: LiveChatModel, tools: list[ToolSpec] | None = None) -> list[ChatEvent]:
    return [e async for e in model.stream_chat(MESSAGES, TOOLS if tools is None else tools, max_tokens=500)]


def text_of(events: list[ChatEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_streams_text_then_turn_with_usage() -> None:
    body = sse(
        chunk("项目目前处于"),
        chunk("执行阶段，3 个程序已完成。"),
        chunk(finish="stop"),
        chunk(usage={"prompt_tokens": 120, "completion_tokens": 18}),
    )
    events = await collect(make(lambda request: httpx.Response(200, content=body)))
    turn = events[-1]
    assert isinstance(turn, AssistantTurn)
    assert text_of(events) == turn.text == "项目目前处于执行阶段，3 个程序已完成。"
    assert (turn.model, turn.input_tokens, turn.output_tokens, turn.finish_reason) == ("gpt-served", 120, 18, "stop")
    assert turn.tool_calls == []


async def test_assembles_tool_call_arguments_by_index() -> None:
    body = sse(
        chunk(tool_calls=[{"index": 0, "id": "call_a", "function": {"name": "get_judgment", "arguments": '{"judg'}}]),
        chunk(tool_calls=[{"index": 1, "function": {"name": "get_overview", "arguments": ""}}]),
        chunk(tool_calls=[{"index": 0, "function": {"arguments": 'ment_id": "J-1"}'}}]),
        chunk(finish="tool_calls"),
    )
    events = await collect(make(lambda request: httpx.Response(200, content=body)))
    turn = events[-1]
    assert isinstance(turn, AssistantTurn)
    assert turn.tool_calls == [
        ToolCall(id="call_a", name="get_judgment", arguments='{"judgment_id": "J-1"}'),
        ToolCall(id="call_1", name="get_overview", arguments=""),
    ]


@pytest.mark.parametrize(
    ("provider", "temperature", "usage"),
    [("openai", True, True), ("anthropic", False, True), ("deepseek", True, False)],
)
async def test_request_body_follows_provider(provider: LlmProvider, temperature: bool, usage: bool) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=sse(chunk("ok"), chunk(finish="stop")))

    history = [
        *MESSAGES,
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="c1", name="get_overview", arguments="{}")]),
        ChatMessage(role="tool", tool_call_id="c1", content='{"phase": "P2_Executing"}'),
    ]
    model = make(handler, provider)
    [e async for e in model.stream_chat(history, TOOLS, tool_choice="none", max_tokens=300)]
    body = seen[0]
    assert body["stream"] is True and body["model"] == "gpt-test" and body["max_tokens"] == 300
    assert ("temperature" in body) is temperature
    assert ("stream_options" in body) is usage
    assert body["tool_choice"] == "none"
    assert body["tools"][0] == {"type": "function", "function": TOOLS[0].model_dump()}
    assert body["messages"][2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_overview", "arguments": "{}"}}],
    }
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"phase": "P2_Executing"}'}


async def test_no_tools_sends_no_tool_fields() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=sse(chunk("摘要"), chunk(finish="stop")))

    await collect(make(handler), tools=[])
    assert "tools" not in seen[0] and "tool_choice" not in seen[0]


async def test_retries_429_before_first_event() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, content=sse(chunk("好的"), chunk(finish="stop")))

    events = await collect(make(handler))
    assert len(calls) == 2
    assert text_of(events) == "好的"


class BreaksAfter(httpx.AsyncByteStream):
    """先给一段数据, 再断开连接."""

    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        raise httpx.ReadError("connection reset")


async def test_no_retry_after_output_started() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, stream=BreaksAfter(sse(chunk("项目目前处于执行阶段"), done=False)))

    model = make(handler, provider="ollama", key="")  # 没有密钥: 不扣字, 第一段即放出
    events: list[ChatEvent] = []
    with pytest.raises(StreamBroken):
        async for event in model.stream_chat(MESSAGES, TOOLS, max_tokens=100):
            events.append(event)
    assert text_of(events) == "项目目前处于执行阶段"
    assert len(calls) == 1


async def test_stream_cut_before_finish_is_broken() -> None:
    body = sse(chunk("半截"), done=False)
    with pytest.raises(StreamBroken):
        await collect(make(lambda request: httpx.Response(200, content=body), provider="ollama", key=""))


async def test_secret_split_across_chunks_is_never_emitted() -> None:
    intro = "这是一段足够长的回答开头，用来确认安全的部分照常放出。"
    body = sse(chunk(intro + " sk-test-SEC"), chunk("RET-123456 之后的内容"), chunk(finish="stop"))
    events: list[ChatEvent] = []
    with pytest.raises(SecretEcho):
        async for event in make(lambda request: httpx.Response(200, content=body)).stream_chat(
            MESSAGES, TOOLS, max_tokens=100
        ):
            events.append(event)
    emitted = text_of(events)
    assert emitted and intro.startswith(emitted)
    assert "sk" not in emitted


async def test_secret_in_tool_arguments_is_rejected() -> None:
    call = {"index": 0, "id": "c", "function": {"name": "get_judgment", "arguments": json.dumps({"judgment_id": KEY})}}
    body = sse(chunk(tool_calls=[call]), chunk(finish="tool_calls"))
    with pytest.raises(SecretEcho):
        await collect(make(lambda request: httpx.Response(200, content=body)))


async def test_context_overflow_is_recognised_and_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded", "message": "too long"}})

    with pytest.raises(ContextOverflow):
        await collect(make(handler))
    assert len(calls) == 1


async def test_client_error_not_retried_and_redacted() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    with pytest.raises(LLMTransportError) as error:
        await collect(make(handler))
    assert len(calls) == 1
    assert KEY not in str(error.value) and "***" in str(error.value)


def rejects(param: str) -> httpx.Response:
    error = {"message": f"Unsupported parameter: '{param}'", "param": param, "code": "unsupported_parameter"}
    return httpx.Response(400, json={"error": error})


async def test_rejected_parameters_are_adapted_and_remembered_by_source() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "max_tokens" in body:
            return rejects("max_tokens")
        if "temperature" in body:
            return rejects("temperature")
        return httpx.Response(200, content=sse(chunk("ok"), chunk(finish="stop")))

    async def config() -> tuple[LlmProvider, str, str, str]:
        return "openai", URL, "gpt-6-luna", KEY

    source = LiveChatSource(config, transport=httpx.MockTransport(handler))
    model = await source.current()
    events = [e async for e in model.stream_chat(MESSAGES, TOOLS, max_tokens=50)]
    assert text_of(events) == "ok"
    assert len(seen) == 3  # 换参数立即重发, 不算重试
    assert seen[-1]["max_completion_tokens"] == 50 and "temperature" not in seen[-1]
    again = await source.current()  # 同一来源的下一轮直接按调整后的方式请求
    [e async for e in again.stream_chat(MESSAGES, TOOLS, max_tokens=50)]
    assert len(seen) == 4


async def test_other_400_is_not_adapted() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return rejects("tool_choice")

    with pytest.raises(LLMTransportError):
        await collect(make(handler))
    assert len(calls) == 1


async def test_source_without_model_name_reports_unconfigured() -> None:
    async def config() -> tuple[LlmProvider, str, str, str]:
        return "openai", URL, "", KEY

    model = await LiveChatSource(config).current()
    with pytest.raises(LLMTransportError, match="未配置大模型"):
        [e async for e in model.stream_chat(MESSAGES, TOOLS, max_tokens=10)]
