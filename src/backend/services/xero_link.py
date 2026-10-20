"""Xero 网页授权 (需求 R279): 在数据管理页连接, 选组织, 同步与断开; 令牌按项目加密存库.

- 授权: connect 生成授权地址, state 与 PKCE verifier 只存在进程内存 (AuthStates, Q1: 本系统只支持单 worker), 绑定
  项目与用户, 10 分钟有效, 取用即作废. 回调 (callback) 换令牌, 取授权给到的组织, 令牌以 Fernet (SETTINGS_SECRET_KEY)
  加密存进 xero_connections, 302 回前端. 只有一个组织时自动选定, 多个时等审计师选 (choose_tenant).
- 令牌: XeroClient 经 ConnectionTokens 读写库里的密文, 刷新 (令牌轮换) 后立即加密写库 (Q2). 刷新令牌失效
  (invalid_grant) 或换得超过 60 天时连接为 expired, 同步被拒, 须重新连接. 命令行的令牌文件 (var/xero/token.json)
  与这里互不读写.
- 同步: 按项目审计期间与上期期末 (起始日前一天) 取数, 原始响应只在内存中 (Q3), 经 build_tables 转成标准表, 以
  write_tables (来源 xero) 写成一个新数据版本, 写完体检. 写七张表 (Q7: 科目, 试算表, 总账分录, 销售发票, 应付账单
  (R178), 银行账户, 银行流水; 不写发运单与审批日志); 付费接口只在 XERO_PAID_ENDPOINTS=live 时申请并取 (Q5). 没取到
  银行对账单 (off, mock, 或 live 下对账单接口 401, 403) 时银行账户与银行流水这一对都不写, 已导入的保留 (只写账户,
  旧流水的 bank_account_id 可能对不上 Xero 生成的账户 ID; 也不拿空表盖掉已导入的流水). 同步与导入共用项目锁, 同步
  期间再同步或导入报 ENGAGEMENT_BUSY.
- 断开: 调 Xero 撤销接口, 删除本项目的连接与令牌; 撤销失败也删 (Q6), 响应与留痕记 revoked: false. 已同步的数据与
  版本不动. 锁定的项目也能断开.
- 组织信息与客户画像的核对 (需求 R291): 同步成功时从取到的组织信息记下名称, 法定名称与 ABN (随 xero_synced 留痕,
  services.xero_profile), 概览按留痕现算核对结果 (StoredXeroConnections.profile_check); 审计师保持原样 (写理由) 或
  按 Xero 更新画像 (resolve_profile): 只有停在 ACC-01 闸门时本项目采用新版本 (走更新客户资料, 重跑 ACC-04), 其余时候
  只给客户写新版本; 选择各写一条 data 留痕. TaxNumber (澳洲为税号) 不读.
令牌, 授权码与 client secret 不进日志, 留痕, 响应与异常消息 (留痕 payload 只有组织名, 组织的法定名称与 ABN, 组织数,
scope, 表名, 资源名, 原因代码与状态码).
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
from typing import TYPE_CHECKING, Any, Literal, get_args

import httpx
from pydantic import ValidationError

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
from backend.contracts.data import (
    XERO_TABLES,
    TableKind,
    XeroConnection,
    XeroProfileCheck,
    XeroProfileDecision,
    XeroProfileStage,
    XeroSkipReason,
    XeroSource,
    XeroSyncFailure,
    XeroSyncResult,
    XeroTenant,
)
from backend.contracts.errors import AbnTaken, DataRejected, DecisionRejected, VersionConflict
from backend.contracts.models import ClientProfile, GateId, Phase, ProfileSource, TrailEntry, TrailKind
from backend.contracts.ports import AuditTrail, EngagementStore, XeroConnectionStore
from backend.core.config import Settings
from backend.core.security import SecretKeyError, decrypt_secret, encrypt_secret
from backend.services.abn import abn_problem, dataset_profiles
from backend.services.data_service import DataService
from backend.services.xero_profile import check_key, organisation_info, profile_differences

if TYPE_CHECKING:  # 只作类型: 编排层引用服务层, 运行时不反向导入
    from backend.orchestration.decisions import GraphDecisionSubmit

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
SYNC_JOB = "xero_sync"  # 同步作业在 DataService 登记的种类 (概览的 syncing 按它)
# 概览的 last_sync 取这几种 data 留痕中最近的一条: 同步结束时写的两种, 加上断开 (最近一条是断开时为空: 上次结果属于
# 当前连接, 与连接行的最近同步, 接入范围同口径, 断开再连为空, 失效重连保留)
SYNC_ACTIONS = ("xero_synced", "xero_sync_failed", "xero_disconnect")
FAILURES: frozenset[str] = frozenset(get_args(XeroSyncFailure))  # 认得的失败原因代码
SKIPS: frozenset[str] = frozenset(get_args(XeroSkipReason))  # 认得的没写原因代码
# 核对用的组织信息取最近一次成功同步记下的; 最近一条是断开时为空 (同 last_sync, 属于当前连接)
ORGANISATION_ACTIONS = ("xero_synced", "xero_disconnect")
RESOLUTION_ACTIONS = ("xero_profile_kept", "xero_profile_updated")  # 审计师对核对的选择 (data 留痕)


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
    expired), 组织名与 tenant_id 为选定的组织 (多个组织未选时为空, 前端据此显示选组织), scope 为最近一次同步写的表,
    syncing 为同步作业在跑 (syncing 回调, 即 DataService.running 的 SYNC_JOB), last_sync 为最近一次同步的结果,
    profile_check 为组织信息与本项目采用的客户画像的核对 (见 profile_check)."""

    def __init__(
        self,
        store: XeroConnectionStore,
        now: Callable[[], datetime],
        trail: AuditTrail | None = None,
        syncing: Callable[[str], bool] = lambda eid: False,
        engagements: EngagementStore | None = None,
        gate: Callable[[str], Awaitable[GateId | None]] | None = None,
    ) -> None:
        self.store = store
        self.now = now
        self.trail = trail  # 取 last_sync 与 profile_check; 为空时两者一律为空
        self.syncing = syncing
        self.engagements = engagements  # 取本项目采用的画像; 为空时 profile_check 为空
        self.gate = gate  # 检查点停在哪道闸门 (DataService.gate_now); 为空时按不在闸门算 profile_check 的 stage

    async def source(self, eid: str) -> XeroSource:
        connection = await self.store.get(eid)
        if connection is None:
            return XeroSource(status="not_connected")
        return XeroSource(
            status="expired" if expired(connection, self.now()) else "connected",
            organisation=connection.tenant_name,
            tenant_id=connection.tenant_id,
            last_synced_at=connection.last_synced_at,
            scope=list(connection.synced_tables),
            syncing=self.syncing(eid),
            last_sync=await self.last_sync(eid),
            profile_check=await self.profile_check(eid, connection),
        )

    async def last_sync(self, eid: str) -> XeroSyncResult | None:
        """当前连接最近一次同步的结果: 本项目 SYNC_ACTIONS 中最近的一条 data 留痕 (AuditTrail.last_of, 不读完全部
        留痕; 不另存, 表结构不变) 转成; 没有, 或最近一条是断开时为空. 时间取留痕写入时间.
        留痕里认不出的原因代码读成 unknown, 认不出的表跳过 (记日志), 不让一条留痕拖垮概览."""
        if self.trail is None:
            return None
        entry = await self.trail.last_of(eid, TrailKind.data, SYNC_ACTIONS)
        if entry is None or entry.payload.get("action") == "xero_disconnect":
            return None
        return sync_result(entry, self.now())

    async def profile_check(self, eid: str, connection: XeroConnection) -> XeroProfileCheck | None:
        """组织信息与本项目采用的客户画像的核对 (需求 R291), 按留痕现算 (不另存, 表结构不变): 组织信息取本项目
        ORGANISATION_ACTIONS 中最近一条 data 留痕, 须是当前选定的组织的 (organisation_info.tenant_id 等于连接的
        tenant_id; 是断开, 改选组织或换授权后还没同步, 或是没记组织信息的同步时为空), 画像取
        Engagement.client_profile_version 那一版; 选择取 RESOLUTION_ACTIONS 中最近一条, 其 key 与本次相同才算.
        next_version 为该客户最新一版 +1; stage 按检查点所在的闸门与项目阶段 (停在 ACC-01 为 at, 承接阶段为 before,
        其余为 after). 项目或画像读不到时为空."""
        if self.trail is None or self.engagements is None:
            return None
        synced = await self.trail.last_of(eid, TrailKind.data, ORGANISATION_ACTIONS)
        if synced is None or synced.payload.get("action") != "xero_synced":
            return None
        info = synced.payload.get("organisation_info")
        if not isinstance(info, dict) or info.get("tenant_id") != connection.tenant_id:
            return None
        engagement = await self.engagements.get(eid)
        if engagement is None:
            return None
        profile = await self.engagements.client_profile(engagement.client_id, engagement.client_profile_version)
        latest = await self.engagements.client_profile(engagement.client_id)
        if profile is None or latest is None:
            return None
        gate = await self.gate(eid) if self.gate is not None else None
        stage: XeroProfileStage = (
            "at" if gate is GateId.ACC_01 else "before" if engagement.phase is Phase.P0_Acceptance else "after"
        )
        legal_name, abn = str(info.get("legal_name") or ""), info.get("abn") or None
        key = check_key(connection.tenant_id, legal_name, abn, profile.version)
        check = XeroProfileCheck(
            key=key,
            xero_name=str(info.get("name") or ""),
            xero_legal_name=legal_name,
            xero_abn=abn,
            profile_version=profile.version,
            profile_legal_name=profile.legal_name,
            profile_abn=profile.abn,
            differences=profile_differences(legal_name, abn, profile),
            checked_at=synced.created_at or self.now(),
            next_version=latest.version + 1,
            stage=stage,
        )
        chosen = await self.trail.last_of(eid, TrailKind.data, RESOLUTION_ACTIONS)
        if chosen is None or chosen.payload.get("key") != key:
            return check
        kept = chosen.payload.get("action") == "xero_profile_kept"
        return check.model_copy(
            update={
                "resolution": "kept" if kept else "updated",
                "reason": chosen.payload.get("reason"),
                "resolved_by": chosen.actor,
                "resolved_at": chosen.created_at,
                "updated_version": None if kept else chosen.payload.get("version"),
                "adopted": None if kept else bool(chosen.payload.get("adopted")),
            }
        )


def sync_result(entry: TrailEntry, now: datetime) -> XeroSyncResult:
    """一条 xero_synced 或 xero_sync_failed 留痕转成的同步结果 (见 StoredXeroConnections.last_sync)."""
    payload = entry.payload
    at = entry.created_at or now
    if payload.get("action") == "xero_sync_failed":
        reason = payload.get("reason")
        if reason not in FAILURES:
            logger.warning("Xero 同步失败留痕的原因代码认不出, 按 unknown 显示: %s %s", entry.engagement_id, reason)
            reason = "unknown"
        return XeroSyncResult(at=at, ok=False, reason=reason)
    kinds = {k.value for k in TableKind}
    tables = [TableKind(k) for k in payload.get("tables", []) if k in kinds]
    skipped: dict[TableKind, XeroSkipReason | Literal["unknown"]] = {}
    for kind, why in dict(payload.get("skipped", {})).items():
        if kind in kinds:
            skipped[TableKind(kind)] = why if why in SKIPS else "unknown"
    if len(tables) != len(payload.get("tables", [])) or len(skipped) != len(payload.get("skipped", {})):
        logger.warning("Xero 同步留痕里有认不出的表, 已跳过: %s", entry.engagement_id)
    return XeroSyncResult(at=at, ok=True, tables=tables, skipped=skipped)


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
        self.connections = StoredXeroConnections(
            store, self.now, deps.trail, lambda eid: data.running(eid, SYNC_JOB), deps.engagement_store, data.gate_now
        )

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
        """同步计划写的表 (202 的 tables, 期间概览中为 converting): 取付费接口 (live) 时七张 (Q7, 含应付账单); 不取
        时没有银行对账单, 银行账户与银行流水都不写. live 下对账单接口 401, 403 时实际也不写这两张 (见 run_sync)."""
        return [k for k in XERO_TABLES if self.paid or k not in BANK_TABLES]

    def skip_reason(self) -> XeroSkipReason:
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
            eid, [k.value for k in labels], lambda: self.run_sync(eid, actor, client, period), prepare, SYNC_JOB
        )
        return labels

    async def run_sync(self, eid: str, actor: str, client: XeroClient, period: Period) -> None:
        """后台的同步作业 (持项目锁): 取数 (只在内存中), 转成标准表, 写一个新数据版本 (write_tables 写完体检), 记下
        同步时间与实际写的表, 写 data 留痕 xero_synced (tables 为实际写的表, skipped 为计划内外没写的表与原因,
        organisation_info 为组织名, 法定名称与 ABN, 供核对客户画像, 见 services.xero_profile).
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
            organisations = files.get("organisation") or [{}]
            organisation = organisation_info(organisations[0])
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
        # 组织信息记下是哪个组织的: 改选组织或换授权后, 核对不拿旧组织的值 (StoredXeroConnections.profile_check)
        organisation["tenant_id"] = connection.tenant_id if connection is not None else None
        summary = f"Xero 同步完成, 写入 {len(kinds)} 张表" + (f", 未写 {', '.join(skipped)}" if skipped else "")
        payload: dict[str, object] = {
            "action": "xero_synced",
            "tables": [k.value for k in kinds],
            "skipped": skipped,
            "organisation_info": organisation,
        }
        await self.record(eid, actor, summary, payload)

    async def sync_failed(
        self, eid: str, actor: str, reason: XeroSyncFailure, status: int | None, *, resources: list[str] | None = None
    ) -> None:
        """同步失败的 data 留痕: 原因代码与状态码, forbidden 另带取不到的资源名."""
        payload: dict[str, object] = {"action": "xero_sync_failed", "reason": reason, "status": status}
        if resources:
            payload["resources"] = resources
        await self.record(eid, actor, f"Xero 同步失败 ({reason})", payload)

    # ── 组织信息与客户画像的核对 (需求 R291) ──

    async def resolve_profile(
        self,
        eid: str,
        key: str,
        decision: XeroProfileDecision,
        reason: str | None,
        actor: str,
        decisions: "GraphDecisionSubmit",
    ) -> XeroProfileCheck | None:
        """审计师对一次核对的选择 (POST /data/xero/profile-check), 返回处理后的核对结果. 核对, 写画像与写留痕都在同一把
        项目锁内 (锁定 423, 忙 409 ENGAGEMENT_BUSY): 没有连接 409 XERO_NOT_CONNECTED; key 不是当前待处理的核对 (Xero
        的值或画像变了, 已处理过, 或本来一致) 409 PROFILE_CHECK_STALE, 并发的两次选择只有一次写成.

        keep: 理由必填 (去首尾空白后为空 422 MISSING_FIELD), 写 data 留痕 xero_profile_kept.
        update: 以该客户最新一版为底, 只改 Xero 与这一版对不上的项 (profile_differences, 名称规范化后相同时保留
        这一版的写法), 其余字段沿用, 来源 xero; 是否提示按本项目采用的版本比 (differences), 改哪些项按合并的底比,
        两版不同时 (本项目采用的版本落后于最新一版) 写出的仍与 Xero 一致. 最新一版已与 Xero 一致时照写一版
        (内容同最新一版). 只有停在 ACC-01 闸门时本项目采用新版本: 走更新客户资料 (decisions.update_client_profile),
        由它取项目锁, 锁内先按 precheck 重新核对 key, 再查闸门与版本, 路由回 p0_inputs, 再到 p0_intel 重跑 ACC-04
        再呈报. 其余时候 (未到 ACC-01 或已过) 在本方法的锁内只给客户写新版本 (下一年度新建项目带出), 本项目照用
        已采用的版本, 不写检查点.
        两条路径的 ABN 校验同 ACC-01 更新客户资料 (需求 R289): 校验位不对 422 INVALID_VALUE (闸门外只在改 ABN 时查),
        改了 ABN 而已是别的客户的 409 ABN_TAKEN; 都不写画像与留痕, 核对仍待处理 (审计师可改为保持原样并写理由).
        写 data 留痕 xero_profile_updated (版本, 本项目是否采用). 留痕写不进时报错 (选择就记在留痕里)."""
        async with self.data.locked(eid):
            engagement = await self.data.unlocked(eid)
            check = await self.pending_check(eid, key)
            payload: dict[str, object] = {
                "key": key,
                "legal_name": check.xero_legal_name,
                "abn": check.xero_abn,
                "profile_version": check.profile_version,
            }
            if decision == "keep":
                text = (reason or "").strip()
                if not text:
                    raise DataRejected("MISSING_FIELD", "reason")
                summary = f"不按 Xero 组织信息更新客户画像, 保持第 {check.profile_version} 版"
                await self.write(eid, actor, summary, {"action": "xero_profile_kept", **payload, "reason": text})
                return await self.current_check(eid)
            latest = await self.deps.engagement_store.client_profile(engagement.client_id)
            if latest is None:
                raise LookupError(f"客户 {engagement.client_id} 没有画像")
            # 合并以最新一版为底, 改哪些项也按它比: 按采用的版本比时, 采用的版本落后的项会沿用最新一版里与 Xero
            # 不同的值. 闸门内 update_client_profile 以通过版本校验的最新一版为底, 版本号按这里读到的最新一版 +1,
            # 期间别处写了新版本即 VERSION_MISMATCH, 合并的底与这里比的是同一版
            fields = {"legal_name": check.xero_legal_name, "abn": check.xero_abn}
            differing = profile_differences(check.xero_legal_name, check.xero_abn, latest)
            changes: dict[str, Any] = {name: fields[name] for name in differing}
            version = latest.version + 1
            adopted = await self.data.gate_now(eid) is GateId.ACC_01
            updated = {"action": "xero_profile_updated", **payload, "version": version, "adopted": adopted}
            summary = f"按 Xero 组织信息更新客户画像至第 {version} 版" + ("" if adopted else ", 本项目仍用已采用的版本")
            if not adopted:
                try:
                    profile = ClientProfile.model_validate({**latest.model_dump(), **changes, "version": version})
                except ValidationError as e:
                    raise DataRejected("INVALID_VALUE", f"按 Xero 合并后的客户画像不合格: {e}") from None
                others = None  # 改了 ABN 时同 ACC-01 更新客户资料: 查校验位, 查重 (库里其他客户与数据集画像)
                if "abn" in changes:
                    problem = abn_problem(profile.abn)
                    if problem is not None:
                        raise DataRejected("INVALID_VALUE", problem)
                    others = dataset_profiles(self.deps.datasets)
                try:
                    await self.deps.engagement_store.save_profile(
                        profile, source=ProfileSource.xero, updated_by=actor, dataset_profiles=others
                    )
                except VersionConflict as e:
                    raise DataRejected("VERSION_MISMATCH", str(e)) from None
                except AbnTaken as e:
                    raise DataRejected("ABN_TAKEN", str(e), client_id=e.client_id, legal_name=e.legal_name) from None
                await self.write(eid, actor, summary, updated)
                return await self.current_check(eid)

        async def recheck() -> None:  # 在 update_client_profile 的项目锁内: 这期间别的选择写成了即 409
            await self.pending_check(eid, key)

        await decisions.update_client_profile(
            eid, version, changes, updated_by=actor, source=ProfileSource.xero, precheck=recheck
        )
        # 新版本已由本项目采用 (画像版本变了, key 随之变): 此后同一 key 的选择都报 409, 留痕在锁外写也不会重复
        await self.write(eid, actor, summary, updated)
        return await self.current_check(eid)

    async def pending_check(self, eid: str, key: str) -> XeroProfileCheck:
        """key 对应的待处理核对; 没有连接 409 XERO_NOT_CONNECTED, 不是当前待处理的 409 PROFILE_CHECK_STALE."""
        check = await self.connections.profile_check(eid, await self.connection(eid))
        if check is None or check.key != key or not check.differences or check.resolution is not None:
            raise DataRejected("PROFILE_CHECK_STALE", "Xero 组织信息的核对已变, 请刷新后再选")
        return check

    async def current_check(self, eid: str) -> XeroProfileCheck | None:
        """选择之后的核对结果 (概览 sources.xero.profile_check 的新值); 连接已不在时为空."""
        connection = await self.store.get(eid)
        return None if connection is None else await self.connections.profile_check(eid, connection)

    async def write(self, eid: str, actor: str, summary: str, payload: dict[str, object]) -> None:
        """一条记下审计师选择的 data 留痕; 写不进时上抛 (与 record 不同)."""
        entry = TrailEntry(
            engagement_id=eid, kind=TrailKind.data, actor=actor, summary=summary, payload=payload, created_at=self.now()
        )
        await self.deps.trail.record(entry)

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
