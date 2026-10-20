"""LLM 客户端. mock 模式读 fixtures, live 模式接 OpenAI 兼容接口.

live 可接 Anthropic, OpenAI, DeepSeek, 通义, 本地 Ollama 等: 设置页按服务商保存配置, 未保存时取 .env 的
LLM_BASE_URL, LLM_MODEL, LLM_API_KEY. 各家对请求参数的支持不同, 见 PROVIDERS. 组装根注入的是 SwitchableLLMClient,
保存设置后热切换, 不用重启.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from backend.contracts.models import LlmProvider
from backend.contracts.ports import EngagementStore, LLMClient, LLMRequest, LLMResponse, T_out
from backend.core.config import Settings

DEFAULT_BASE_URL = "https://api.openai.com/v1"  # 未配置 LLM_BASE_URL 时的接口地址
MAX_RETRIES = 3  # 传输层最多重试次数, 不占校验重试 (JudgmentEngine.call_llm)
# 单次请求的超时, 秒. 不是流式请求, 模型写完之前收不到字节, 读超时即整次生成的时间; 按缺省输出上限 32768 token
# 放宽 (原为 120 秒), 每秒不到约 55 token 才会超时 (需求 R312)
TIMEOUT = 600.0
ERROR_TEXT_LIMIT = 500  # 异常信息中服务端错误正文的最大长度 (先脱敏再截断)
# 输出在上限处被截断时 LLMResponse.error 的写法 (需求 R312): 经语法级错误回传给模型重试, 用英文
TRUNCATED = (
    "The reply was cut off at the output token limit ({limit} tokens) before the JSON object was complete. "
    "Reply again with a complete JSON object and keep it concise."
)
# 服务端因输出达到上限回 400 时 error.message 里的字样, 按小写比 (OpenAI 在 JSON 模式下输出不完整时不给半截内容,
# 回 400, param 与 code 都为空; 需求 R312)
OUTPUT_LIMIT_MESSAGE = "output limit was reached"
# 可能写出服务端回显内容 (如 reason phrase, 响应头) 的日志器, 挂脱敏过滤
HTTP_LOGGERS = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy")


@dataclass(frozen=True)
class ProviderTraits:
    """一家服务商的 OpenAI 兼容接口对请求参数的支持."""

    requires_key: bool  # 必须有密钥 (本地 Ollama 不要); 有密钥就发 Authorization: Bearer
    json_mode: bool  # 发 response_format json_object (不支持的服务商靠系统提示词要求 JSON)
    temperature: bool  # 发 temperature
    stream_usage: bool  # 流式请求发 stream_options.include_usage 取 token 用量


# Anthropic 一行按官方文档 (platform.claude.com/docs/en/api/openai-sdk, 2026-10-17 核对): response_format 被忽略,
# stream_options 完全支持, Claude 4.7 及之后的模型 temperature 小于 1 即返回 400. 其余服务商的 stream_options
# 尚未核对官方文档, 先不发, 流式时 token 记 0.
PROVIDERS: dict[LlmProvider, ProviderTraits] = {
    "anthropic": ProviderTraits(requires_key=True, json_mode=False, temperature=False, stream_usage=True),
    "openai": ProviderTraits(requires_key=True, json_mode=True, temperature=True, stream_usage=True),
    "deepseek": ProviderTraits(requires_key=True, json_mode=True, temperature=True, stream_usage=False),
    "qwen": ProviderTraits(requires_key=True, json_mode=True, temperature=True, stream_usage=False),
    "custom": ProviderTraits(requires_key=True, json_mode=True, temperature=True, stream_usage=False),
    "ollama": ProviderTraits(requires_key=False, json_mode=True, temperature=True, stream_usage=False),
}


def retryable(status: int) -> bool:
    """可退避重试的状态码: 408 超时, 429 限流, 全部 5xx."""
    return status in (408, 429) or 500 <= status < 600


class RedactingFilter(logging.Filter):
    """日志过滤: 消息中出现任一已登记的密钥时替换为 ***. 挂在 HTTP_LOGGERS 上, 防止服务端回显的密钥写进日志."""

    def __init__(self) -> None:
        super().__init__()
        self.secrets: set[str] = set()  # 要脱敏的密钥: 用过的都留着 (热切换前的请求可能还在写日志)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        hits = [secret for secret in self.secrets if secret in message]
        if hits:
            for secret in sorted(hits, key=len, reverse=True):
                message = message.replace(secret, "***")
            record.msg, record.args = message, ()
        return True


def install_redaction(secret: str) -> None:
    """把密钥登记到 HTTP_LOGGERS 各自唯一的 RedactingFilter 上 (没有就挂一个). 空串不登记: "" in 任何文本都为真."""
    if not secret:
        return
    for name in HTTP_LOGGERS:
        logger = logging.getLogger(name)
        found = next((f for f in logger.filters if isinstance(f, RedactingFilter)), None)
        if found is None:
            found = RedactingFilter()
            logger.addFilter(found)
        found.secrets.add(secret)


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


XERO_PREFIX = "xero/"  # Xero 数据集名的前缀 (同 XeroDatasetLoader.PREFIX), 其后为快照名


class DatasetFixtureLLMClient(MockLLMClient):
    """mock 模式的客户端: 按请求所属项目 (LLMRequest.eid) 的数据集选 fixture 目录, 目录内读法同 MockLLMClient.

    数据集为 xero/<快照> 且 dataset_root/xero_demo/<快照>/llm 存在时, 读该目录 (生成脚本从 tests/fixtures/llm_xero
    复制的运行副本); 其余 (demo, 没有补造 llm/ 的 Xero 快照, 没有数据集的新客户, eid 为空, 查不到项目) 读 fixtures_root
    (LLM_FIXTURES_ROOT). engagements 为空时总读 fixtures_root.
    """

    def __init__(self, fixtures_root: Path, dataset_root: Path, engagements: EngagementStore | None = None) -> None:
        super().__init__(fixtures_root)  # root: 非 Xero 数据集的 fixture 目录
        self.dataset_root = dataset_root  # 合成数据集目录 (DATASET_ROOT), Xero 的补造目录在其 xero_demo/ 下
        self.engagements = engagements  # 按 eid 取项目的数据集

    async def directory(self, eid: str) -> Path:
        """该项目读的 fixture 目录 (见类说明)."""
        if not eid or self.engagements is None:
            return self.root
        engagement = await self.engagements.get(eid)
        if engagement is None or engagement.dataset is None or not engagement.dataset.startswith(XERO_PREFIX):
            return self.root
        snapshot = engagement.dataset.removeprefix(XERO_PREFIX)
        if not snapshot or "/" in snapshot or snapshot in (".", ".."):  # 快照名只能是一段目录名
            return self.root
        llm = self.dataset_root / "xero_demo" / snapshot / "llm"
        return llm if llm.is_dir() else self.root

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        directory = await self.directory(req.eid)
        if directory == self.root:
            return await super().complete(req, output_model)
        return await MockLLMClient(directory).complete(req, output_model)


class LLMTransportError(RuntimeError):
    """传输层失败: 不可重试的状态码, 重试耗尽, 响应不是 Chat Completions 的格式, 或响应回显了密钥. 消息不含密钥."""


class OutputLimitReached(LLMTransportError):
    """HTTP 400 且错误体说输出达到上限 (见 OUTPUT_LIMIT_MESSAGE). LiveLLMClient.complete 转成截断的响应, 不外抛."""


class UnsupportedParameter(LLMTransportError):
    """HTTP 400 且错误体指明某个请求参数不受该模型支持 (OpenAI 错误体的 error.param 与 error.code)."""

    def __init__(self, message: str, param: str) -> None:
        super().__init__(message)
        self.param = param  # 不受支持的参数名


def contains_secret(value: Any, secret: str) -> bool:
    """JSON 解码后的值 (含嵌套的键与值) 中是否有字符串包含 secret. 用来拦下经 \\uXXXX 转义回显的密钥.
    secret 为空 (本地 Ollama 不用密钥) 时为假."""
    if not secret:
        return False
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

    system 作首条 system 消息, 其后为 req.messages; max_tokens 透传; temperature 与 JSON 模式 (response_format
    json_object) 按服务商的支持决定发不发 (PROVIDERS); 输出模型的 JSON schema 由调用方写在 system 中,
    见 JudgmentEngine.call_llm. 推理类模型 (如 OpenAI gpt-6 系列) 不接受 max_tokens 与非默认 temperature: 收到
    400 且错误体指明 max_tokens 不受支持时改用 max_completion_tokens, 指明 temperature 取值不受支持时不再发送
    temperature (用模型默认), 随即重发, 不计传输重试; 调整对本客户端此后的调用一直有效.
    输出在上限处被截断 (finish_reason 为 length, 或服务端因输出达到上限回 400) 时不抛错, error 为 TRUNCATED, 交给
    校验按语法失败回传给模型重试 (需求 R312); finish_reason 为 length 时 raw 照常保留, 400 时为空.
    408, 429, 全部 5xx 与超时按指数退避至多重试 max_retries 次; 其余失败状态码 (如 400, 401) 与连接失败不重试.
    不可重试或重试耗尽时抛 LLMTransportError.

    密钥只放在请求头. 成功响应中出现密钥 (服务端回显) 时整体拒绝, 抛 LLMTransportError, 不让它进入 raw, 校验错误与留痕;
    错误信息先脱敏再截断; httpx, httpcore 的日志挂脱敏过滤 (install_redaction). 没有密钥 (本地 Ollama) 时不发
    Authorization, 也不做这些检查.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        provider: LlmProvider = "openai",
        timeout: float = TIMEOUT,
        backoff: float = 1.0,
        max_retries: int = MAX_RETRIES,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """model 为空, 或服务商要密钥而 api_key 为空时抛 ValueError (配置错误).

        backoff: 第 n 次重试前等待 backoff × 2^(n-1) 秒; transport, sleep 供测试替换 (假 HTTP 传输, 不真等待).
        """
        if not model:
            raise ValueError("缺模型名 (设置页的模型名, 或 .env 的 LLM_MODEL)")
        if not api_key and PROVIDERS[provider].requires_key:
            raise ValueError("缺密钥 (设置页的密钥, 或 .env 的 LLM_API_KEY)")
        self._api_key = api_key  # 只用于请求头与错误信息脱敏
        self.model = model  # 请求的模型名
        self.provider = provider  # 服务商, 决定请求里发哪些参数
        self.url = base_url.rstrip("/") + "/chat/completions"  # 接口地址
        self.timeout = timeout  # 单次请求超时, 秒
        self.backoff = backoff  # 退避基数, 秒
        self.max_retries = max_retries  # 传输层最多重试次数
        self._transport = transport
        self._sleep = sleep
        self.token_param = "max_tokens"  # 输出上限的参数名; 模型不支持时改为 max_completion_tokens
        self.send_temperature = True  # 是否发送 temperature; 模型只支持默认值时为假
        install_redaction(api_key)

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        """调用一次 (含传输层重试). 输出按 output_model 解析: 失败时 output 为空, raw 保留原文, error 为解析错误;
        model 与 token 数取响应. 输出被截断时 error 为 TRUNCATED (见类说明), 服务端回 400 的那种 token 数记 0."""
        truncated = TRUNCATED.format(limit=req.max_tokens)
        while True:
            body = self._body(req)
            try:
                data = await self._post(body)
                break
            except UnsupportedParameter as e:
                # 按本次实际发出的请求判断: 并发调用可能已先一步调整过, 这时照样按新方式重发
                if e.param not in body or not self._adapt(e.param):
                    raise
            except OutputLimitReached:
                return LLMResponse[output_model](  # type: ignore[valid-type]
                    output=None, raw="", model=self.model, input_tokens=0, output_tokens=0, error=truncated
                )
        raw, model, input_tokens, output_tokens, finish = self._envelope(data)
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
        if finish == "length":
            error = truncated
        return LLMResponse[output_model](  # type: ignore[valid-type]
            output=output, raw=raw, model=model, input_tokens=input_tokens, output_tokens=output_tokens, error=error
        )

    def _body(self, req: LLMRequest) -> dict[str, Any]:
        """请求体: temperature 与 JSON 模式按服务商的支持 (PROVIDERS); 输出上限用当前的参数名, 模型只支持默认温度时
        不带 temperature (见 _adapt)."""
        traits = PROVIDERS[self.provider]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": req.system},
                *({"role": m.role, "content": m.content} for m in req.messages),
            ],
            self.token_param: req.max_tokens,
        }
        if traits.temperature and self.send_temperature:
            body["temperature"] = req.temperature
        if traits.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _adapt(self, param: str) -> bool:
        """按不受支持的参数调整请求方式 (可重复调用, 结果相同); 不是可调整的参数返回假 (调用方照常抛错).
        调整只朝一个方向 (改用 max_completion_tokens, 不再发 temperature), 每次重发都少一个被拒参数, 不会循环."""
        if param == "max_tokens":
            self.token_param = "max_completion_tokens"
            return True
        if param == "temperature":
            self.send_temperature = False
            return True
        return False

    def _envelope(self, data: Any) -> tuple[str, str, int, int, Any]:
        """Chat Completions 响应的 (content, model, prompt_tokens, completion_tokens, finish_reason).

        只对缺失或 null 用默认值: content 取空串, model 取请求的模型名 (空串同样), usage 与 token 数取 0;
        给了值就须类型正确 (content, model 为字符串, usage 为对象, token 数为非负整数, 不接受 bool, 浮点),
        否则抛 LLMTransportError (信息只含类型名, 不含字段值).
        """
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
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
        return content, model or self.model, tokens[0], tokens[1], choice.get("finish_reason")

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """发请求, 可重试的失败按指数退避重试. 返回响应 JSON."""
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        failure = ""
        for attempt in range(self.max_retries + 1):
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
                if self._api_key and self._api_key in response.text:
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
            param = unsupported_param(response)
            if param is not None:
                raise UnsupportedParameter(failure, param)
            if output_limit_reached(response):
                raise OutputLimitReached(failure)
            if not retryable(response.status_code):
                raise LLMTransportError(failure)
        if not self.max_retries:
            raise LLMTransportError(failure)
        raise LLMTransportError(f"重试 {self.max_retries} 次后仍失败: {failure}")

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text


def unsupported_param(response: httpx.Response) -> str | None:
    """400 响应中被指明不受支持的参数名: 错误体 error.code 为 unsupported_parameter 或 unsupported_value 时取
    error.param; 其余情况 (含错误体不是这种格式) 为 None."""
    if response.status_code != 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or error.get("code") not in ("unsupported_parameter", "unsupported_value"):
        return None
    param = error.get("param")
    return param if isinstance(param, str) else None


def output_limit_reached(response: httpx.Response) -> bool:
    """400 响应的错误体 error.message 是否说输出达到上限 (含 OUTPUT_LIMIT_MESSAGE, 不分大小写); 错误体不是这种
    格式为假."""
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return isinstance(message, str) and OUTPUT_LIMIT_MESSAGE in message.lower()


class UnconfiguredLLMClient:
    """live 模式还没有可用配置 (设置页与 .env 都缺密钥或模型名) 时的占位: 应用照常启动, 调用时报错,
    程序按执行失败处理, 配好后可以重跑."""

    def __init__(self, reason: str) -> None:
        self.reason = reason  # 缺什么, 如 "缺密钥"

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        raise LLMTransportError(f"未配置大模型 ({self.reason}), 请在设置页配置模型与密钥")


class SwitchableLLMClient:
    """可热切换的客户端: 调用转发给当前的内部客户端; reload 之后, 下一次调用起用新的, 不用重启.

    组装根给主模型与复核模型各建一个, 主模型那个同时注入 AgentDeps.llm 与 JudgmentEngine. 已在进行的调用
    用的仍是开始时的内部客户端.
    """

    def __init__(self, inner: LLMClient, model_name: str) -> None:
        self.inner = inner  # 当前的内部客户端
        self.model_name = model_name  # 当前生效的模型名, 冻结进项目的版本快照

    def reload(self, inner: LLMClient, model_name: str) -> None:
        self.inner, self.model_name = inner, model_name

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        return await self.inner.complete(req, output_model)


class LlmPing(BaseModel):
    """测试连接时要求模型回的 JSON: {"ok": true}. 多出的键不算错."""

    ok: bool


def make_live_client(
    provider: LlmProvider,
    base_url: str,
    model: str,
    api_key: str,
    **options: Any,
) -> LLMClient:
    """按一套配置建 live 客户端; 缺模型名或该服务商要的密钥时为 UnconfiguredLLMClient. options 透传给 LiveLLMClient."""
    try:
        return LiveLLMClient(api_key, model, base_url or DEFAULT_BASE_URL, provider=provider, **options)
    except ValueError as e:
        return UnconfiguredLLMClient(str(e))


def llm_version(settings: Settings) -> dict[str, str]:
    """.env 给出的 LLM 信息: 模式, 主模型与复核模型的模型名 (mock 模式都为 "mock"; 复核模型回退为主模型).
    设置页保存后以 LlmSettingsService.versions 为准."""
    model = "mock" if settings.LLM_MODE == "mock" else settings.LLM_MODEL
    return {"llm_mode": settings.LLM_MODE, "model": model, "critic_model_version": model}


def env_provider(settings: Settings) -> LlmProvider:
    """.env 配置对应的服务商: 没填 LLM_BASE_URL 为 openai (默认地址), 否则为 custom."""
    return "custom" if settings.LLM_BASE_URL else "openai"


def make_llm_client(settings: Settings, engagements: EngagementStore | None = None) -> LLMClient:
    """按 LLM_MODE 建 .env 配置的客户端. mock 模式为 DatasetFixtureLLMClient (engagements 用来按项目数据集选 fixture
    目录, 为空时总读 LLM_FIXTURES_ROOT). live 模式缺密钥或模型名时为 UnconfiguredLLMClient (应用照常启动,
    可在设置页配置); 接口地址取 LLM_BASE_URL, 为空时为 DEFAULT_BASE_URL."""
    if settings.LLM_MODE == "mock":
        return DatasetFixtureLLMClient(settings.LLM_FIXTURES_ROOT, settings.DATASET_ROOT, engagements)
    return make_live_client(env_provider(settings), settings.LLM_BASE_URL, settings.LLM_MODEL, settings.LLM_API_KEY)
