"""Xero 会计系统接入: OAuth2 授权码授权, 只读拉取 Accounting API, 存成快照.

两种应用类型都支持 (按 Xero 官方指南): 配了 XERO_CLIENT_SECRET 时按 Web app 的标准授权码流程, 换令牌用
Basic base64(client_id:client_secret) 请求头; 没配时按 PKCE (Mobile or desktop app), 用 code verifier 代替 secret.
回调地址须为 https, 测试可用 http://localhost (不能写 127.0.0.1).
访问令牌 30 分钟, 刷新令牌 60 天; 令牌存 XERO_TOKEN_PATH (默认 var/xero/token.json, var/ 不入库), 文件权限 0600.
付费接口 (Journals 只在开发者 Advanced 档开放, Bank Statements Plus 需另外申请) 按 XERO_PAID_ENDPOINTS 取:
live 调真实接口, 失败 (401, 403) 记入快照的 unavailable 不中断拉取; mock 用网页导出模拟 (见 xero_mock); off 不取.

命令行: uv run python -m backend.adapters.xero login [--journals] [--bank-statements] | tenants
        | pull --period-start D --period-end D --prior-end D [--out DIR] [--paid live|mock|off] [--statements-end D]
--statements-end 晚于本期截止日时, 银行对账单多取期后流水 (期后事项, 函证差异调节要用).
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import urllib.parse
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import httpx

from backend.adapters.xero_dataset import Period
from backend.core.config import get_settings

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"  # 授权页
TOKEN_URL = "https://identity.xero.com/connect/token"  # 换取与刷新令牌
CONNECTIONS_URL = "https://api.xero.com/connections"  # 已授权的组织
API_URL = "https://api.xero.com/api.xro/2.0"  # Accounting API
FINANCE_URL = "https://api.xero.com/finance.xro/1.0"  # Finance API (Bank Statements Plus, 需另外申请)
# 只读的细粒度 scope (2026-03 起新应用使用). 付费接口的 scope 另列, 应用未获准时申请会被授权页拒绝 (invalid_scope)
SCOPES = (
    "offline_access",
    "accounting.settings.read",
    "accounting.contacts.read",
    "accounting.invoices.read",
    "accounting.payments.read",
    "accounting.banktransactions.read",
    "accounting.manualjournals.read",
    "accounting.reports.trialbalance.read",
    "accounting.reports.banksummary.read",
)
JOURNALS_SCOPE = "accounting.journals.read"  # Journals 接口, 开发者 Advanced 档
STATEMENTS_SCOPE = "finance.bankstatementsplus.read"  # Bank Statements Plus, 需另外申请
ERROR_FIELDS = ("Type", "Title", "title", "error", "ErrorNumber")  # 错误响应里只取这些短字段, 不取正文 (可能回显令牌)
# 像令牌或 base64 凭据的片段: 连续 16 个以上令牌字符且含数字或 + / = _ (纯字母的错误类型名如 ValidationException 不算)
TOKEN_LIKE = re.compile(r"(?=[A-Za-z0-9+/=_.-]*[0-9+/=_])[A-Za-z0-9+/=_.-]{16,}")


def login_scopes(*, journals: bool = False, statements: bool = False) -> tuple[str, ...]:
    """授权要申请的 scope: 免费的只读 scope, 加上按需开启的付费接口 scope. 启用付费接口后须重新 login."""
    return SCOPES + ((JOURNALS_SCOPE,) if journals else ()) + ((STATEMENTS_SCOPE,) if statements else ())


def error_text(response: httpx.Response, secrets_: tuple[str, ...]) -> str:
    """错误响应的简短说明: 状态码加 JSON 中错误类型或标题的短值. 先在完整值里把凭据换成 ***, 再丢掉仍像令牌的值
    (含连续 16 个以上的令牌字符, 防止只回显了凭据的一部分), 最后截到 80 字; 不取响应正文."""
    try:
        data = response.json()
    except ValueError:
        data = None
    parts = [f"HTTP {response.status_code}"]
    if isinstance(data, dict):
        for key in ERROR_FIELDS:
            value = data.get(key)
            if not isinstance(value, str | int) or value == "":
                continue
            text = str(value)
            for secret in secrets_:
                if secret:
                    text = text.replace(secret, "***")
            if not TOKEN_LIKE.search(text):
                parts.append(text[:80])
    return " ".join(parts)


PAGE_SIZE = 100  # Accounting API 分页接口每页条数, 返回不足一页即为最后一页
MAX_RETRIES = 3  # 429, 5xx 与超时最多重试次数
REFRESH_MARGIN = 60  # 访问令牌剩余不足这么多秒就先刷新
# 分页拉取的资源: (快照名, 接口路径, 响应中的列表键)
PAGED = (
    ("contacts", "Contacts", "Contacts"),
    ("invoices", "Invoices", "Invoices"),
    ("credit_notes", "CreditNotes", "CreditNotes"),
    ("payments", "Payments", "Payments"),
    ("bank_transactions", "BankTransactions", "BankTransactions"),
    ("manual_journals", "ManualJournals", "ManualJournals"),
    ("overpayments", "Overpayments", "Overpayments"),
    ("prepayments", "Prepayments", "Prepayments"),
)
# 不分页的资源
SINGLE = (
    ("organisation", "Organisation", "Organisations"),
    ("accounts", "Accounts", "Accounts"),
    ("bank_transfers", "BankTransfers", "BankTransfers"),
)


class XeroError(RuntimeError):
    """Xero 调用失败: 未授权, 不可重试的状态码, 重试耗尽或响应格式不对. 消息不含令牌."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP 状态码, 连接失败等为空


def code_verifier() -> str:
    """PKCE code verifier: 43-128 位, 字符取 A-Z a-z 0-9 - . _ ~ (token_urlsafe 只出 A-Z a-z 0-9 - _)."""
    return secrets.token_urlsafe(64)[:128]


def code_challenge(verifier: str) -> str:
    """BASE64URL(SHA256(verifier)), 去掉末尾的 =."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str, scopes: tuple[str, ...]) -> str:
    """用户在浏览器打开的授权地址. challenge 为空 (Web app) 时不带 PKCE 参数."""
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    }
    if challenge:
        query |= {"code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}"


@dataclass
class Tokens:
    """令牌文件的内容. tenant_id 为选定的组织, 拉取时作 xero-tenant-id 请求头."""

    access_token: str
    refresh_token: str
    expires_at: float  # 访问令牌到期的 Unix 时间, 秒
    scope: str = ""  # 授权时实际给到的 scope, 空格分隔
    tenant_id: str = ""  # 选定的组织 ID
    tenant_name: str = ""  # 选定的组织名称, 只供显示

    @classmethod
    def from_response(cls, data: dict[str, Any], now: float, previous: "Tokens | None" = None) -> "Tokens":
        """令牌接口的响应转成 Tokens; 刷新时沿用上一份的组织."""
        try:
            access, refresh, expires_in = data["access_token"], data["refresh_token"], data["expires_in"]
        except KeyError as e:
            raise XeroError(f"令牌响应缺字段 {e.args[0]} (授权时须带 offline_access 才有刷新令牌)") from None
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=now + float(expires_in),
            scope=str(data.get("scope", previous.scope if previous else "")),
            tenant_id=previous.tenant_id if previous else "",
            tenant_name=previous.tenant_name if previous else "",
        )


@dataclass
class TokenStore:
    """令牌文件: JSON, 写入时权限 0600, 父目录不存在就建."""

    path: Path

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        return Tokens(**json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, tokens: Tokens) -> None:
        """先写同目录下新建即为 0600 的临时文件 (mkstemp), 再原子替换; 出错时删掉临时文件."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".token-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(tokens.__dict__, stream, indent=2)
            os.replace(name, self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise


@dataclass
class XeroClient:
    """只读的 Xero 客户端: 自动刷新访问令牌, 429 按 Retry-After 等待, 5xx 与超时指数退避, 401 刷新后重试一次."""

    client_id: str
    store: TokenStore
    client_secret: str = field(default="", repr=False)  # Web app 的 secret, 为空时按 PKCE; 不进 repr
    timeout: float = 60.0  # 单次请求超时, 秒
    backoff: float = 1.0  # 第 n 次重试前等待 backoff × 2^(n-1) 秒 (429 有 Retry-After 时按它)
    transport: httpx.AsyncBaseTransport | None = None  # 测试替换为假传输
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # 测试替换为不真等待
    clock: Callable[[], float] = time.time  # 测试替换为固定时间
    calls: int = field(default=0, init=False)  # 已发出的 HTTP 请求数 (含重试), 供核对限流用量

    def __post_init__(self) -> None:
        if not self.client_id:
            raise ValueError("未配置 XERO_CLIENT_ID")

    async def exchange(self, code: str, verifier: str, redirect_uri: str) -> Tokens:
        """授权码换令牌并保存. PKCE 时带 verifier; Web app 时 verifier 传空串, 改用 secret."""
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if verifier:
            form["code_verifier"] = verifier
        tokens = Tokens.from_response(await self._token_request(form), self.clock())
        self.store.save(tokens)
        return tokens

    async def refresh(self, tokens: Tokens) -> Tokens:
        """用刷新令牌换新的一对令牌并保存 (刷新令牌会轮换, 旧的随即失效)."""
        form = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
        fresh = Tokens.from_response(await self._token_request(form), self.clock(), tokens)
        self.store.save(fresh)
        return fresh

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        """令牌接口: Web app 用 Basic 认证带 secret, PKCE 在表单里带 client_id.
        失败时只报状态码与 error 字段, 不带响应原文 (可能含令牌)."""
        self.calls += 1
        headers = {}
        if self.client_secret:
            headers["Authorization"] = f"Basic {self._basic()}"
        else:
            form = {**form, "client_id": self.client_id}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(TOKEN_URL, data=form, headers=headers)
        if response.status_code != 200:
            raise XeroError(f"令牌接口 {error_text(response, self._secrets())}", response.status_code)
        data: dict[str, Any] = response.json()
        return data

    async def _tokens(self) -> Tokens:
        """当前可用的令牌, 快到期就先刷新. 没有令牌文件时提示先 login."""
        tokens = self.store.load()
        if tokens is None:
            raise XeroError(f"没有 Xero 令牌, 先运行 login ({self.store.path})")
        if tokens.expires_at - self.clock() < REFRESH_MARGIN:
            tokens = await self.refresh(tokens)
        return tokens

    async def connections(self) -> list[dict[str, Any]]:
        """当前令牌已授权的组织列表."""
        data = await self._get(CONNECTIONS_URL, None, tenant=False)
        if not isinstance(data, list):
            raise XeroError("connections 响应不是列表")
        return data

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET {API_URL}/{path}, 带组织请求头; 返回响应 JSON 对象."""
        data = await self._get(f"{API_URL}/{path}", params, tenant=True)
        if not isinstance(data, dict):
            raise XeroError(f"{path} 响应不是 JSON 对象")
        return data

    async def paged(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """page=1, 2, ... 依次取, 某页不足 PAGE_SIZE 条即停. 分页时 Invoices 等会带上明细行."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = (await self.get(path, {**(params or {}), "page": page})).get(key) or []
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                return rows
            page += 1

    async def journals(self) -> list[dict[str, Any]]:
        """Journals 按 offset 取 (每次最多 100 笔, offset 取上一批最后的 JournalNumber), 取空即停."""
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = (await self.get("Journals", {"offset": offset})).get("Journals") or []
            if not batch:
                return rows
            rows.extend(batch)
            offset = max(int(j["JournalNumber"]) for j in batch)

    async def bank_statements(self, account_id: str, start: date, end: date) -> dict[str, Any]:
        """Bank Statements Plus: 按不超过 12 个月的区间分段取 (截止日不晚于今天), 各段的对账单合并到一个响应."""
        end = min(end, datetime.fromtimestamp(self.clock(), UTC).date())
        merged: dict[str, Any] | None = None
        while start <= end:
            stop = min(end, date(start.year + 1, start.month, 1) - timedelta(days=1))
            params = {"BankAccountID": account_id, "FromDate": start.isoformat(), "ToDate": stop.isoformat()}
            data = await self._get(f"{FINANCE_URL}/BankStatementsPlus/statements", params, tenant=True)
            if merged is None:
                merged = data
            else:
                merged["statements"].extend(data.get("statements") or [])
            start = stop + timedelta(days=1)
        return merged or {"bankAccountId": account_id, "statements": []}

    async def report(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Reports/{name}, 返回 Reports[0]."""
        reports = (await self.get(f"Reports/{name}", params)).get("Reports") or []
        if not reports:
            raise XeroError(f"报表 {name} 响应为空")
        report: dict[str, Any] = reports[0]
        return report

    async def _get(self, url: str, params: dict[str, Any] | None, *, tenant: bool) -> Any:
        tokens = await self._tokens()
        if tenant and not tokens.tenant_id:
            raise XeroError("还没选定组织, 先运行 tenants 或在 login 时选定")
        refreshed = False
        failure = ""
        attempt = 0
        while attempt <= MAX_RETRIES:
            headers = {"Authorization": f"Bearer {tokens.access_token}", "Accept": "application/json"}
            if tenant:
                headers["xero-tenant-id"] = tokens.tenant_id
            self.calls += 1
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                    response = await client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as e:
                failure = f"超时 ({type(e).__name__})"
                attempt += 1
                await self._wait(attempt, None)
                continue
            except httpx.TransportError as e:
                raise XeroError(f"连接失败: {type(e).__name__}") from None
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise XeroError(f"{url} 响应不是 JSON", status) from None
            if status == 401 and not refreshed:  # 令牌被撤销或提前失效: 刷新一次再试, 不计入重试
                tokens, refreshed = await self.refresh(tokens), True
                continue
            failure = error_text(response, self._secrets(tokens))
            if status != 429 and status < 500:
                raise XeroError(failure, status)
            attempt += 1
            if attempt <= MAX_RETRIES:
                await self._wait(attempt, response.headers.get("Retry-After"))
        raise XeroError(f"重试 {MAX_RETRIES} 次后仍失败: {failure}")

    def _secrets(self, tokens: Tokens | None = None) -> tuple[str, ...]:
        """错误信息里要去掉的凭据: secret, 实际发送的 Basic 凭据, 当前 (及文件中最新) 的访问令牌与刷新令牌."""
        found = [self.client_secret, self._basic()]
        for item in (tokens, self.store.load()):
            if item is not None:
                found += [item.access_token, item.refresh_token]
        return tuple(found)

    def _basic(self) -> str:
        """Web app 换令牌用的 Basic 凭据 base64(client_id:client_secret); 没有 secret 时为空串."""
        if not self.client_secret:
            return ""
        return base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode("ascii")

    async def _wait(self, attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.isdigit():
            await self.sleep(float(retry_after))
        else:
            await self.sleep(self.backoff * 2 ** (attempt - 1))


async def pull(
    client: XeroClient,
    out: Path,
    period: Period,
    *,
    paid: str = "off",
    extra_dates: list[date] | None = None,
    statements_end: date | None = None,
) -> dict[str, Any]:
    """拉取全部只读资源写入 out/{名称}.json, 另写 manifest.json (条数, 不可用的资源与原因) 与 dataset.json (期间).

    试算表取本期截止日, 上期截止日与 extra_dates 各一份. paid 为 live 或 mock 时再取付费接口:
    journals.json 与 bank_statements.json (银行科目 → 本期起始日至 statements_end 的对账单; statements_end 缺省为
    本期截止日, 晚于它时多取期后流水, 供期后事项与函证差异调节用, 写进 dataset.json); mock 时 client.transport 应为
    MockPaidTransport (快照目录用 staging(out)). 付费接口返回 401, 403 时记入 unavailable, 其余资源照常拉取.
    out 已存在时报错, 不覆盖旧快照; 先写到 staging(out), 全部成功后改名为 out, 失败不留下可载入的半成品.
    """
    if out.exists():
        raise XeroError(f"快照目录已存在, 换一个目录: {out}")
    statements_end = statements_end or period.end
    if statements_end < period.end:
        raise XeroError(f"对账单截止日 {statements_end} 早于本期截止日 {period.end}")
    final, out = out, staging(out)
    shutil.rmtree(out, ignore_errors=True)  # 上次失败留下的半成品
    out.mkdir(parents=True)
    tokens = client.store.load()
    manifest: dict[str, Any] = {
        "pulled_at": datetime.now(UTC).isoformat(),
        "tenant_id": tokens.tenant_id if tokens else "",
        "tenant_name": tokens.tenant_name if tokens else "",
        "scope": tokens.scope if tokens else "",
        "paid_endpoints": paid,
        "counts": {},
        "unavailable": {},
    }

    async def save(name: str, fetch: Callable[[], Awaitable[Any]]) -> Any:
        try:
            data = await fetch()
        except XeroError as e:
            if e.status in (401, 403):
                manifest["unavailable"][name] = str(e)
                return None
            raise
        (out / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        manifest["counts"][name] = len(data) if isinstance(data, list) else 1
        return data

    accounts = None
    for name, path, key in SINGLE:
        data = await save(name, partial(_list, client, path, key))
        accounts = data if name == "accounts" else accounts
    for name, path, key in PAGED:
        await save(name, partial(client.paged, path, key))
    for day in sorted({period.end, period.prior_end, *(extra_dates or [])}):
        await save(f"trial_balance_{day.isoformat()}", partial(client.report, "TrialBalance", {"date": day}))
    if paid != "off":
        await save("journals", client.journals)
        banks = [a for a in accounts or [] if a.get("Type") == "BANK"]
        await save("bank_statements", partial(_statements, client, banks, period.start, statements_end))
    meta = {
        "source": "xero",
        "period_start": period.start,
        "period_end": period.end,
        "prior_end": period.prior_end,
        "statements_end": statements_end,
    }
    (out / "dataset.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    manifest["calls"] = client.calls
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    out.rename(final)
    return manifest


def staging(out: Path) -> Path:
    """快照的临时目录: 与 out 同级的隐藏目录, 拉取成功后改名为 out."""
    return out.with_name(f".{out.name}.partial")


async def _statements(client: XeroClient, banks: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    """各银行科目 [start, end] 的对账单, 键为科目代码."""
    return {a["Code"]: await client.bank_statements(a["AccountID"], start, end) for a in banks}


async def _list(client: XeroClient, path: str, key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = (await client.get(path)).get(key) or []
    return rows


def wait_for_code(redirect_uri: str, state: str, timeout: float = 300.0) -> str:
    """在回调地址的端口起一次性 HTTP 服务, 等 Xero 带 code 回跳. state 不符或带 error 时抛 XeroError."""
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        raise XeroError("命令行登录只支持 localhost 回调地址")
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server 的固定方法名
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Xero 授权已收到, 可以关掉这个页面.".encode())

        def log_message(self, format: str, *args: Any) -> None:  # 不把带 code 的请求行打到终端
            return

    server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), Handler)
    server.timeout = timeout
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()
    if "error" in result:
        raise XeroError(f"授权被拒绝或失败: {result['error']}")
    if result.get("state") != state:
        raise XeroError("回调的 state 不符, 已放弃 (可能不是本次发起的授权)")
    if "code" not in result:
        raise XeroError("等待授权回调超时")
    return result["code"]


async def choose_tenant(client: XeroClient, tenant_id: str = "") -> Tokens:
    """在已授权的组织里选定一个写回令牌文件: 指定了 tenant_id 就用它, 否则要求恰好一个组织."""
    organisations = [c for c in await client.connections() if c.get("tenantType") == "ORGANISATION"]
    if tenant_id:
        chosen = [c for c in organisations if c["tenantId"] == tenant_id]
    else:
        chosen = organisations if len(organisations) == 1 else []
    if not chosen:
        names = ", ".join(f"{c['tenantName']} ({c['tenantId']})" for c in organisations) or "无"
        raise XeroError(f"无法确定组织, 请在 .env 设 XERO_TENANT_ID. 已授权的组织: {names}")
    tokens = client.store.load()
    if tokens is None:
        raise XeroError(f"没有 Xero 令牌, 先运行 login ({client.store.path})")
    tokens.tenant_id, tokens.tenant_name = chosen[0]["tenantId"], chosen[0]["tenantName"]
    client.store.save(tokens)
    return tokens


def make_client() -> XeroClient:
    settings = get_settings()
    return XeroClient(settings.XERO_CLIENT_ID, TokenStore(settings.XERO_TOKEN_PATH), settings.XERO_CLIENT_SECRET)


async def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.adapters.xero")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="浏览器授权, 保存令牌并选定组织")
    login.add_argument("--no-browser", action="store_true", help="只打印授权地址, 不自动打开浏览器")
    login.add_argument("--journals", action="store_true", help="另申请付费的 Journals 接口 (应用须已获准)")
    login.add_argument("--bank-statements", action="store_true", help="另申请 Bank Statements Plus (应用须已获准)")
    sub.add_parser("tenants", help="列出已授权的组织并按 XERO_TENANT_ID 选定")
    pull_cmd = sub.add_parser("pull", help="拉取全部只读资源存成快照")
    pull_cmd.add_argument("--out", type=Path, default=None, help="快照目录, 默认 data/external/xero/api/{时间}")
    pull_cmd.add_argument("--period-start", type=date.fromisoformat, required=True, help="本期起始日")
    pull_cmd.add_argument("--period-end", type=date.fromisoformat, required=True, help="本期截止日")
    pull_cmd.add_argument("--prior-end", type=date.fromisoformat, required=True, help="上期截止日")
    pull_cmd.add_argument("--date", type=date.fromisoformat, action="append", default=[], help="另取试算表的日期")
    pull_cmd.add_argument(
        "--statements-end",
        type=date.fromisoformat,
        default=None,
        help="银行对账单取到哪天, 默认本期截止日; 晚于它时含期后流水",
    )
    pull_cmd.add_argument(
        "--paid", choices=("live", "mock", "off"), default=None, help="付费接口, 默认 XERO_PAID_ENDPOINTS"
    )
    pull_cmd.add_argument(
        "--exports", type=Path, default=None, help="mock 用的网页导出目录, 默认 data/external/xero/raw"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    client = make_client()
    if args.command == "login":
        scopes = login_scopes(journals=args.journals, statements=args.bank_statements)
        verifier = "" if client.client_secret else code_verifier()  # Web app 不用 PKCE
        state = secrets.token_urlsafe(16)
        challenge = code_challenge(verifier) if verifier else ""
        url = authorize_url(client.client_id, settings.XERO_REDIRECT_URI, challenge, state, scopes)
        print(f"在浏览器打开并授权 (5 分钟内有效):\n{url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        code = await asyncio.to_thread(wait_for_code, settings.XERO_REDIRECT_URI, state)
        tokens = await client.exchange(code, verifier, settings.XERO_REDIRECT_URI)
        print(f"已保存令牌, 授权 scope: {tokens.scope}")
    if args.command in ("login", "tenants"):
        tokens = await choose_tenant(client, settings.XERO_TENANT_ID)
        print(f"已选定组织: {tokens.tenant_name} ({tokens.tenant_id})")
    if args.command == "pull":
        out = args.out or settings.DATASET_ROOT / "external" / "xero" / "api" / datetime.now().strftime("%Y%m%d-%H%M%S")
        paid = args.paid or settings.XERO_PAID_ENDPOINTS
        if paid == "mock":
            from backend.adapters.xero_mock import MockPaidTransport

            exports = args.exports or settings.DATASET_ROOT / "external" / "xero" / "raw"
            client.transport = MockPaidTransport(exports, staging(out), datetime.now(UTC).date())
        period = Period(args.period_start, args.period_end, args.prior_end)
        manifest = await pull(client, out, period, paid=paid, extra_dates=args.date, statements_end=args.statements_end)
        print(json.dumps({k: manifest[k] for k in ("counts", "unavailable", "calls")}, indent=2, ensure_ascii=False))
        print(f"快照: {out}")


if __name__ == "__main__":
    asyncio.run(_main())
