"""LiveLLMClient: OpenAI 兼容接口. 单元测试用假 HTTP 传输 (httpx.MockTransport), 不联网;
联网测试标 slow 与 live, 需配好 LLM_MODEL, LLM_API_KEY (环境变量或 .env, 见 tests.external.live_llm_settings; 只发合成
或公开数据)."""

import json
import logging
from datetime import date
from typing import Any

import httpx
import pytest

from backend.adapters.llm import (
    DEFAULT_BASE_URL,
    ERROR_TEXT_LIMIT,
    HTTP_LOGGERS,
    TRUNCATED,
    LiveLLMClient,
    LLMTransportError,
    MockLLMClient,
    RedactingFilter,
    SwitchableLLMClient,
    UnconfiguredLLMClient,
    install_redaction,
    make_live_client,
    make_llm_client,
    strip_fences,
)
from backend.contracts.llm_outputs import ConfirmationExtraction
from backend.contracts.models import TrailKind
from backend.contracts.ports import LLMMessage, LLMRequest
from backend.core.config import Settings
from backend.orchestration.nodes import GraphNodes
from tests.external import live_llm_settings
from tests.test_ingest_doc06 import raw, setup
from tests.tools.live_budget import Budget, BudgetedLLMClient, report

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


def api_error(message: str, param: str | None = None, code: str | None = None) -> tuple[int, Any]:
    """OpenAI 格式的 400 错误体."""
    return 400, {"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}}


# 服务端因输出达到上限回的 400 (R312 实测 gpt-6-luna, JSON 模式下输出不完整时不给半截内容), 及 OpenAI 的另一种写法
OUTPUT_LIMIT = api_error(
    "Could not finish the message because max_tokens or model output limit was reached. "
    "Please try again with higher max_tokens."
)
OUTPUT_LIMIT_OLDER = api_error(
    "Could not finish the message because max_tokens was reached. Please try again with higher max_tokens."
)


def finished(content: str, finish_reason: str | None) -> dict[str, Any]:
    """带 finish_reason 的响应 (为空时不带这个字段)."""
    data = completion(content)
    if finish_reason is not None:
        data["choices"][0]["finish_reason"] = finish_reason
    return data


@pytest.mark.parametrize(
    ("length", "finish_reason", "truncated"),
    [(40, "length", True), (40, "stop", False), (40, None, False), (None, "length", False)],
)
async def test_truncated_output_says_so(length: int | None, finish_reason: str | None, truncated: bool) -> None:
    """R312 验收 2: 半截输出且 finish_reason 为 length 时 error 写明在上限 (请求的 4096) 处被截断; stop 与缺失时照旧
    为解析错误; raw 都照常保留. 正文恰好完整 (length 但解析得出) 时照常出 output, 不写截断说明 (R312-07)."""
    content = raw("BA-01")[:length]
    response = await client(Server((200, finished(content, finish_reason)))).complete(REQUEST, ConfirmationExtraction)
    assert response.raw == content and (response.output is None) == (length is not None)
    assert (response.error == TRUNCATED.format(limit=4096)) == truncated, response.error


@pytest.mark.parametrize("reply", [OUTPUT_LIMIT, OUTPUT_LIMIT_OLDER], ids=["output_limit", "older"])
async def test_output_limit_400_is_a_truncated_reply(reply: tuple[int, Any]) -> None:
    """R312 验收 2, 5: 服务端因输出达到上限回 400 时 (两种写法, 认共同的开头) 不抛错 (此前整次执行因此失败), 返回 raw
    为空, token 数记 0, error 同截断; 不算传输层重试, 只发一次."""
    server = Server(reply)
    response = await client(server).complete(REQUEST, ConfirmationExtraction)
    assert (response.output, response.raw, response.input_tokens, response.output_tokens) == (None, "", 0, 0)
    assert response.error == TRUNCATED.format(limit=4096) and len(server.requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        api_error(  # OpenAI
            "max_tokens is too large: 4096. This model supports at most 2048 completion tokens, "
            "whereas you provided 4096.",
            "max_tokens",
            "invalid_value",
        ),
        api_error("Invalid max_tokens value, the valid range of max_tokens is [1, 2048]"),  # DeepSeek, 通义一类
    ],
    ids=["openai", "range"],
)
async def test_output_cap_from_the_error_caps_later_calls(reply: tuple[int, Any]) -> None:
    """R312-01: 输出上限超出模型所允许的 400 给出允许的上限 2048 时, 以 2048 立即重发 (不计传输重试, 不等待); 截断说明
    写实际发出的 2048; 此后的调用一开始就以 2048 封顶."""
    sleeps: list[float] = []
    server = Server(reply, (200, finished(raw("BA-01")[:40], "length")), (200, completion(raw("BA-01"))))
    llm = client(server, sleeps)
    first = await llm.complete(REQUEST, ConfirmationExtraction)
    second = await llm.complete(REQUEST, ConfirmationExtraction)
    assert [body["max_tokens"] for body in bodies(server)] == [4096, 2048, 2048] and sleeps == []
    assert first.error == TRUNCATED.format(limit=2048) and second.output is not None


@pytest.mark.parametrize(
    "reply",
    [
        api_error("max_tokens is too large", "max_tokens", "invalid_value"),  # 取不出数
        api_error(  # 允许的上限小于 1, 没有可发的上限 (R312-08: 曾当作没封顶, 以同一上限一直重发)
            "max_tokens is too large: 4096. This model supports at most 0 completion tokens, "
            "whereas you provided 4096.",
            "max_tokens",
            "invalid_value",
        ),
        api_error("Invalid max_tokens value, the valid range of max_tokens is [1, 8192]"),  # 已不大于允许的上限
        api_error("temperature must be in [0, 2]", "temperature", "invalid_value"),  # 不是输出上限的参数
    ],
    ids=["no_number", "zero", "not_lower", "other_param"],
)
async def test_output_cap_unusable_raises(reply: tuple[int, Any]) -> None:
    """R312-01: 取不出允许的上限, 给出的上限小于 1 或不比发出的小 (封顶也没用), 或说的不是输出上限时, 照常抛错,
    只发一次."""
    server = Server(reply)
    with pytest.raises(LLMTransportError, match="HTTP 400"):
        await client(server).complete(REQUEST, ConfirmationExtraction)
    assert len(server.requests) == 1


async def test_connect_timeout_is_short_and_read_timeout_long() -> None:
    """R312-04: 缺省超时只放宽读 (非流式请求的读超时即整次生成的时间), 建连仍短."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=completion(raw("BA-01")))

    await LiveLLMClient(KEY, "gpt-6-luna", transport=httpx.MockTransport(handler)).complete(
        REQUEST, ConfirmationExtraction
    )
    assert (seen[0]["connect"], seen[0]["read"]) == (15.0, 600.0)


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


def unsupported(param: str, code: str = "unsupported_parameter") -> tuple[int, Any]:
    """OpenAI 对不受支持参数的 400 错误体."""
    error = {"message": f"{param} not supported", "type": "invalid_request_error", "param": param, "code": code}
    return 400, {"error": error}


def bodies(server: Server) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in server.requests]


async def test_max_tokens_unsupported_switches_to_max_completion_tokens() -> None:
    """(R59 联调) 推理类模型不接受 max_tokens: 改用 max_completion_tokens 重发, 此后的调用沿用, 不再先试 max_tokens."""
    sleeps: list[float] = []
    server = Server(unsupported("max_tokens"), (200, completion(raw("BA-01"))), (200, completion(raw("BA-01"))))
    llm = client(server, sleeps)
    first = await llm.complete(REQUEST, ConfirmationExtraction)
    second = await llm.complete(REQUEST, ConfirmationExtraction)

    assert isinstance(first.output, ConfirmationExtraction) and isinstance(second.output, ConfirmationExtraction)
    sent = bodies(server)
    assert [("max_tokens" in b, b.get("max_completion_tokens"), b["temperature"]) for b in sent] == [
        (True, None, 0),
        (False, 4096, 0),
        (False, 4096, 0),
    ]
    assert sleeps == []  # 调整后立即重发, 不是退避重试


async def test_temperature_unsupported_is_omitted() -> None:
    """(R59 联调) 模型只支持默认温度: 不再发送 temperature 后重发."""
    server = Server(unsupported("temperature", "unsupported_value"), (200, completion(raw("BA-01"))))
    response = await client(server).complete(REQUEST, ConfirmationExtraction)

    assert isinstance(response.output, ConfirmationExtraction)
    assert [("temperature" in b, b.get("max_tokens")) for b in bodies(server)] == [(True, 4096), (False, 4096)]


async def test_both_parameters_adapted_in_turn() -> None:
    """(R59 联调) 两个参数先后被拒: 依次调整, 第 3 次请求用 max_completion_tokens 且不带 temperature."""
    server = Server(
        unsupported("max_tokens"), unsupported("temperature", "unsupported_value"), (200, completion(raw("BA-01")))
    )
    await client(server).complete(REQUEST, ConfirmationExtraction)

    last = bodies(server)[-1]
    assert len(server.requests) == 3
    assert ("max_tokens" in last, last["max_completion_tokens"], "temperature" in last) == (False, 4096, False)


async def test_adapts_parameters_without_transport_retries() -> None:
    """设置页的测试连接 (max_retries=0) 不做传输重试, 但参数不受支持时照样调整后重发: 推理类模型也能测通."""
    server = Server(unsupported("max_tokens"), (200, completion(raw("BA-01"))))
    response = await client(server, max_retries=0).complete(REQUEST, ConfirmationExtraction)

    assert isinstance(response.output, ConfirmationExtraction)
    assert [b.get("max_completion_tokens") for b in bodies(server)] == [None, 4096]


@pytest.mark.parametrize(
    "reply",
    [
        unsupported("response_format"),  # 不是可调整的参数
        unsupported("max_tokens", "invalid_value"),  # 不是 "不受支持" 的错误码
        (400, {"error": "bad"}),  # 错误体不是 OpenAI 格式
        (422, {"error": {"param": "max_tokens", "code": "unsupported_parameter"}}),  # 不是 400
        *((400, body) for body in ([], "null", False, 42, '"bad request"')),  # (Codex R59 探针) 顶层不是对象
    ],
)
async def test_other_400_not_adapted(reply: tuple[int, Any]) -> None:
    """其余 400 (及非 400 的同样错误体) 不调整参数, 照常抛 LLMTransportError, 只发一次."""
    server = Server(reply)
    with pytest.raises(LLMTransportError, match=f"HTTP {reply[0]}"):
        await client(server).complete(REQUEST, ConfirmationExtraction)
    assert len(server.requests) == 1


async def test_same_parameter_rejected_again_raises() -> None:
    """调整过的参数再次被拒 (改名后仍不支持) 时不再循环, 抛 LLMTransportError."""
    server = Server(unsupported("max_tokens"), unsupported("max_tokens"))
    with pytest.raises(LLMTransportError, match="HTTP 400"):
        await client(server).complete(REQUEST, ConfirmationExtraction)
    assert len(server.requests) == 2


async def test_concurrent_call_already_adapted_still_resends() -> None:
    """(R59 联调) 并发调用: 本次发出的是 max_tokens, 被拒前别的调用已改用 max_completion_tokens; 本次照样按新方式重发,
    不当作 "调整过仍被拒" 而失败 (首次真实联调中 DOC-04 三份合同并发, 曾因此整次失败)."""
    requests: list[dict[str, Any]] = []
    llm: LiveLLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "max_tokens" in body:
            llm.token_param = "max_completion_tokens"  # 模拟并发的另一次调用先完成了调整
            status, error = unsupported("max_tokens")
            return httpx.Response(status, json=error)
        return httpx.Response(200, json=completion(raw("BA-01")))

    llm = LiveLLMClient(KEY, "gpt-6-luna", "https://llm.example.test/v1/", transport=httpx.MockTransport(handler))
    response = await llm.complete(REQUEST, ConfirmationExtraction)

    assert isinstance(response.output, ConfirmationExtraction)
    assert [("max_tokens" in b, b.get("max_completion_tokens")) for b in requests] == [(True, None), (False, 4096)]


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
    patch, _, _ = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
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
    patch, _, _ = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert all(KEY not in json.dumps(e.model_dump(mode="json"), ensure_ascii=False) for e in trail.entries)
    assert KEY not in json.dumps([r.model_dump(mode="json") for r in patch["procedure_runs"]])


# ── 配置 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("changes", "message"),
    [({"LLM_API_KEY": ""}, "缺密钥"), ({"LLM_MODEL": ""}, "缺模型名")],
)
async def test_live_without_key_or_model_starts_and_fails_on_call(changes: dict[str, str], message: str) -> None:
    """live 模式 .env 缺密钥或模型名时应用照常启动 (设置页要应用跑起来才能用), 调用模型时才报错, 提示到设置页配置.
    推翻原验收 1 "启动即失败"."""
    settings = Settings(LLM_MODE="live", LLM_MODEL="deepseek-chat", LLM_API_KEY=KEY).model_copy(update=changes)
    unconfigured = make_llm_client(settings)
    assert isinstance(unconfigured, UnconfiguredLLMClient)
    with pytest.raises(LLMTransportError, match=f"{message}.*设置页"):
        await unconfigured.complete(REQUEST, ConfirmationExtraction)


def test_make_llm_client_by_mode() -> None:
    """LLM_BASE_URL 为空时用 OpenAI 默认地址; 显式传空, 不受本机 .env 影响."""
    live = make_llm_client(Settings(LLM_MODE="live", LLM_MODEL="deepseek-chat", LLM_API_KEY=KEY, LLM_BASE_URL=""))
    assert isinstance(live, LiveLLMClient) and live.url == f"{DEFAULT_BASE_URL}/chat/completions"
    assert isinstance(make_llm_client(Settings(LLM_MODE="mock")), MockLLMClient)


def test_make_llm_client_reads_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_BASE_URL 从环境变量与 .env 读取 (配置类未定义该键时会被忽略), 末尾斜杠不影响拼接."""
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1/")
    live = make_llm_client(Settings(LLM_MODE="live", LLM_MODEL="deepseek-chat", LLM_API_KEY=KEY))
    assert isinstance(live, LiveLLMClient) and live.url == "https://api.deepseek.com/v1/chat/completions"


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
@pytest.mark.live(max_calls=6)  # 两份回函, 每份最多 3 次尝试 (需求 R115 的预算)
async def test_live_doc06_extracts_ba01_reply(live_budget: Budget) -> None:
    """验收 4 (联网): 配好的真实接口 (环境变量或 .env, tests.external.live_llm_settings) 读 BA-01 回函, 得回函余额
    120840000, 截至 2026-06-30. 调用计入预算, 超限即失败; 结束时写用量汇总."""
    settings = live_llm_settings()
    agent, ctx, trail = setup(llm=BudgetedLLMClient(make_llm_client(settings), live_budget))
    patch = await agent.run(ctx)
    report("test_live_doc06_extracts_ba01_reply", trail.entries, live_budget)
    live_budget.check()
    ba01 = next(c for c in patch["confirmations"] if c.bank_account_id == "BA-01")
    assert (ba01.confirmed_balance_cents, ba01.balance_date) == (120840000, date(2026, 6, 30))
    assert all(settings.LLM_API_KEY not in json.dumps(e.model_dump(mode="json")) for e in trail.entries)


# ── 服务商差异, 无密钥, 脱敏过滤器与热切换 (R6) ─────────────


async def test_anthropic_omits_temperature_and_json_mode() -> None:
    """Anthropic 的兼容接口: Claude 4.7 起 temperature 小于 1 返回 400, response_format 被忽略, 两者都不发."""
    server = Server((200, completion(raw("BA-01"), model="claude-opus-5-5")))
    await client(server, provider="anthropic").complete(REQUEST, ConfirmationExtraction)
    body = json.loads(server.requests[0].content)
    assert "temperature" not in body and "response_format" not in body
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"


async def test_ollama_needs_no_key_and_sends_no_authorization() -> None:
    server = Server((200, completion(raw("BA-01"), model="llama3")))
    live = LiveLLMClient(
        "", "llama3", "http://localhost:11434/v1", provider="ollama", transport=httpx.MockTransport(server.handler)
    )
    response = await live.complete(REQUEST, ConfirmationExtraction)
    assert "authorization" not in server.requests[0].headers
    assert response.output is not None  # 空密钥不会把每个响应都当成 "回显了密钥"
    with pytest.raises(ValueError, match="缺密钥"):
        LiveLLMClient("", "gpt-5", provider="openai")
    assert isinstance(make_live_client("deepseek", "", "deepseek-chat", ""), UnconfiguredLLMClient)


def test_one_redacting_filter_per_logger_holds_all_keys() -> None:
    """每个日志器只挂一个过滤器, 热切换过的密钥都登记在上面; 空串不登记 ("" in 任何文本都为真)."""
    install_redaction("sk-first-0000")
    install_redaction("sk-second-1111")
    install_redaction("")
    for name in HTTP_LOGGERS:
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, RedactingFilter)]
        assert len(filters) == 1 and {"sk-first-0000", "sk-second-1111"} <= filters[0].secrets
        assert "" not in filters[0].secrets
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "a sk-first-0000 b sk-second-1111", None, None)
    filters[0].filter(record)
    assert record.getMessage() == "a *** b ***"


async def test_switchable_client_uses_new_inner_from_next_call() -> None:
    first, second = (
        Server((200, completion(raw("BA-01"), model="m-1"))),
        Server((200, completion(raw("BA-01"), model="m-2"))),
    )
    switchable = SwitchableLLMClient(client(first), "m-1")
    assert (await switchable.complete(REQUEST, ConfirmationExtraction)).model == "m-1"
    switchable.reload(client(second), "m-2")
    assert (await switchable.complete(REQUEST, ConfirmationExtraction)).model == "m-2"
    assert switchable.model_name == "m-2" and len(first.requests) == len(second.requests) == 1


async def test_no_retry_when_max_retries_is_zero() -> None:
    server = Server((503, "busy"))
    with pytest.raises(LLMTransportError) as e:
        await client(server, max_retries=0).complete(REQUEST, ConfirmationExtraction)
    assert len(server.requests) == 1 and str(e.value).startswith("HTTP 503")
