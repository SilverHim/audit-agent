"""审计助理 (A10) 的对话模型: OpenAI 兼容 Chat Completions 的流式工具调用.

与 adapters/llm.py 的 LiveLLMClient (审计程序用, 一次返回 JSON) 分开: 这里是 stream: true, 带 tools, 输出纯文本.
服务商对参数的支持沿用 llm.PROVIDERS. 模型配置取设置页的主模型 (LlmSettingsService.config("primary")),
每次提问重新取, 设置页保存后下一次提问即生效.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from backend.adapters.llm import (
    DEFAULT_BASE_URL,
    ERROR_TEXT_LIMIT,
    MAX_RETRIES,
    PROVIDERS,
    LLMTransportError,
    install_redaction,
    retryable,
)
from backend.contracts.conversation import AssistantTurn, ChatEvent, ChatMessage, TextDelta, ToolCall, ToolSpec
from backend.contracts.models import LlmProvider
from backend.contracts.ports import ChatModel

# 上下文超限的错误文本 (各家措辞不同, 取常见的几种); 命中时抛 ContextOverflow, 由调用方压缩历史后重试
OVERFLOW_PATTERNS = re.compile(
    r"context[_ ]length[_ ]exceeded|maximum context length|context window|prompt is too long|too many tokens"
    r"|input is too long|exceeds? the (?:model's )?(?:maximum )?context",
    re.IGNORECASE,
)
# 400 指明有问题时可调整的参数 (见 LiveChatModel.adapt): max_tokens 改用 max_completion_tokens, temperature 不再发,
# reasoning_effort 设为 none (如 OpenAI gpt-6 系列在 Chat Completions 里带工具时要求如此)
ADAPTABLE = frozenset({"max_tokens", "temperature", "reasoning_effort"})


class SecretEcho(LLMTransportError):
    """模型输出里出现了密钥: 已中止, 含密钥的片段一个字都没有外发."""


class ContextOverflow(LLMTransportError):
    """服务商报上下文超限."""


class StreamBroken(LLMTransportError):
    """已经开始输出之后连接出错或响应不完整: 不重试 (已发出的片段收不回)."""


def wire_message(message: ChatMessage) -> dict[str, Any]:
    """消息转成 Chat Completions 的格式."""
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in message.tool_calls
        ]
        if not message.content:
            wire["content"] = None
    return wire


def wire_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
    }


@dataclass
class PartialCall:
    """流式拼接中的一个工具调用: 参数按片段累加."""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class Holdback:
    """密钥回显防护: 累计全文, 只放出末尾 len(secret)-1 个字之前的部分; 全文一出现密钥即抛 SecretEcho.

    密钥若从某处开始出现, 在它整段到齐之前, 起点之后的字都还压着; 整段到齐时检查即命中. 所以含密钥的任何片段都不会外发.
    """

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.keep = max(len(secret) - 1, 0)
        self.text = ""  # 累计全文
        self.sent = 0  # 已放出的字数

    def push(self, delta: str) -> str:
        """收一段, 返回可以放出的部分 (可能为空)."""
        self.text += delta
        if self.secret and self.secret in self.text:
            raise SecretEcho("模型输出中回显了密钥, 已中止 (未外发, 不进入留痕)")
        upto = max(len(self.text) - self.keep, self.sent)
        out, self.sent = self.text[self.sent : upto], upto
        return out

    def flush(self) -> str:
        """输出结束: 放出余下部分."""
        out, self.sent = self.text[self.sent :], len(self.text)
        return out


class LiveChatModel:
    """OpenAI 兼容的流式对话模型: POST {base_url}/chat/completions, stream: true, 带 tools.

    - 工具调用按 index 拼接参数片段; 服务商没给调用 ID 时补 call_{index}.
    - 408, 429, 5xx 与超时按指数退避重试, 但只在产出第一个事件之前; 之后出错抛 StreamBroken, 不重试.
    - 400 的错误体指明 max_tokens, temperature 或 reasoning_effort 有问题时按 ADAPTABLE 调整, 立即重发
      (不算重试; 前两项同 R59 对 LiveLLMClient 的处理).
    - 其余 400 等不可重试的状态码抛 LLMTransportError; 错误文本命中上下文超限时抛 ContextOverflow.
    - 密钥: 只放在请求头; 回答文本经 Holdback 扣住末尾, 出现密钥即抛 SecretEcho; 工具调用参数结束时检查;
      错误信息先脱敏再截断; 日志挂脱敏过滤 (install_redaction). 没有密钥 (本地 Ollama) 时不做这些检查.
    - temperature (固定 0) 与 stream_options.include_usage 按服务商的支持决定发不发 (llm.PROVIDERS).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        provider: LlmProvider = "openai",
        timeout: float = 120.0,
        backoff: float = 1.0,
        max_retries: int = MAX_RETRIES,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        adjusted: set[str] | None = None,
    ) -> None:
        """model 为空, 或服务商要密钥而 api_key 为空时抛 ValueError (配置错误).

        adjusted: 已为这个模型调整过的参数 (见 adapt); LiveChatSource 按 (地址, 模型名) 共享一份, 不给时自建.
        """
        if not model:
            raise ValueError("缺模型名 (设置页的模型名, 或 .env 的 LLM_MODEL)")
        if not api_key and PROVIDERS[provider].requires_key:
            raise ValueError("缺密钥 (设置页的密钥, 或 .env 的 LLM_API_KEY)")
        self._api_key = api_key  # 只用于请求头与脱敏
        self.model = model  # 请求的模型名
        self.provider = provider  # 服务商, 决定发哪些参数
        self.url = base_url.rstrip("/") + "/chat/completions"  # 接口地址
        self.timeout = timeout  # 单次请求超时, 秒
        self.backoff = backoff  # 退避基数, 秒
        self.max_retries = max_retries  # 首个事件之前最多重试次数
        self._transport = transport
        self._sleep = sleep
        self.adjusted = adjusted if adjusted is not None else set()  # 已调整的参数, 取值见 ADAPTABLE
        install_redaction(api_key)

    def body(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec], tool_choice: str, max_tokens: int
    ) -> dict[str, Any]:
        """请求体. 有工具时总是带上 tools (tool_choice=none 时也带: 消息里已有工具调用, 有的服务商要求有 tools)."""
        traits = PROVIDERS[self.provider]
        token_field = "max_completion_tokens" if "max_tokens" in self.adjusted else "max_tokens"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [wire_message(m) for m in messages],
            token_field: max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = [wire_tool(t) for t in tools]
            body["tool_choice"] = tool_choice
        if traits.temperature and "temperature" not in self.adjusted:
            body["temperature"] = 0
        if "reasoning_effort" in self.adjusted:
            body["reasoning_effort"] = "none"
        if traits.stream_usage:
            body["stream_options"] = {"include_usage": True}
        return body

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        max_tokens: int,
    ) -> AsyncIterator[ChatEvent]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        retries = 0
        while True:
            body = self.body(messages, tools, tool_choice, max_tokens)
            emitted = False
            failure = ""  # 可重试的失败; 为空表示换了参数立即重发
            try:
                async with (
                    httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client,
                    client.stream("POST", self.url, json=body, headers=headers) as response,
                ):
                    if response.status_code != 200:
                        text = (await response.aread()).decode("utf-8", "replace")
                        if response.status_code == 400 and self.adapt(text):
                            continue
                        detail = self._redact(f"{response.reason_phrase}: {text}")[:ERROR_TEXT_LIMIT]
                        failure = f"HTTP {response.status_code} {detail}"
                        if OVERFLOW_PATTERNS.search(text):
                            raise ContextOverflow(failure)
                        if not retryable(response.status_code):
                            raise LLMTransportError(failure)
                    else:
                        async for event in self._events(response):
                            emitted = True
                            yield event
                        return
            except LLMTransportError:
                raise
            except httpx.TimeoutException as e:
                if emitted:
                    raise StreamBroken(f"输出中途超时 ({type(e).__name__})") from None
                failure = f"超时 ({type(e).__name__})"
            except httpx.TransportError as e:
                message = self._redact(f"{type(e).__name__}: {e}")
                if emitted:
                    raise StreamBroken(f"输出中途连接出错: {message}") from None
                raise LLMTransportError(f"连接失败: {message}") from None
            if retries >= self.max_retries:
                raise LLMTransportError(f"重试 {retries} 次后仍失败: {failure}" if retries else failure)
            retries += 1
            await self._sleep(self.backoff * 2 ** (retries - 1))

    def adapt(self, text: str) -> bool:
        """400 的错误体 (error.param) 指明 ADAPTABLE 中的参数有问题 (推理类模型, 如 OpenAI gpt-6 系列) 时调整请求方式.
        已调整过的参数不再调整, 所以最多多发 len(ADAPTABLE) 次, 不会循环. 调整记在共享的 adjusted 里, 同一来源此后的
        模型直接按调整后的方式请求. 不是可调整的错误返回假."""
        try:
            error = json.loads(text).get("error") or {}
        except (ValueError, AttributeError):
            return False
        param = error.get("param") if isinstance(error, dict) else None
        if param in ADAPTABLE and param not in self.adjusted:
            self.adjusted.add(param)
            return True
        return False

    async def _events(self, response: httpx.Response) -> AsyncIterator[ChatEvent]:
        """解析 SSE: 每个 data 行一个 JSON 分块, [DONE] 结束. 回答文本经 Holdback 放出, 结束时给 AssistantTurn."""
        guard = Holdback(self._api_key)
        calls: dict[int, PartialCall] = {}
        finish: str | None = None
        model = self.model
        tokens = (0, 0)
        done = False
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                raise StreamBroken("响应分块不是 JSON") from None
            if not isinstance(chunk, dict):
                raise StreamBroken(f"响应分块不是 JSON 对象 ({type(chunk).__name__})")
            if chunk.get("error"):
                detail = json.dumps(chunk["error"], ensure_ascii=False)
                if OVERFLOW_PATTERNS.search(detail):
                    raise ContextOverflow(self._redact(detail)[:ERROR_TEXT_LIMIT])
                raise StreamBroken(self._redact(f"服务商返回错误: {detail}")[:ERROR_TEXT_LIMIT])
            if isinstance(chunk.get("model"), str) and chunk["model"]:
                model = chunk["model"]
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                tokens = (count(usage.get("prompt_tokens")), count(usage.get("completion_tokens")))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    out = guard.push(content)
                    if out:
                        yield TextDelta(text=out)
                for item in delta.get("tool_calls") or []:
                    partial = calls.setdefault(int(item.get("index", 0)), PartialCall())
                    partial.id = item.get("id") or partial.id
                    function = item.get("function") or {}
                    partial.name = function.get("name") or partial.name
                    if function.get("arguments"):
                        partial.arguments.append(function["arguments"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        if not done and finish is None:
            raise StreamBroken("响应在结束之前断开")
        rest = guard.flush()
        if rest:
            yield TextDelta(text=rest)
        tool_calls = [
            ToolCall(id=p.id or f"call_{i}", name=p.name, arguments="".join(p.arguments))
            for i, p in sorted(calls.items())
        ]
        # 回答文本之外, 会外发 (tool 事件) 或写进留痕的模型字段: 工具调用的 ID, 名称, 参数, 模型名, 结束原因
        fields = [model, finish or "", *(part for c in tool_calls for part in (c.id, c.name, c.arguments))]
        if self._api_key and any(self._api_key in value for value in fields):
            raise SecretEcho("模型的工具调用或响应字段中回显了密钥, 已中止")
        yield AssistantTurn(
            text=guard.text,
            tool_calls=tool_calls,
            finish_reason=finish,
            model=model,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
        )

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text


def count(value: Any) -> int:
    """用量里的 token 数: 不是非负整数 (缺失, null, 浮点, bool) 时记 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class UnconfiguredChatModel:
    """live 模式还没有可用配置时的占位: 调用即报错, 提示到设置页配置."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        max_tokens: int,
    ) -> AsyncIterator[ChatEvent]:
        raise LLMTransportError(f"未配置大模型 ({self.reason}), 请在设置页配置模型与密钥")
        yield  # pragma: no cover  (使之成为异步生成器)


ModelConfig = tuple[LlmProvider, str, str, str]  # (服务商, 接口地址, 模型名, 明文密钥), 同 LlmSettingsService.config


class LiveChatSource:
    """每次提问按设置页主模型的生效配置建 LiveChatModel. config: 取生效配置的函数, 如 LlmSettingsService 的
    lambda: service.config("primary"); 主密钥不可用时它抛 SecretKeyError, 原样上抛."""

    def __init__(
        self, config: Callable[[], Awaitable[ModelConfig]], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport  # 测试时换成假 HTTP 传输
        self.adjusted: dict[tuple[str, str], set[str]] = {}  # (接口地址, 模型名) -> 被拒的参数, 各轮共用

    async def current(self) -> ChatModel:
        provider, base_url, model, key = await self.config()
        url = base_url or DEFAULT_BASE_URL
        adjusted = self.adjusted.setdefault((url, model), set())
        try:
            return LiveChatModel(key, model, url, provider=provider, transport=self.transport, adjusted=adjusted)
        except ValueError as e:
            return UnconfiguredChatModel(str(e))
