"""LiveLLMClient: OpenAI 兼容接口. 单元测试用假 HTTP 传输 (httpx.MockTransport), 不联网;
联网测试标 slow, 需在 .env 配好 LLM_MODE=live, LLM_MODEL, LLM_API_KEY (只发合成数据)."""

import json
import logging
from datetime import date
from typing import Any

import httpx
import pytest

from backend.adapters.llm import (
    DEFAULT_BASE_URL,
    ERROR_TEXT_LIMIT,
    LiveLLMClient,
    LLMTransportError,
    MockLLMClient,
    make_llm_client,
    strip_fences,
)
from backend.contracts.llm_outputs import ConfirmationExtraction
from backend.contracts.models import TrailKind
from backend.contracts.ports import LLMMessage, LLMRequest
from backend.core.config import Settings, get_settings
from backend.orchestration.nodes import GraphNodes
from tests.test_ingest_doc06 import raw, setup

KEY = "sk-test-0123456789"
REQUEST = LLMRequest(
    procedure_id="DOC-06",
    run_id="RUN-DOC-06-1",
    system="Return one JSON object.",
    messages=[LLMMessage(role="user", content="Extract the reply.")],
    max_tokens=4096,
    temperature=0,
    fixture_key="BA-01",
)


def completion(content: str, model: str = "deepseek-chat") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 812, "completion_tokens": 96},
    }


class Server:
    """假接口: 按顺序给出预置的响应 (状态码与 JSON, 或要抛的异常), 记下收到的请求."""

    def __init__(self, *replies: tuple[int, Any] | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)


def client(server: Server, sleeps: list[float] | None = None, **kwargs: Any) -> LiveLLMClient:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return LiveLLMClient(
        KEY,
        "deepseek-chat",
        "https://llm.example.test/v1/",
        transport=httpx.MockTransport(server.handler),
        sleep=sleep,
        **kwargs,
    )


# ── 请求与解析 ─────────────────────────────────────────


async def test_request_body_and_parsed_output() -> None:
    """验收 2: system 作首条消息, temperature 与 max_tokens 透传, 要求 JSON 输出;
    解析出模型实例, 模型名与 token 数取响应."""
    server = Server((200, completion(raw("BA-01"))))
    response = await client(server).complete(REQUEST, ConfirmationExtraction)

    [request] = server.requests
    assert str(request.url) == "https://llm.example.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body == {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Return one JSON object."},
            {"role": "user", "content": "Extract the reply."},
        ],
        "temperature": 0,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }
    assert isinstance(response.output, ConfirmationExtraction)
    assert (response.output.confirmed_balance_cents, response.output.balance_date) == (120840000, date(2026, 6, 30))
    assert (response.model, response.input_tokens, response.output_tokens, response.error) == (
        "deepseek-chat",
        812,
        96,
        None,
    )
    assert response.raw == raw("BA-01")


async def test_code_fence_is_stripped_before_parsing() -> None:
    server = Server((200, completion(f"```json\n{raw('BA-01')}\n```")))
    response = await client(server).complete(REQUEST, ConfirmationExtraction)
    assert isinstance(response.output, ConfirmationExtraction) and response.raw.startswith("```json")


@pytest.mark.parametrize("content", ["not json", '{"account_no": 1}', ""])
async def test_unparsable_output_keeps_raw_and_error(content: str) -> None:
    """验收 2: 解析失败时 output 为空, raw 保留原文, error 为解析错误 (交给校验与重试, 不抛异常)."""
    response = await client(Server((200, completion(content)))).complete(REQUEST, ConfirmationExtraction)
    assert response.output is None and response.raw == content and response.error


def test_strip_fences() -> None:
    assert strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences('{"a": 1}') == '{"a": 1}'


async def test_malformed_completion_raises() -> None:
    with pytest.raises(LLMTransportError, match="Chat Completions"):
        await client(Server((200, {"model": "x", "choices": []}))).complete(REQUEST, ConfirmationExtraction)


# ── 传输层重试 ─────────────────────────────────────────


@pytest.mark.parametrize(
    "failure", [(429, {"error": "rate limited"}), (500, "oops"), (503, "busy"), httpx.ReadTimeout("slow")]
)
async def test_retryable_failures_back_off_then_succeed(failure: tuple[int, Any] | Exception) -> None:
    """验收 3: 限流, 5xx, 超时按指数退避重试 (1, 2, 4 秒), 第 4 次成功."""
    sleeps: list[float] = []
    server = Server(failure, failure, failure, (200, completion(raw("BA-01"))))
    response = await client(server, sleeps).complete(REQUEST, ConfirmationExtraction)
    assert response.output is not None
    assert len(server.requests) == 4 and sleeps == [1.0, 2.0, 4.0]


async def test_retries_exhausted_raise() -> None:
    """验收 3: 重试 3 次后仍失败抛异常, 共 4 次请求."""
    sleeps: list[float] = []
    server = Server(*[(429, {"error": "rate limited"})] * 4)
    with pytest.raises(LLMTransportError, match="重试 3 次后仍失败: HTTP 429"):
        await client(server, sleeps).complete(REQUEST, ConfirmationExtraction)
    assert len(server.requests) == 4 and sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_not_retried(status: int) -> None:
    """验收 3: 400, 401 等不重试, 直接抛异常."""
    sleeps: list[float] = []
    server = Server((status, {"error": {"message": "bad request"}}))
    with pytest.raises(LLMTransportError, match=f"HTTP {status}"):
        await client(server, sleeps).complete(REQUEST, ConfirmationExtraction)
    assert len(server.requests) == 1 and sleeps == []


async def test_connection_error_not_retried() -> None:
    server = Server(httpx.ConnectError("refused"))
    with pytest.raises(LLMTransportError, match="连接失败"):
        await client(server).complete(REQUEST, ConfirmationExtraction)


async def test_key_redacted_from_errors() -> None:
    """验收 5: 接口在错误信息里回显密钥时, 异常信息中替换为 ***."""
    server = Server((401, {"error": {"message": f"Incorrect API key provided: {KEY}"}}))
    with pytest.raises(LLMTransportError) as e:
        await client(server).complete(REQUEST, ConfirmationExtraction)
    assert KEY not in str(e.value) and "***" in str(e.value)


# ── Codex R58 意见: 全部 5xx, 响应格式, 密钥回显, 先脱敏再截断 ─────────────────────────────────────────


@pytest.mark.parametrize("status", [501, 507, 520, 529, 599, 408])
async def test_every_5xx_and_408_retried(status: int) -> None:
    """(R58-01) 5xx 一律退避重试, 408 同样."""
    server = Server((status, "busy"), (200, completion(raw("BA-01"))))
    response = await client(server, []).complete(REQUEST, ConfirmationExtraction)
    assert response.output is not None and len(server.requests) == 2


@pytest.mark.parametrize(
    "data",
    [
        [completion("{}")],  # 不是对象
        {"choices": [{"message": {"content": ["a"]}}]},
        {"choices": [{"message": {"content": {"a": 1}}}]},
        {"choices": [{"message": {"content": 42}}]},
        {"choices": [{"message": "x"}]},
        completion("{}") | {"model": 7},
        completion("{}") | {"usage": ["x"]},
        completion("{}") | {"usage": {"prompt_tokens": "abc"}},
        completion("{}") | {"usage": {"completion_tokens": True}},
        completion("{}") | {"usage": {"prompt_tokens": -1}},
    ],
)
async def test_malformed_envelope_raises_transport_error(data: Any) -> None:
    """(R58-02) 响应结构或字段类型不对一律抛 LLMTransportError (不是 AttributeError, ValueError 等)."""
    with pytest.raises(LLMTransportError):
        await client(Server((200, data))).complete(REQUEST, ConfirmationExtraction)


async def test_null_content_is_empty_output() -> None:
    response = await client(Server((200, completion(None)))).complete(REQUEST, ConfirmationExtraction)  # type: ignore[arg-type]
    assert response.output is None and response.raw == "" and response.error


@pytest.mark.parametrize(
    "data",
    [
        completion(raw("BA-01") + f" {KEY}"),
        completion(json.dumps({"account_no": KEY})),
        completion(raw("BA-01")) | {"usage": {"prompt_tokens": KEY}},
        completion(raw("BA-01"), model=KEY),
    ],
    ids=["content", "content_field", "usage", "model"],
)
async def test_response_echoing_key_rejected(data: dict[str, Any]) -> None:
    """(R58-03) 成功响应中任何位置回显密钥: 整体拒绝, 不产出 raw 与校验错误, 异常信息不含密钥."""
    with pytest.raises(LLMTransportError, match="回显了密钥") as e:
        await client(Server((200, data))).complete(REQUEST, ConfirmationExtraction)
    assert KEY not in str(e.value)


async def test_key_echo_not_in_trail_through_engine() -> None:
    """(R58-03) 经真实 A3 与判断引擎: 回显密钥的响应使整次执行失败, 留痕与 Run 的失败原因都不含密钥."""
    server = Server((200, completion(raw("BA-01") + KEY)), (200, completion(raw("BA-02"))))
    agent, ctx, trail = setup(llm=client(server))
    patch, _ = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert all(KEY not in json.dumps(e.model_dump(mode="json"), ensure_ascii=False) for e in trail.entries)
    assert KEY not in json.dumps([r.model_dump(mode="json") for r in patch["procedure_runs"]])


async def test_httpx_log_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """(R58-03) 服务端在 reason phrase 中回显密钥: httpx 的 INFO 日志与异常信息都已脱敏."""
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="no", extensions={"reason_phrase": f"Bad key {KEY}".encode()})

    live = LiveLLMClient(KEY, "deepseek-chat", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMTransportError) as e:
        await live.complete(REQUEST, ConfirmationExtraction)
    assert "HTTP Request" in caplog.text and KEY not in caplog.text and "***" in caplog.text
    assert KEY not in str(e.value)


async def test_key_across_truncation_point_redacted() -> None:
    """(R58-04) 密钥跨过截断位置时也不留片段: 先脱敏再截断."""
    prefix = "Bad Request: "  # 异常信息正文前的 reason phrase
    body = "x" * (ERROR_TEXT_LIMIT - len(prefix) - 5) + KEY  # 密钥前 5 个字符落在截断位置之内
    with pytest.raises(LLMTransportError) as e:
        await client(Server((400, body))).complete(REQUEST, ConfirmationExtraction)
    assert KEY[:5] not in str(e.value)


# ── Codex R58 第 2 轮: 非法假值, 转义的密钥回显 ─────────────────────────────────────────


def escaped(text: str) -> str:
    """每个字符都写成 JSON 的 \\uXXXX 转义 (解码后还原为原文)."""
    return "".join(f"\\u{ord(c):04x}" for c in text)


@pytest.mark.parametrize(
    "changes",
    [
        {"usage": {"prompt_tokens": False}},
        {"usage": {"prompt_tokens": 0.0}},
        {"usage": {"completion_tokens": ""}},
        {"usage": {"completion_tokens": []}},
        {"usage": {"prompt_tokens": {}}},
        {"usage": []},
        {"usage": ""},
        {"usage": False},
        {"model": False},
        {"model": 0},
        {"model": []},
    ],
)
async def test_falsy_invalid_fields_rejected(changes: dict[str, Any]) -> None:
    """(R58-05) 给了值但类型不对的字段, 即使是假值 (False, 0.0, 空串, 空容器) 也报错, 不当作缺失取默认值."""
    with pytest.raises(LLMTransportError):
        await client(Server((200, completion(raw("BA-01")) | changes))).complete(REQUEST, ConfirmationExtraction)


@pytest.mark.parametrize(
    ("changes", "model", "tokens"),
    [
        ({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}, "deepseek-chat", (0, 0)),
        ({"usage": None}, "deepseek-chat", (0, 0)),
        ({"usage": {}}, "deepseek-chat", (0, 0)),
        ({"model": None}, "deepseek-chat", (812, 96)),
        ({"model": ""}, "deepseek-chat", (812, 96)),
    ],
)
async def test_missing_or_null_fields_take_defaults(
    changes: dict[str, Any], model: str, tokens: tuple[int, int]
) -> None:
    """缺失或 null 的字段取默认值; 合法的 0 照收."""
    response = await client(Server((200, completion(raw("BA-01")) | changes))).complete(REQUEST, ConfirmationExtraction)
    assert response.output is not None and (response.model, response.input_tokens, response.output_tokens) == (
        model,
        *tokens,
    )


async def test_missing_usage_field_takes_zero() -> None:
    data = completion(raw("BA-01"))
    del data["usage"]
    response = await client(Server((200, data))).complete(REQUEST, ConfirmationExtraction)
    assert (response.input_tokens, response.output_tokens) == (0, 0)


@pytest.mark.parametrize("where", ["model", "envelope_content", "content_field"])
async def test_escaped_key_echo_rejected(where: str) -> None:
    """(R58-06) 密钥以 \\uXXXX 转义回显 (正文中没有明文): 在响应信封, 或模型内容的 JSON 里, 解码后都被拦下."""
    content = raw("BA-01")
    model = "deepseek-chat"
    if where == "model":
        model = escaped(KEY)
    if where == "envelope_content":
        content = content[:-1] + ', "note": "' + escaped(KEY) + '"}'
    body = json.dumps(completion("CONTENT", model="MODEL")).replace('"MODEL"', f'"{model}"')
    if where == "content_field":  # 模型内容本身是 JSON, 其中的字段值写成转义: 信封解码后仍是转义文本, 再解析才还原
        inner = content[:-1] + ', "note": "' + escaped(KEY) + '"}'
        body = body.replace('"CONTENT"', json.dumps(inner))
    else:
        body = body.replace(
            '"CONTENT"', json.dumps(content) if where != "envelope_content" else '"' + content.replace('"', '\\"') + '"'
        )
    assert KEY not in body
    with pytest.raises(LLMTransportError, match="回显了密钥") as e:
        await client(Server((200, body))).complete(REQUEST, ConfirmationExtraction)
    assert KEY not in str(e.value)


async def test_escaped_model_echo_not_in_trail_through_engine() -> None:
    """(R58-06) 经真实 A3, 判断引擎与 A1: 模型名以转义回显密钥, 整次失败, 留痕与 Run 中都没有密钥."""
    body = json.dumps(completion(raw("BA-01"), model="MODEL")).replace('"MODEL"', f'"{escaped(KEY)}"')
    server = Server((200, body), (200, completion(raw("BA-02"))))
    agent, ctx, trail = setup(llm=client(server))
    patch, _ = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert all(KEY not in json.dumps(e.model_dump(mode="json"), ensure_ascii=False) for e in trail.entries)
    assert KEY not in json.dumps([r.model_dump(mode="json") for r in patch["procedure_runs"]])


# ── 配置 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("changes", "message"),
    [({"LLM_API_KEY": ""}, "LLM_API_KEY"), ({"LLM_MODEL": ""}, "LLM_MODEL")],
)
def test_live_without_key_or_model_fails_at_startup(changes: dict[str, str], message: str) -> None:
    """验收 1: live 模式密钥或模型名为空时 make_llm_client 抛配置错误 (build_deps 在启动时调用, 应用启动即失败)."""
    settings = Settings(LLM_MODE="live", LLM_MODEL="deepseek-chat", LLM_API_KEY=KEY).model_copy(update=changes)
    with pytest.raises(ValueError, match=message):
        make_llm_client(settings)


def test_make_llm_client_by_mode() -> None:
    live = make_llm_client(Settings(LLM_MODE="live", LLM_MODEL="deepseek-chat", LLM_API_KEY=KEY))
    assert isinstance(live, LiveLLMClient) and live.url == f"{DEFAULT_BASE_URL}/chat/completions"
    assert isinstance(make_llm_client(Settings(LLM_MODE="mock")), MockLLMClient)


# ── 经判断引擎调用 ─────────────────────────────────────────


async def test_doc06_through_judgment_engine_with_fake_http() -> None:
    """真实 A3 与判断引擎, 大模型换成指向假接口的 LiveLLMClient: 两份回函各一次通过, 留痕中没有密钥."""
    server = Server((200, completion(raw("BA-01"))), (200, completion(raw("BA-02"))))
    agent, ctx, trail = setup(llm=client(server))
    patch = await agent.run(ctx)
    balances = {c.bank_account_id: c.confirmed_balance_cents for c in patch["confirmations"]}
    assert balances == {"BA-01": 120840000, "BA-02": 40000000}
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert [e.model for e in calls] == ["deepseek-chat", "deepseek-chat"]
    assert all(KEY not in json.dumps(e.model_dump(mode="json"), ensure_ascii=False) for e in trail.entries)


# ── 联网 ─────────────────────────────────────────


@pytest.mark.slow
async def test_live_doc06_extracts_ba01_reply() -> None:
    """验收 4 (联网): .env 配好的真实接口读 BA-01 回函, 得回函余额 120840000, 截至 2026-06-30."""
    settings = get_settings()
    if not settings.LLM_API_KEY or not settings.LLM_MODEL:
        pytest.skip("未配置 LLM_MODEL 与 LLM_API_KEY")
    agent, ctx, trail = setup(llm=make_llm_client(settings.model_copy(update={"LLM_MODE": "live"})))
    patch = await agent.run(ctx)
    ba01 = next(c for c in patch["confirmations"] if c.bank_account_id == "BA-01")
    assert (ba01.confirmed_balance_cents, ba01.balance_date) == (120840000, date(2026, 6, 30))
    assert all(settings.LLM_API_KEY not in json.dumps(e.model_dump(mode="json")) for e in trail.entries)
