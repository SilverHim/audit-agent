"""设置页的模型配置: 主模型 (primary, 给审计助理与各审计智能体) 与复核模型 (review, 给独立复核智能体 A7) 两套.

存库 (llm_settings, 密钥只存密文), 保存后热切换对应的 SwitchableLLMClient, 不用重启. 规则见 开发接口文档.md 第 10 节.
生效规则: 库里有某一套就以库为准; 没有时主模型回退到 .env 的 LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, 复核模型回退到主模型
的配置 (A7 另用独立的系统提示词). LLM_MODE 只是部署开关: mock 时保存照存, 客户端仍是固定输出.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from backend.adapters.llm import (
    DEFAULT_BASE_URL,
    LiveLLMClient,
    LlmPing,
    LLMTransportError,
    SwitchableLLMClient,
    env_provider,
    make_live_client,
)
from backend.contracts.models import LlmProvider, LlmSettings, LlmSlot
from backend.contracts.ports import LLMMessage, LLMRequest, LlmSettingsStore
from backend.core.config import Settings
from backend.core.security import SecretKeyError, decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)
SLOTS: tuple[LlmSlot, ...] = ("primary", "review")
TEST_TIMEOUT = 20.0  # 测试连接的超时, 秒; 不重试
# 测试连接的请求: 要模型回 {"ok": true}; system 里须出现 "JSON" (OpenAI 的 json_object 模式要求). 上限 1024: 推理类
# 模型的推理 token 计入上限, 原来的 64 可能还没写出正文就用完了 (需求 R312); 只封顶, 不增加用量
PING = LLMRequest(
    procedure_id="LLM-TEST",
    run_id="LLM-TEST",
    system='Reply with exactly this JSON object and nothing else: {"ok": true}',
    messages=[LLMMessage(role="user", content="Connection test.")],
    max_tokens=1024,
)


class SettingsRejected(Exception):
    """请求不合规, 什么都不写. code 为接口错误码: MISSING_FIELD, INVALID_VALUE (422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SlotView:
    """给界面看的一套配置: 不含密钥原文."""

    provider: LlmProvider
    base_url: str
    model: str
    has_key: bool
    key_last4: str | None
    updated_at: datetime | None  # 这一套没保存过 (取回退值) 时为空
    updated_by: str | None


@dataclass(frozen=True)
class ConnectionTest:
    """测试连接的结果. message 已脱敏."""

    ok: bool
    model: str | None  # 响应里的模型名, 没拿到响应为空
    latency_ms: int
    message: str


def last4(key: str) -> str | None:
    return key[-4:] if key else None


def normalize_url(base_url: str) -> str:
    """去掉首尾空白与末尾斜杠, 保存与比较都用这个形式."""
    return base_url.strip().rstrip("/")


def check_input(base_url: str, model: str) -> None:
    """base_url 须是 http 或 https 地址, model 不能为空; 不合规抛 SettingsRejected (INVALID_VALUE)."""
    try:
        parts = urlsplit(base_url)
    except ValueError:  # 如 http://[ 这类畸形地址
        raise SettingsRejected("INVALID_VALUE", f"接口地址不合法: {base_url!r}") from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SettingsRejected("INVALID_VALUE", f"接口地址须是 http 或 https 地址: {base_url!r}")
    if not model.strip():
        raise SettingsRejected("INVALID_VALUE", "模型名不能为空")


class LlmSettingsService:
    """模型设置的读写, 测试连接与热切换. clients: 两套配置各自的可切换客户端, 由组装根注入 (同 AgentDeps 里的)."""

    def __init__(
        self,
        store: LlmSettingsStore,
        primary: SwitchableLLMClient,
        review: SwitchableLLMClient,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store  # 表 llm_settings
        self.clients: dict[LlmSlot, SwitchableLLMClient] = {"primary": primary, "review": review}
        self.settings = settings  # LLM_MODE 与 .env 的回退值
        self.transport = transport  # 测试时换成假 HTTP 传输; 新建的 live 客户端都用它
        # 串行化保存与加载 (单 worker): 读旧配置, 写库, 判断复核模型是否跟随, 热切换要一气呵成,
        # 否则交错的两次保存可能让较早的那次用旧配置覆盖客户端, 库与实际生效的配置不一致
        self._lock = asyncio.Lock()

    @property
    def live(self) -> bool:
        return self.settings.LLM_MODE == "live"

    def versions(self) -> dict[str, str]:
        """项目启动时冻结进版本快照的 LLM 信息: 模式, 主模型与复核模型当前生效的模型名 (mock 模式都为 "mock").
        复核模型的记为 critic_model_version (规格书 5.5)."""
        if not self.live:
            return {"llm_mode": self.settings.LLM_MODE, "model": "mock", "critic_model_version": "mock"}
        return {
            "llm_mode": self.settings.LLM_MODE,
            "model": self.clients["primary"].model_name,
            "critic_model_version": self.clients["review"].model_name,
        }

    async def load(self) -> None:
        """启动时把库里的配置换到客户端上. 只在 live 模式读库; 某一套读不了 (库不可用, 主密钥缺失或不对) 时记日志,
        沿用 .env 建的客户端, 不阻止启动."""
        if not self.live:
            return
        async with self._lock:
            await self._load()

    async def _load(self) -> None:
        for slot in SLOTS:
            try:
                await self.apply(slot)
            except SecretKeyError as e:
                log.warning("模型设置 %s 未生效, 沿用 .env: %s", slot, e.message)
            except Exception as e:  # 库不可用等; 只记类型, 不记可能带连接串的消息
                log.warning("模型设置 %s 未生效, 沿用 .env: %s", slot, type(e).__name__)

    async def view(self, slot: LlmSlot) -> SlotView:
        """界面显示的配置: 库里有就用库里的; 没有时按回退规则显示生效的值, updated_at 为空. 不解密."""
        row = await self.store.get(slot)
        if row is not None:
            return SlotView(
                provider=row.provider,
                base_url=row.base_url,
                model=row.model,
                has_key=row.api_key_encrypted is not None,
                key_last4=row.key_last4,
                updated_at=row.updated_at,
                updated_by=row.updated_by,
            )
        if slot == "review":
            return replace(await self.view("primary"), updated_at=None, updated_by=None)
        key = self.settings.LLM_API_KEY
        return SlotView(
            provider=env_provider(self.settings),
            base_url=self.settings.LLM_BASE_URL or DEFAULT_BASE_URL,
            model=self.settings.LLM_MODEL,
            has_key=bool(key),
            key_last4=last4(key),
            updated_at=None,
            updated_by=None,
        )

    async def config(self, slot: LlmSlot) -> tuple[LlmProvider, str, str, str]:
        """生效配置 (服务商, 地址, 模型名, 明文密钥), 按回退规则; 库里的密钥在此解密, 主密钥不可用抛 SecretKeyError."""
        row = await self.store.get(slot)
        if row is not None:
            key = (
                decrypt_secret(row.api_key_encrypted, self.settings.SETTINGS_SECRET_KEY)
                if row.api_key_encrypted
                else ""
            )
            return row.provider, row.base_url, row.model, key
        if slot == "review":
            return await self.config("primary")
        s = self.settings
        return env_provider(s), s.LLM_BASE_URL or DEFAULT_BASE_URL, s.LLM_MODEL, s.LLM_API_KEY

    async def apply(self, slot: LlmSlot) -> None:
        """按生效配置重建该套客户端 (live 模式); 缺模型名或密钥时换成调用即报错的占位客户端."""
        provider, base_url, model, key = await self.config(slot)
        self.clients[slot].reload(make_live_client(provider, base_url, model, key, transport=self.transport), model)

    async def resolve_key(
        self, slot: LlmSlot, provider: LlmProvider, base_url: str, api_key: str | None
    ) -> tuple[str, LlmSettings | None]:
        """请求要用的密钥: 给了就用给的; 没给时 Ollama 不用密钥, 其余沿用该套当前生效的密钥 (config: 库里的,
        没存过时按回退规则取 .env 或主模型的), 与界面显示的 has_key 一致.

        沿用时服务商与接口地址须与生效配置一致, 否则 INVALID_VALUE: 不让已有的密钥被发到别的地址. 生效配置没有密钥时
        MISSING_FIELD. 返回 (明文密钥, 库里的该套配置); 库里的密钥在这里解密.
        """
        stored = await self.store.get(slot)
        if api_key:
            return api_key, stored
        if provider == "ollama":
            return "", stored
        current_provider, current_url, _, key = await self.config(slot)
        if not key:
            raise SettingsRejected("MISSING_FIELD", "api_key: 这一套还没有密钥, 须填写")
        if (current_provider, normalize_url(current_url)) != (provider, base_url):
            raise SettingsRejected("INVALID_VALUE", "改了服务商或接口地址, 须重新填写密钥")
        return key, stored

    async def update(
        self,
        slot: LlmSlot,
        *,
        provider: LlmProvider,
        base_url: str,
        model: str,
        api_key: str | None,
        updated_by: str,
    ) -> SlotView:
        """保存一套配置, live 模式下随即热切换. api_key 为空表示沿用当前生效的密钥 (见 resolve_key);
        Ollama 不填时清掉密钥.

        校验不过抛 SettingsRejected, 主密钥不可用抛 SecretKeyError, 都在写库之前. 主模型保存后, 复核模型若没有
        自己的配置 (跟随主模型), 一并切换.
        """
        base_url, model = normalize_url(base_url), model.strip()
        check_input(base_url, model)
        async with self._lock:
            return await self._update(slot, provider, base_url, model, api_key, updated_by)

    async def _update(
        self, slot: LlmSlot, provider: LlmProvider, base_url: str, model: str, api_key: str | None, updated_by: str
    ) -> SlotView:
        key, stored = await self.resolve_key(slot, provider, base_url, api_key)
        encrypted: str | None
        if not key:  # Ollama 不填密钥: 清掉
            encrypted, shown = None, None
        elif not api_key and stored is not None and stored.api_key_encrypted is not None:
            encrypted, shown = stored.api_key_encrypted, stored.key_last4  # 沿用本套已存的, 不重新加密
        else:  # 新填的, 或沿用回退来的 (.env 或主模型的), 加密后存进本套
            encrypted, shown = encrypt_secret(key, self.settings.SETTINGS_SECRET_KEY), last4(key)
        row = LlmSettings(
            slot=slot,
            provider=provider,
            base_url=base_url,
            model=model,
            api_key_encrypted=encrypted,
            key_last4=shown,
            updated_by=updated_by,
            updated_at=datetime.now(UTC),
        )
        await self.store.save(row)
        if self.live:
            client = make_live_client(provider, base_url, model, key, transport=self.transport)
            self.clients[slot].reload(client, model)
            if slot == "primary" and await self.store.get("review") is None:
                self.clients["review"].reload(
                    make_live_client(provider, base_url, model, key, transport=self.transport), model
                )
        return await self.view(slot)

    async def test(
        self, slot: LlmSlot, *, provider: LlmProvider, base_url: str, model: str, api_key: str | None
    ) -> ConnectionTest:
        """用请求里还没保存的值发一次最小的请求 (JSON 模式, 要模型回 {"ok": true}), 超时 20 秒, 不重试.

        api_key 为空时按 resolve_key 沿用当前生效的密钥. 不改库, 不切换客户端. 失败不抛, 记在结果里; message 已脱敏.
        """
        base_url, model = normalize_url(base_url), model.strip()
        check_input(base_url, model)
        key, _ = await self.resolve_key(slot, provider, base_url, api_key)
        client = LiveLLMClient(
            key, model, base_url, provider=provider, timeout=TEST_TIMEOUT, max_retries=0, transport=self.transport
        )

        def redact(text: str) -> str:
            return text.replace(key, "***") if key else text

        started = time.monotonic()
        try:
            response = await client.complete(PING, LlmPing)
        except LLMTransportError as e:
            return ConnectionTest(ok=False, model=None, latency_ms=elapsed_ms(started), message=redact(str(e)))
        except Exception as e:  # 地址不合法等; 只给类型名
            return ConnectionTest(ok=False, model=None, latency_ms=elapsed_ms(started), message=type(e).__name__)
        ok = response.output is not None and response.output.ok is True
        reply = f'模型的回复不是 {{"ok": true}}: {response.raw[:200]}'
        # 没解析出来时带上客户端给的原因, 如输出在上限处被截断 (adapters/llm.TRUNCATED, 需求 R312)
        failure = f"{reply} ({response.error[:300]})" if response.output is None and response.error else reply
        message = "连接成功" if ok else redact(failure)
        return ConnectionTest(ok=ok, model=response.model, latency_ms=elapsed_ms(started), message=message)


def elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
