"""LLM 客户端 (A 负责). mock 模式读 fixtures, live 模式接 OpenAI 兼容接口.

live 按 Q3 可切换 OpenAI, DeepSeek, 通义, 本地 Ollama 等, 由 LLM_BASE_URL 指定.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from backend.contracts.ports import LLMClient, LLMRequest, LLMResponse, T_out
from backend.core.config import Settings

DEFAULT_BASE_URL = "https://api.openai.com/v1"  # 未配置 LLM_BASE_URL 时的接口地址
MAX_RETRIES = 3  # 传输层最多重试次数, 不占校验重试 (JudgmentEngine.call_llm)
ERROR_TEXT_LIMIT = 500  # 异常信息中服务端错误正文的最大长度 (先脱敏再截断)
# 可能写出服务端回显内容 (如 reason phrase, 响应头) 的日志器, 挂脱敏过滤
HTTP_LOGGERS = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy")


def retryable(status: int) -> bool:
    """可退避重试的状态码: 408 超时, 429 限流, 全部 5xx."""
    return status in (408, 429) or 500 <= status < 600


class RedactingFilter(logging.Filter):
    """日志过滤: 消息中出现密钥时替换为 ***. 挂在 HTTP_LOGGERS 上, 防止服务端回显的密钥写进日志."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret  # 要脱敏的密钥

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.secret in message:
            record.msg, record.args = message.replace(self.secret, "***"), ()
        return True


def install_redaction(secret: str) -> None:
    """给 HTTP_LOGGERS 各挂一个该密钥的 RedactingFilter (已挂过的不重复挂)."""
    for name in HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) and f.secret == secret for f in logger.filters):
            logger.addFilter(RedactingFilter(secret))


class MockLLMClient:
    """读 {fixtures}/{procedure_id}/{fixture_key}.json, 找不到读 default.json."""

    def __init__(self, fixtures_root: Path) -> None:
        self.root = fixtures_root

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        folder = self.root / req.procedure_id
        path = next((p for p in (folder / f"{req.fixture_key}.json", folder / "default.json") if p.exists()), None)
        if path is None:
            return LLMResponse[output_model](  # type: ignore[valid-type]
                output=None, raw="", model="mock", input_tokens=0, output_tokens=0, error="fixture_not_found"
            )
        raw = path.read_text(encoding="utf-8")
        try:
            output: BaseModel | None = output_model.model_validate_json(raw)
            error = None
        except ValidationError as e:
            output, error = None, str(e)
        return LLMResponse[output_model](  # type: ignore[valid-type]
            output=output, raw=raw, model="mock", input_tokens=0, output_tokens=0, error=error
        )


class LLMTransportError(RuntimeError):
    """传输层失败: 不可重试的状态码, 重试耗尽, 响应不是 Chat Completions 的格式, 或响应回显了密钥. 消息不含密钥."""


def contains_secret(value: Any, secret: str) -> bool:
    """JSON 解码后的值 (含嵌套的键与值) 中是否有字符串包含 secret. 用来拦下经 \\uXXXX 转义回显的密钥."""
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(contains_secret(k, secret) or contains_secret(v, secret) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_secret(v, secret) for v in value)
    return False


def strip_fences(raw: str) -> str:
    """去掉模型偶尔包在 JSON 外的 Markdown 代码块 (```json ... ```); 没有时原样返回."""
    text = raw.strip()
    if not text.startswith("```"):
        return raw
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


class LiveLLMClient:
    """OpenAI 兼容的 Chat Completions 接口: POST {base_url}/chat/completions.

    system 作首条 system 消息, 其后为 req.messages; temperature, max_tokens 透传; 要求 JSON 输出
    (response_format json_object, 各家兼容接口普遍支持; 输出模型的 JSON schema 由调用方写在 system 中,
    见 JudgmentEngine.call_llm). 408, 429, 全部 5xx 与超时按指数退避至多重试 MAX_RETRIES 次; 其余失败状态码
    (如 400, 401) 与连接失败不重试. 不可重试或重试耗尽时抛 LLMTransportError.

    密钥只放在请求头. 成功响应中出现密钥 (服务端回显) 时整体拒绝, 抛 LLMTransportError, 不让它进入 raw, 校验错误与留痕;
    错误信息先脱敏再截断; httpx, httpcore 的日志挂脱敏过滤 (install_redaction).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 120.0,
        backoff: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """api_key, model 为空抛 ValueError (live 模式的配置错误, 应用启动即失败).

        backoff: 第 n 次重试前等待 backoff × 2^(n-1) 秒; transport, sleep 供测试替换 (假 HTTP 传输, 不真等待).
        """
        if not api_key:
            raise ValueError("LLM_MODE=live 须配置 LLM_API_KEY")
        if not model:
            raise ValueError("LLM_MODE=live 须配置 LLM_MODEL")
        self._api_key = api_key  # 只用于请求头与错误信息脱敏
        self.model = model  # 请求的模型名
        self.url = base_url.rstrip("/") + "/chat/completions"  # 接口地址
        self.timeout = timeout  # 单次请求超时, 秒
        self.backoff = backoff  # 退避基数, 秒
        self._transport = transport
        self._sleep = sleep
        install_redaction(api_key)

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        """调用一次 (含传输层重试). 输出按 output_model 解析: 失败时 output 为空, raw 保留原文, error 为解析错误;
        model 与 token 数取响应."""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": req.system},
                *({"role": m.role, "content": m.content} for m in req.messages),
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "response_format": {"type": "json_object"},
        }
        raw, model, input_tokens, output_tokens = self._envelope(await self._post(body))
        try:
            decoded = json.loads(strip_fences(raw))
        except ValueError:
            decoded = None
        if contains_secret(decoded, self._api_key):  # 模型内容本身是 JSON, 其中转义的密钥解析后才出现
            raise LLMTransportError("模型输出中回显了密钥, 已拒绝 (不进入输出与留痕)")
        try:
            output: BaseModel | None = output_model.model_validate_json(strip_fences(raw))
            error = None
        except ValidationError as e:
            output, error = None, str(e)
        return LLMResponse[output_model](  # type: ignore[valid-type]
            output=output, raw=raw, model=model, input_tokens=input_tokens, output_tokens=output_tokens, error=error
        )

    def _envelope(self, data: Any) -> tuple[str, str, int, int]:
        """Chat Completions 响应的 (content, model, prompt_tokens, completion_tokens).

        只对缺失或 null 用默认值: content 取空串, model 取请求的模型名 (空串同样), usage 与 token 数取 0;
        给了值就须类型正确 (content, model 为字符串, usage 为对象, token 数为非负整数, 不接受 bool, 浮点),
        否则抛 LLMTransportError (信息只含类型名, 不含字段值).
        """
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMTransportError("响应不是 Chat Completions 格式: 缺 choices[0].message.content") from None
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise LLMTransportError(f"响应的 content 不是字符串 ({type(content).__name__})")
        model = data.get("model")
        if model is not None and not isinstance(model, str):
            raise LLMTransportError(f"响应的 model 不是字符串 ({type(model).__name__})")
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise LLMTransportError(f"响应的 usage 不是对象 ({type(usage).__name__})")
        tokens = []
        for name in ("prompt_tokens", "completion_tokens"):
            value = usage.get(name)
            if value is None:
                value = 0
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LLMTransportError(f"响应的 usage.{name} 不是非负整数 ({type(value).__name__})")
            tokens.append(value)
        return content, model or self.model, tokens[0], tokens[1]

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """发请求, 可重试的失败按指数退避重试. 返回响应 JSON."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        failure = ""
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                await self._sleep(self.backoff * 2 ** (attempt - 1))
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                    response = await client.post(self.url, json=body, headers=headers)
            except httpx.TimeoutException as e:
                failure = f"超时 ({type(e).__name__})"
                continue
            except httpx.TransportError as e:
                raise LLMTransportError(self._redact(f"连接失败: {type(e).__name__}: {e}")) from None
            if response.status_code == 200:
                if self._api_key in response.text:
                    raise LLMTransportError("响应中回显了密钥, 已拒绝 (不进入输出与留痕)")
                try:
                    data: dict[str, Any] = response.json()
                except ValueError:
                    raise LLMTransportError("响应不是 JSON") from None
                if contains_secret(data, self._api_key):  # 经 \uXXXX 转义的回显, 解码后才出现
                    raise LLMTransportError("响应中回显了密钥, 已拒绝 (不进入输出与留痕)")
                if not isinstance(data, dict):
                    raise LLMTransportError(f"响应不是 JSON 对象 ({type(data).__name__})")
                return data
            detail = self._redact(f"{response.reason_phrase}: {response.text}")[:ERROR_TEXT_LIMIT]
            failure = f"HTTP {response.status_code} {detail}"
            if not retryable(response.status_code):
                raise LLMTransportError(failure)
        raise LLMTransportError(f"重试 {MAX_RETRIES} 次后仍失败: {failure}")

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***")


def llm_version(settings: Settings) -> dict[str, str]:
    """项目启动时冻结进 version_snapshot 的 LLM 信息: 模式与模型名 (mock 模式为 "mock")."""
    return {"llm_mode": settings.LLM_MODE, "model": "mock" if settings.LLM_MODE == "mock" else settings.LLM_MODEL}


def make_llm_client(settings: Settings) -> LLMClient:
    """按 LLM_MODE 建客户端. live 模式密钥或模型名为空时抛 ValueError (应用启动即失败).

    接口地址取配置 LLM_BASE_URL, 为空时为 DEFAULT_BASE_URL.
    """
    if settings.LLM_MODE == "mock":
        return MockLLMClient(settings.LLM_FIXTURES_ROOT)
    base_url = settings.LLM_BASE_URL or DEFAULT_BASE_URL
    return LiveLLMClient(settings.LLM_API_KEY, settings.LLM_MODEL, base_url)
