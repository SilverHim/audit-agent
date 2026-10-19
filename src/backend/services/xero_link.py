"""Xero 网页授权 (需求 R279): 在数据管理页连接, 选组织, 同步与断开; 令牌按项目加密存库.

- 授权: connect 生成授权地址, state 与 PKCE verifier 只存在进程内存 (AuthStates, Q1: 本系统只支持单 worker), 绑定
  项目与用户, 10 分钟有效, 取用即作废. 回调 (callback) 换令牌, 取授权给到的组织, 令牌以 Fernet (SETTINGS_SECRET_KEY)
  加密存进 xero_connections, 302 回前端. 只有一个组织时自动选定, 多个时等审计师选 (choose_tenant).
- 令牌: XeroClient 经 ConnectionTokens 读写库里的密文, 刷新 (令牌轮换) 后立即加密写库 (Q2). 刷新令牌失效
  (invalid_grant) 或换得超过 60 天时连接为 expired, 同步被拒, 须重新连接. 命令行的令牌文件 (var/xero/token.json)
  与这里互不读写.
- 同步: 按项目审计期间与上期期末 (起始日前一天) 取数, 原始响应只在内存中 (Q3), 经 build_tables 转成标准表, 以
  write_tables (来源 xero) 写成一个新数据版本, 写完体检. 写六张表 (Q7: 科目, 试算表, 总账分录, 销售发票, 银行账户,
  银行流水; 不写发运单与审批日志); 付费接口只在 XERO_PAID_ENDPOINTS=live 时申请并取 (Q5). 没取到银行对账单 (off,
  mock, 或 live 下对账单接口 401, 403) 时银行账户与银行流水这一对都不写, 已导入的保留 (只写账户, 旧流水的
  bank_account_id 可能对不上 Xero 生成的账户 ID; 也不拿空表盖掉已导入的流水). 同步与导入共用项目锁, 同步期间再同步
  或导入报 ENGAGEMENT_BUSY.
- 断开: 调 Xero 撤销接口, 删除本项目的连接与令牌; 撤销失败也删 (Q6), 响应与留痕记 revoked: false. 已同步的数据与
  版本不动. 锁定的项目也能断开.
令牌, 授权码与 client secret 不进日志, 留痕, 响应与异常消息 (留痕 payload 只有组织名, 组织数, scope, 表名, 资源名,
原因代码与状态码).
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from backend.adapters.xero import (
    MemoryTokens,
    Tokens,
    XeroAuthExpired,
    XeroClient,
    XeroError,
    authorize_url,
    code_challenge,
    code_verifier,
    fetch,
    login_scopes,
)
from backend.adapters.xero_dataset import Period, Snapshot, build_tables
from backend.agents.base import AgentDeps
from backend.contracts.data import XERO_TABLES, TableKind, XeroConnection, XeroSource, XeroTenant
from backend.contracts.errors import DataRejected, DecisionRejected
from backend.contracts.models import TrailEntry, TrailKind
from backend.contracts.ports import XeroConnectionStore
from backend.core.config import Settings
from backend.core.security import SecretKeyError, decrypt_secret, encrypt_secret
from backend.services.data_service import DataService

logger = logging.getLogger(__name__)
STATE_TTL = timedelta(minutes=10)  # 授权 state 的有效期
USED_TTL = timedelta(hours=1)  # 用掉的 state 记多久 (重复使用报 state_used, 之后按不认识的处理)
REFRESH_LIFETIME = timedelta(days=60)  # 刷新令牌的有效期: 换得之后这么久没刷新即失效
CALLBACK_PATH = "/api/v1/integrations/xero/callback"  # 回调路由 (XERO_REDIRECT_URI 的路径)
# 回调失败的原因代码 (302 带 ?xero=error&reason=), 前端按代码给文案; 之外不回带任何内容
CALLBACK_REASONS = (
    "denied",  # 用户在 Xero 拒绝授权
    "state_invalid",  # state 不认识 (不是本系统发起的, 或进程重启过)
    "state_used",  # state 已用过
    "state_expired",  # 超过 10 分钟
    "not_found",  # 项目不存在
    "locked",  # 项目已锁定
    "busy",  # 项目正忙
    "exchange_failed",  # 换令牌或取组织失败
    "no_organisation",  # 授权里没有组织
    "settings_key",  # 加密主密钥不可用
    "internal_error",  # 其他意外错误 (如存库失败), 只记异常类型
)
# 银行这一对: 没取到银行对账单时一起不写 (见模块说明)
BANK_TABLES = (TableKind.bank_accounts, TableKind.bank_transactions)
PAID_RESOURCES = frozenset({"journals", "bank_statements"})  # 付费接口的资源名: 401, 403 时退回 (单据反推, 不写银行)


class CallbackFailed(Exception):
    """授权回调失败: reason 为原因代码, eid 为能认出的项目 (认不出 state 时为空)."""

    def __init__(self, reason: str, eid: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.eid = eid


@dataclass(frozen=True)
class PendingAuth:
    """一次发起中的授权: 项目, 发起人, PKCE verifier (Web app 为空串), 发起时间."""

    eid: str
    user_id: str
    verifier: str = field(repr=False)
    created_at: datetime


class AuthStates:
    """进程内存里的授权 state (Q1): 按 state 的 sha256 登记, 不存原值; 取用即删, 用过的另记一段时间以分辨重复使用.
    授权途中重启后端, 那次授权作废 (回调报 state_invalid), 要再点一次连接."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self.now = now
        self.pending: dict[str, PendingAuth] = {}  # state 的哈希 → 发起中的授权
        self.used: dict[str, tuple[str, datetime]] = {}  # 用过的 state 的哈希 → (项目, 用掉的时间)

    @staticmethod
    def digest(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    def issue(self, eid: str, user_id: str, verifier: str) -> str:
        """登记一次授权, 返回 state."""
        self.prune()
        state = secrets.token_urlsafe(32)
        self.pending[self.digest(state)] = PendingAuth(eid, user_id, verifier, self.now())
        return state

    def take(self, state: str | None) -> PendingAuth:
        """取用 state (取到即作废): 不认识 state_invalid, 用过 state_used, 过期 state_expired (抛 CallbackFailed)."""
        self.prune()
        if not state:
            raise CallbackFailed("state_invalid")
        key = self.digest(state)
        if key in self.used:
            raise CallbackFailed("state_used", self.used[key][0])
        pending = self.pending.pop(key, None)
        if pending is None:
            raise CallbackFailed("state_invalid")
        self.used[key] = (pending.eid, self.now())
        if self.now() - pending.created_at > STATE_TTL:
            raise CallbackFailed("state_expired", pending.eid)
        return pending

    def prune(self) -> None:
        """清掉早已过期的登记 (过期后再留一个 USED_TTL, 期间回调仍报 state_expired) 与早已用掉的记录."""
        now = self.now()
        self.pending = {k: v for k, v in self.pending.items() if now - v.created_at <= STATE_TTL + USED_TTL}
        self.used = {k: v for k, v in self.used.items() if now - v[1] <= USED_TTL}


def key_rejected(error: SecretKeyError) -> DataRejected:
    """加密主密钥不可用 → 数据接口的错误 (503 SETTINGS_KEY_MISSING / SETTINGS_KEY_INVALID), 消息写 Xero 的处理办法."""
    if error.code == "SETTINGS_KEY_MISSING":
        return DataRejected(error.code, "未配置 SETTINGS_SECRET_KEY, 不能保存或读取 Xero 令牌")
    return DataRejected(error.code, "SETTINGS_SECRET_KEY 不对 (格式不对或已更换), 已存的 Xero 令牌解不开, 请重新连接")


def seal(tokens: Tokens, key: str) -> str:
    """令牌 (访问令牌, 刷新令牌, 到期时间) 加密成可存库的文本; 组织与 scope 存在连接行里."""
    plain = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
    }
    try:
        return encrypt_secret(json.dumps(plain), key)
    except SecretKeyError as e:
        raise key_rejected(e) from None


def expired(connection: XeroConnection, now: datetime) -> bool:
    """连接是否已失效: 标过 expired (invalid_grant), 或刷新令牌换得已超过 60 天."""
    return connection.status == "expired" or now - connection.refreshed_at > REFRESH_LIFETIME


@dataclass
class ConnectionTokens:
    """网页连接的令牌来源 (TokenSource): 读写 xero_connections 的密文. 组织与 scope 取连接行; 刷新后立即加密写库,
    并记下换得时间 (60 天过期从这里算). 主密钥不可用抛 DataRejected (SETTINGS_KEY_*)."""

    store: XeroConnectionStore
    eid: str
    key: str = field(repr=False)
    now: Callable[[], datetime]

    async def load(self) -> Tokens | None:
        connection = await self.store.get(self.eid)
        encrypted = await self.store.tokens(self.eid)
        if connection is None or encrypted is None:
            return None
        try:
            plain = json.loads(decrypt_secret(encrypted, self.key))
        except SecretKeyError as e:
            raise key_rejected(e) from None
        return Tokens(
            access_token=plain["access_token"],
            refresh_token=plain["refresh_token"],
            expires_at=float(plain["expires_at"]),
            scope=connection.scope,
            tenant_id=connection.tenant_id or "",
            tenant_name=connection.tenant_name or "",
        )

    async def save(self, tokens: Tokens) -> None:
        await self.store.set_tokens(self.eid, seal(tokens, self.key), self.now())


class StoredXeroConnections:
    """数据概览的 sources.xero (端口 XeroConnections): 没有连接行为 not_connected; 有则 connected 或 expired (见
    expired), 组织名为选定的组织 (多个组织未选时为空, 前端据此显示选组织), scope 为最近一次同步写的表."""

    def __init__(self, store: XeroConnectionStore, now: Callable[[], datetime]) -> None:
        self.store = store
        self.now = now

    async def source(self, eid: str) -> XeroSource:
        connection = await self.store.get(eid)
        if connection is None:
            return XeroSource(status="not_connected")
        return XeroSource(
            status="expired" if expired(connection, self.now()) else "connected",
            organisation=connection.tenant_name,
            last_synced_at=connection.last_synced_at,
            scope=list(connection.synced_tables),
        )


class XeroLink:
    """Xero 网页授权的服务 (组装根建一个, 挂在 app.state.xero). 写操作与导入共用项目锁 (经 DataService)."""

    def __init__(
        self,
        deps: AgentDeps,
        data: DataService,
        store: XeroConnectionStore,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.deps = deps
        self.data = data  # 项目锁, 后台作业与 write_tables
        self.store = store  # 连接与令牌密文
        self.settings = settings
        self.transport = transport  # 测试换成假 Xero
        self.clock = clock  # 秒; 测试固定
        self.sleep = sleep
        self.states = AuthStates(self.now)
        self.connections = StoredXeroConnections(store, self.now)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    @property
    def paid(self) -> bool:
        """是否取付费接口 (Journals, Bank Statements Plus): 只在 XERO_PAID_ENDPOINTS=live 时 (Q5)."""
        return self.settings.XERO_PAID_ENDPOINTS == "live"

    def client(self, tokens: MemoryTokens | ConnectionTokens) -> XeroClient:
        return XeroClient(
            self.settings.XERO_CLIENT_ID,
            tokens,
            self.settings.XERO_CLIENT_SECRET,
            transport=self.transport,
            sleep=self.sleep,
            clock=self.clock,
        )

    def tokens(self, eid: str) -> ConnectionTokens:
        return ConnectionTokens(self.store, eid, self.settings.SETTINGS_SECRET_KEY, self.now)

    # ── 授权 ──

    async def authorize(self, eid: str, user_id: str) -> str:
        """授权地址 (GET /data/xero/connect): 项目未锁定 (423), 已配 XERO_CLIENT_ID (503 XERO_NOT_CONFIGURED), 主密钥
        可用 (503 SETTINGS_KEY_*) 才发. 有 client secret 时按 Web app, 没有时按 PKCE; scope 为只读 scope 加
        offline_access, 付费接口为 live 时另申请 Journals 与 Bank Statements Plus."""
        await self.data.unlocked(eid)
        if not self.settings.XERO_CLIENT_ID:
            raise DataRejected("XERO_NOT_CONFIGURED", "未配置 XERO_CLIENT_ID, 不能连接 Xero")
        seal(Tokens("", "", 0.0), self.settings.SETTINGS_SECRET_KEY)  # 主密钥不可用时现在就报, 不等回调
        verifier = "" if self.settings.XERO_CLIENT_SECRET else code_verifier()
        challenge = code_challenge(verifier) if verifier else ""
        state = self.states.issue(eid, user_id, verifier)
        scopes = login_scopes(journals=self.paid, statements=self.paid)
        return authorize_url(self.settings.XERO_CLIENT_ID, self.settings.XERO_REDIRECT_URI, challenge, state, scopes)

    async def callback(self, code: str | None, state: str | None, error: str | None) -> str:
        """授权回调 (GET /integrations/xero/callback), 返回 302 的跳转地址. 先取用 state (不认识, 用过, 过期都拒),
        再看 error (用户拒绝), 然后持项目锁 (锁定, 忙) 换令牌, 取组织, 加密存库, 写 data 留痕 (action xero_connect).
        成功跳 {前端}/engagements/{eid}/data?xero=connected, 失败跳同一页 (认不出项目时跳前端首页) 带
        ?xero=error&reason=<原因代码> (CALLBACK_REASONS)."""
        eid: str | None = None
        try:
            pending = self.states.take(state)
            eid = pending.eid
            if error:
                raise CallbackFailed("denied", eid)
            if not code:
                raise CallbackFailed("exchange_failed", eid)
            try:
                async with self.data.locked(eid):
                    await self.connect(pending, code)
            except LookupError:
                raise CallbackFailed("not_found", None) from None
            except DecisionRejected as e:  # 含 DataRejected
                reason = {"ENGAGEMENT_LOCKED": "locked", "ENGAGEMENT_BUSY": "busy"}.get(e.code, "settings_key")
                raise CallbackFailed(reason, eid) from None
        except CallbackFailed as e:
            logger.info("Xero 授权回调失败: %s %s", e.eid or "-", e.reason)
            return self.redirect(e.eid, e.reason)
        except Exception as e:  # 意外错误也回前端; 只记类型, 消息可能带出值
            logger.error("Xero 授权回调出错: %s %s", eid or "-", type(e).__name__)
            return self.redirect(eid, "internal_error")
        return self.redirect(eid, None)

    async def connect(self, pending: PendingAuth, code: str) -> None:
        """换令牌, 取组织, 存连接 (持项目锁调用). 重新连接时保留最近一次同步的时间与表, 原来选的组织仍在授权里时保留."""
        eid = pending.eid
        memory = MemoryTokens()
        client = self.client(memory)
        try:
            tokens = await client.exchange(code, pending.verifier, self.settings.XERO_REDIRECT_URI)
            organisations = [
                XeroTenant(id=str(c["tenantId"]), name=str(c["tenantName"]))
                for c in await client.connections()
                if c.get("tenantType") == "ORGANISATION"
            ]
        except (XeroError, KeyError) as e:
            logger.info("Xero 换令牌失败: %s %s", eid, e if isinstance(e, XeroError) else type(e).__name__)
            raise CallbackFailed("exchange_failed", eid) from None
        if not organisations:
            raise CallbackFailed("no_organisation", eid)
        previous = await self.store.get(eid)
        chosen = organisations[0] if len(organisations) == 1 else None
        if chosen is None and previous is not None:
            chosen = next((t for t in organisations if t.id == previous.tenant_id), None)
        now = self.now()
        connection = XeroConnection(
            tenants=organisations,
            tenant_id=chosen.id if chosen else None,
            tenant_name=chosen.name if chosen else None,
            scope=tokens.scope,
            connected_by=pending.user_id,
            connected_at=now,
            refreshed_at=now,
            last_synced_at=previous.last_synced_at if previous else None,
            synced_tables=list(previous.synced_tables) if previous else [],
        )
        await self.store.put(eid, connection, seal(tokens, self.settings.SETTINGS_SECRET_KEY))
        summary = f"连接 Xero, 授权 {len(organisations)} 个组织" + (f", 选定 {chosen.name}" if chosen else ", 待选组织")
        await self.record(
            eid,
            pending.user_id,
            summary,
            {
                "action": "xero_connect",
                "organisations": len(organisations),
                "organisation": chosen.name if chosen else None,
                "scope": tokens.scope,
            },
        )

    def redirect(self, eid: str | None, reason: str | None) -> str:
        """回调后跳回前端的地址: 认得出项目跳它的数据管理页, 否则跳首页; 只带 xero 与原因代码."""
        base = self.settings.frontend_url
        path = f"/engagements/{urllib.parse.quote(eid, safe='')}/data" if eid else "/"
        query = {"xero": "connected"} if reason is None else {"xero": "error", "reason": reason}
        return f"{base}{path}?{urllib.parse.urlencode(query)}"

    # ── 组织 ──

    async def connection(self, eid: str) -> XeroConnection:
        """该项目的连接; 没有 409 XERO_NOT_CONNECTED."""
        found = await self.store.get(eid)
        if found is None:
            raise DataRejected("XERO_NOT_CONNECTED", "还没有连接 Xero")
        return found

    async def tenants(self, eid: str) -> list[XeroTenant]:
        """授权给到的组织 (GET /data/xero/tenants), 取连接时存下的列表, 不调 Xero."""
        return list((await self.connection(eid)).tenants)

    async def choose_tenant(self, eid: str, tenant_id: str, actor: str) -> XeroSource:
        """选定组织 (PUT /data/xero/tenant), 只能选授权给到的 (否则 422 INVALID_VALUE); 持项目锁 (锁定 423, 忙 409).
        改了时写 data 留痕 (action xero_tenant). 返回概览里的 Xero 连接."""
        async with self.data.locked(eid):
            connection = await self.connection(eid)
            chosen = next((t for t in connection.tenants if t.id == tenant_id), None)
            if chosen is None:
                raise DataRejected("INVALID_VALUE", "不是这次授权给到的组织")
            if connection.tenant_id != chosen.id:
                await self.store.update(
                    eid, connection.model_copy(update={"tenant_id": chosen.id, "tenant_name": chosen.name})
                )
                await self.record(
                    eid, actor, f"选定 Xero 组织 {chosen.name}", {"action": "xero_tenant", "organisation": chosen.name}
                )
        return await self.connections.source(eid)

    # ── 同步 ──

    def labels(self) -> list[TableKind]:
        """同步计划写的表 (202 的 tables, 期间概览中为 converting): 取付费接口 (live) 时六张 (Q7); 不取时没有银行
        对账单, 银行账户与银行流水都不写. live 下对账单接口 401, 403 时实际也不写这两张 (见 run_sync)."""
        return [k for k in XERO_TABLES if self.paid or k not in BANK_TABLES]

    def skip_reason(self) -> str:
        """没写银行这一对的原因代码: 付费接口没开 (off, mock) 为 paid_off, live 下对账单接口拒绝为
        statements_forbidden."""
        return "statements_forbidden" if self.paid else "paid_off"

    async def usable(self, eid: str) -> XeroConnection:
        """能同步的连接: 没有 XERO_NOT_CONNECTED, 已失效 XERO_RECONNECT_REQUIRED (并置 expired), 多个组织未选
        XERO_TENANT_REQUIRED (都是 409)."""
        connection = await self.connection(eid)
        if expired(connection, self.now()):
            await self.expire(eid)
            raise DataRejected("XERO_RECONNECT_REQUIRED", "Xero 授权已失效, 请重新连接")
        if not connection.tenant_id:
            raise DataRejected("XERO_TENANT_REQUIRED", "授权里有多个组织, 先选定要同步的组织")
        return connection

    async def expire(self, eid: str) -> None:
        """把连接置 expired (刷新令牌失效)."""
        connection = await self.store.get(eid)
        if connection is not None and connection.status != "expired":
            await self.store.update(eid, connection.model_copy(update={"status": "expired"}))

    async def sync(self, eid: str, actor: str) -> list[TableKind]:
        """开始同步 (POST /data/xero/sync, 202), 返回计划写的表 (labels). 当场拒绝: 锁定 423; 忙 (图在执行, 导入,
        另一同步) 409 ENGAGEMENT_BUSY; 连接不可用 (见 usable); 访问令牌快到期时先刷新一次, invalid_grant 置 expired
        并报 XERO_RECONNECT_REQUIRED, Xero 不可用 502 XERO_UNAVAILABLE. 之后在后台取数并写入 (见 run_sync); 期间
        概览中计划写的表为 converting, processing 计数. 实际写了哪些表看完成后概览的 scope 与留痕 xero_synced."""
        engagement = await self.data.unlocked(eid)
        labels = self.labels()
        client = self.client(self.tokens(eid))

        async def prepare() -> None:
            await self.usable(eid)
            try:
                await client.access()
            except XeroAuthExpired:
                await self.expire(eid)
                raise DataRejected("XERO_RECONNECT_REQUIRED", "Xero 授权已失效, 请重新连接") from None
            except XeroError as e:
                raise DataRejected("XERO_UNAVAILABLE", f"Xero 暂时连不上: {e}") from None

        period = Period(engagement.period_start, engagement.period_end, engagement.period_start - timedelta(days=1))
        await self.data.launch(
            eid, [k.value for k in labels], lambda: self.run_sync(eid, actor, client, period), prepare
        )
        return labels

    async def run_sync(self, eid: str, actor: str, client: XeroClient, period: Period) -> None:
        """后台的同步作业 (持项目锁): 取数 (只在内存中), 转成标准表, 写一个新数据版本 (write_tables 写完体检), 记下
        同步时间与实际写的表, 写 data 留痕 xero_synced (tables 为实际写的表, skipped 为计划内外没写的表与原因).
        写 labels() 中的表; 没取到银行对账单 (live 下对账单接口 401, 403) 时银行这一对都不写 (skip_reason).
        失败时不写数据, 写 data 留痕 xero_sync_failed (payload 只有原因代码与状态码): 授权失效 expired (另置
        expired); Xero 报错 xero_error; 付费接口以外的资源 401, 403 forbidden (不拿空表盖掉已有的数据, payload 另带
        资源名); 中途主密钥不可用 settings_key; 响应转不了表 mapping. 写库失败记 write 后上抛 (由 _spawn 记日志)."""
        files: dict[str, Any] = {}
        try:
            manifest = await fetch(client, period, files.__setitem__, paid="live" if self.paid else "off")
            denied = sorted(set(manifest["unavailable"]) - PAID_RESOURCES)
            if denied:
                logger.warning("Xero 同步取不到资源 (无权限): %s %s", eid, denied)
                await self.sync_failed(eid, actor, "forbidden", None, resources=denied)
                return
            tables = build_tables(Snapshot(Path(f"xero-sync-{eid}"), files=files))
            statements = "bank_statements" in files
        except XeroAuthExpired as e:
            await self.expire(eid)
            await self.sync_failed(eid, actor, "expired", e.status)
            return
        except XeroError as e:
            logger.warning("Xero 同步失败: %s %s", eid, e)
            await self.sync_failed(eid, actor, "xero_error", e.status)
            return
        except DataRejected as e:  # 中途换了主密钥: 令牌解不开或刷新后存不了
            logger.warning("Xero 同步时主密钥不可用: %s %s", eid, e.code)
            await self.sync_failed(eid, actor, "settings_key", None)
            return
        except Exception as e:  # 响应的结构不对或转换出错; 只记类型, 消息可能含往来名称
            logger.warning("Xero 同步的数据无法转换: %s %s", eid, type(e).__name__)
            await self.sync_failed(eid, actor, "mapping", None)
            return
        finally:
            files.clear()  # 原始响应 (含联系人) 用完即丢
        kinds = [k for k in self.labels() if statements or k not in BANK_TABLES]
        skipped = {k.value: self.skip_reason() for k in XERO_TABLES if k not in kinds}
        try:
            await self.data.write_tables(eid, {k: tables[k.value] for k in kinds}, source="xero", actor=actor)
        except Exception:
            await self.sync_failed(eid, actor, "write", None)
            raise
        connection = await self.store.get(eid)
        if connection is not None:
            update = {"last_synced_at": self.now(), "synced_tables": kinds}
            await self.store.update(eid, connection.model_copy(update=update))
        summary = f"Xero 同步完成, 写入 {len(kinds)} 张表" + (f", 未写 {', '.join(skipped)}" if skipped else "")
        payload: dict[str, object] = {"action": "xero_synced", "tables": [k.value for k in kinds], "skipped": skipped}
        await self.record(eid, actor, summary, payload)

    async def sync_failed(
        self, eid: str, actor: str, reason: str, status: int | None, *, resources: list[str] | None = None
    ) -> None:
        """同步失败的 data 留痕: 原因代码与状态码, forbidden 另带取不到的资源名."""
        payload: dict[str, object] = {"action": "xero_sync_failed", "reason": reason, "status": status}
        if resources:
            payload["resources"] = resources
        await self.record(eid, actor, f"Xero 同步失败 ({reason})", payload)

    # ── 断开 ──

    async def disconnect(self, eid: str, actor: str) -> bool:
        """断开 (DELETE /data/xero), 返回 Xero 侧是否已撤销. 持项目锁 (忙 409; 锁定的项目也可以断开); 没有连接 409
        XERO_NOT_CONNECTED. 调撤销接口后删除连接与令牌; 撤销失败 (网络, 4xx, 令牌解不开) 也删, 返回 False, 由前端
        提示到 Xero 后台的 Connected apps 手动移除. 写 data 留痕 (action xero_disconnect, payload 带 revoked).
        已同步的数据与版本不动."""
        if await self.deps.engagement_store.get(eid) is None:
            raise LookupError(f"Engagement {eid} not found")
        self.data.runner.acquire(eid)
        try:
            connection = await self.connection(eid)
            revoked = False
            try:
                tokens = await self.tokens(eid).load()
                if tokens is not None:
                    await self.client(MemoryTokens(tokens)).revoke(tokens)
                    revoked = True
            except (XeroError, DataRejected) as e:
                logger.warning("Xero 撤销授权失败, 只删本地令牌: %s %s", eid, e)
            await self.store.delete(eid)
            await self.record(
                eid,
                actor,
                "断开 Xero" + ("" if revoked else " (Xero 侧未能撤销, 须到 Xero 后台移除)"),
                {"action": "xero_disconnect", "revoked": revoked, "organisation": connection.tenant_name},
            )
        finally:
            self.data.runner.release(eid)
        return revoked

    async def record(self, eid: str, actor: str, summary: str, payload: dict[str, object]) -> None:
        """一条 Xero 连接的 data 留痕; 写失败只记日志 (连接已存或已删, 不因留痕失败报错)."""
        entry = TrailEntry(
            engagement_id=eid, kind=TrailKind.data, actor=actor, summary=summary, payload=payload, created_at=self.now()
        )
        try:
            await self.deps.trail.record(entry)
        except Exception:
            logger.exception("Xero 留痕写入失败: %s %s", eid, payload.get("action"))
