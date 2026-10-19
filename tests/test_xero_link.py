"""Xero 网页授权 (需求 R279): 连接, 选组织, 同步与断开. 全部用假 Xero (httpx.MockTransport), 不联网.

假 Xero 的 identity 接口 (换令牌, 刷新, 撤销, connections) 一律用假值; Accounting 与付费接口回放快照
data/external/xero/api/20261017-mock. 快照的联系人只经 xero_injection.filtered_contacts 去掉登录账号联系人后在
内存中作答, 不写盘, 不进断言消息. 令牌, 授权码与 client secret 都是可辨认的假值, 密钥扫描据此查找.
"""

import asyncio
import base64
import hashlib
import json
import logging
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from backend.adapters.xero import API_URL, CONNECTIONS_URL, FINANCE_URL, REVOKE_URL, TOKEN_URL, code_challenge
from backend.adapters.xero_dataset import build_tables
from backend.api.routes.integrations import RedactCallbackQuery
from backend.contracts.data import XERO_TABLES, TableKind
from backend.contracts.errors import DataRejected
from backend.core.config import Settings
from backend.services.data_service import DataService
from backend.services.xero_link import CALLBACK_PATH, XeroLink
from tests.fakes import (
    XERO_SNAPSHOT,
    XERO_SNAPSHOTS,
    FakeDataStore,
    FakeEngagementStore,
    FakeTrail,
    FakeXeroConnectionStore,
    api_client,
    make_runner,
    xero_engagement,
    xero_snapshot,
)
from tests.xero_injection import filtered_contacts

CLIENT_ID = "fake-client-id"  # 合成: Xero 应用的 Client ID (不是密钥)
SECRET = "fake-client-secret-R279zz"  # 合成: client secret
CODE = "fake-auth-code-R279qq"  # 合成: 授权码
ACCESS = "fake-access-token-R279"  # 合成: 访问令牌的前缀, 第 n 次换发为 {ACCESS}-{n}
REFRESH = "fake-refresh-token-R279"  # 合成: 刷新令牌的前缀
BASIC = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()  # Web app 换令牌的 Basic 凭据
SECRETS = (SECRET, CODE, ACCESS, REFRESH, BASIC)  # 密钥扫描要找的值 (另加每次授权的 state 与 PKCE verifier)
SCOPE = "offline_access accounting.settings.read accounting.transactions.read"  # 合成: 换令牌响应里的 scope
DEMO = {"tenantId": "3c1332bb-175c-411a-83e8-1b6a809f05d1", "tenantName": "Demo Company (AU)"}  # 快照的组织
OTHER = {"tenantId": "00000000-0000-4000-8000-000000000002", "tenantName": "Synthetic Trading Pty Ltd"}  # 合成
THIRD = {"tenantId": "00000000-0000-4000-8000-000000000003", "tenantName": "Synthetic Holdings Pty Ltd"}  # 合成
FRONT = "http://front.test"  # 合成: 前端地址
START = datetime(2026, 12, 15, 1, 0, tzinfo=UTC)  # 固定时钟: 晚于快照期间截止日, 银行对账单只取一段
API = "/api/v1/engagements/{eid}/data/xero"
EXPECTED_ROWS = {  # 该快照作为数据集载入时各表的行数 (验收 2.3)
    TableKind.accounts: 62,
    TableKind.trial_balance: 38,
    TableKind.journal_entries: 1017,
    TableKind.sales_invoices: 42,
    TableKind.bank_accounts: 2,
    TableKind.bank_transactions: 146,
}


def digest(rows: list[Any]) -> str:
    """一组行的摘要: 比对行内容而不在断言消息里带出行 (行里有往来名称)."""
    return hashlib.sha256(json.dumps([r.model_dump(mode="json") for r in rows], sort_keys=True).encode()).hexdigest()


class FakeXero:
    """假 Xero: 令牌轮换 (旧刷新令牌即失效), 校验 Bearer 与组织请求头, 按快照作答. gate 不为空时 Accounting 请求先等它
    (观察同步进行中的状态). requests 只记 (方法, 路径), 不记请求体."""

    def __init__(self, tenants: tuple[dict[str, str], ...] = (DEMO,)) -> None:
        root = XERO_SNAPSHOTS / XERO_SNAPSHOT
        self.files = {p.stem: p for p in root.glob("*.json") if p.stem != "contacts"}
        self.contacts, _ = filtered_contacts(root / "contacts.json")
        self.tenants = list(tenants)
        self.issued = 0  # 已换发的令牌对数
        self.access = ""  # 当前有效的访问令牌
        self.refresh = ""  # 当前有效的刷新令牌
        self.refresh_error: str | None = None  # 刷新时返回的错误 (如 invalid_grant)
        self.refresh_status = 400  # 刷新出错时的状态码 (Xero 对 invalid_grant 回 400)
        self.token_status: int | None = None  # 令牌接口固定返回的状态码 (如 503); 为空时照常作答
        self.statements_status: int | None = None  # 银行对账单接口 (Finance) 固定返回的状态码 (如 403)
        self.revoke_status = 200  # 撤销接口的状态码
        self.revoked: list[str] = []  # 撤销接口收到的令牌
        self.fail: dict[str, int] = {}  # Accounting 资源 → 固定返回的状态码
        self.echo_on_exchange = False  # 换令牌失败时在错误标题里回显授权码与 secret
        self.gate: asyncio.Event | None = None
        self.requests: list[tuple[str, str]] = []
        self.forms: list[dict[str, list[str]]] = []  # 令牌接口收到的表单 (只在内存中核对)

    def load(self, name: str) -> Any:
        return json.loads(self.files[name].read_text(encoding="utf-8"))

    def issue(self) -> dict[str, Any]:
        self.issued += 1
        self.access, self.refresh = f"{ACCESS}-{self.issued}", f"{REFRESH}-{self.issued}"
        return {
            "access_token": self.access,
            "refresh_token": self.refresh,
            "expires_in": 1800,
            "scope": SCOPE,
        }

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?", 1)[0]
        self.requests.append((request.method, url))
        if request.method == "POST":
            form = urllib.parse.parse_qs(request.content.decode())
            self.forms.append(form)
            basic = request.headers.get("Authorization") == f"Basic {BASIC}"
            pkce = "Authorization" not in request.headers and form.get("client_id") == [CLIENT_ID]
            if form.get("grant_type") == ["authorization_code"] and pkce and "code_verifier" not in form:
                pkce = False  # PKCE 换令牌必须带 verifier
            if not (basic or pkce):
                return httpx.Response(401, json={"error": "invalid_client"})
            if self.token_status is not None:
                return httpx.Response(self.token_status, json={"Title": "Unavailable"})
            if url == TOKEN_URL:
                return self.token(form)
            if url == REVOKE_URL:
                self.revoked.append(form["token"][0])
                return httpx.Response(self.revoke_status)
        if request.headers.get("Authorization") != f"Bearer {self.access}":
            return httpx.Response(401, json={"Title": "Unauthorized"})
        if url == CONNECTIONS_URL:
            return httpx.Response(200, json=[{**t, "tenantType": "ORGANISATION"} for t in self.tenants])
        if request.headers.get("xero-tenant-id") != DEMO["tenantId"]:
            return httpx.Response(403, json={"Title": "Forbidden"})
        if self.gate is not None:
            await self.gate.wait()
        if url.startswith(FINANCE_URL):
            if self.statements_status is not None:
                return httpx.Response(self.statements_status, json={"Title": "Forbidden"})
            return self.statements(request)
        path = url.removeprefix(API_URL + "/")
        if path in self.fail:
            return httpx.Response(self.fail[path], json={"Title": "Unavailable"})
        return httpx.Response(200, json=self.accounting(path, request.url.params))

    def token(self, form: dict[str, list[str]]) -> httpx.Response:
        grant = form["grant_type"][0]
        if grant == "authorization_code":
            if form.get("code") != [CODE]:
                title = f"Rejected {CODE} {SECRET}" if self.echo_on_exchange else "Rejected"
                return httpx.Response(400, json={"error": "invalid_grant", "Title": title})
            return httpx.Response(200, json=self.issue())
        if self.refresh_error or form.get("refresh_token") != [self.refresh]:
            return httpx.Response(self.refresh_status, json={"error": self.refresh_error or "invalid_grant"})
        return httpx.Response(200, json=self.issue())

    def accounting(self, path: str, params: httpx.QueryParams) -> dict[str, Any]:
        single = {"Organisation": ("organisation", "Organisations"), "Accounts": ("accounts", "Accounts")}
        single["BankTransfers"] = ("bank_transfers", "BankTransfers")
        if path in single:
            name, key = single[path]
            return {key: self.load(name)}
        if path == "Reports/TrialBalance":
            return {"Reports": [self.load(f"trial_balance_{params['date']}")]}
        if path == "Journals":
            offset = int(params["offset"])
            rows = sorted(
                (j for j in self.load("journals") if j["JournalNumber"] > offset), key=lambda j: j["JournalNumber"]
            )
            return {"Journals": rows[:100]}
        names = {
            "Invoices": "invoices",
            "CreditNotes": "credit_notes",
            "Payments": "payments",
            "BankTransactions": "bank_transactions",
            "ManualJournals": "manual_journals",
            "Overpayments": "overpayments",
            "Prepayments": "prepayments",
        }
        rows = self.contacts if path == "Contacts" else self.load(names[path])
        page = int(params["page"])
        return {path: rows[(page - 1) * 100 : page * 100]}

    def statements(self, request: httpx.Request) -> httpx.Response:
        codes = {a["AccountID"]: a["Code"] for a in self.load("accounts") if a.get("Type") == "BANK"}
        code = codes[request.url.params["BankAccountID"]]
        return httpx.Response(200, json=self.load("bank_statements")[code])


@dataclass
class Case:
    """一个 Xero 快照期间的项目, 假存储与假 Xero 上的 XeroLink."""

    xero: XeroLink
    fake: FakeXero
    store: FakeXeroConnectionStore
    data: FakeDataStore
    trail: FakeTrail
    eid: str
    now: list[datetime] = field(default_factory=lambda: [START])
    checkups: list[int] = field(default_factory=list)

    def advance(self, delta: timedelta) -> None:
        self.now[0] += delta

    def actions(self) -> list[Any]:
        return [e.payload.get("action") for e in self.trail.entries if e.payload.get("action", "").startswith("xero")]

    async def drain(self) -> None:
        await asyncio.gather(*list(self.xero.data.tasks))


def settings(tmp_path: Path, **changes: Any) -> Settings:
    """测试配置: 不读 .env (_env_file=None), 一律假值; 命令行令牌文件放 tmp_path."""
    values: dict[str, Any] = {
        "XERO_CLIENT_ID": CLIENT_ID,
        "XERO_CLIENT_SECRET": SECRET,
        "XERO_PAID_ENDPOINTS": "live",
        "SETTINGS_SECRET_KEY": Fernet.generate_key().decode(),
        "FRONTEND_URL": FRONT,
        "XERO_TOKEN_PATH": tmp_path / "cli" / "token.json",
    }
    return Settings(_env_file=None, **(values | changes))  # type: ignore[call-arg]


def case(tmp_path: Path, tenants: tuple[dict[str, str], ...] = (DEMO,), **changes: Any) -> Case:
    profile, engagement = xero_engagement(xero_snapshot())
    data = FakeDataStore()
    trail = FakeTrail()
    runner, _ = make_runner(engagement_store=FakeEngagementStore([profile], [engagement]), data=data, trail=trail)
    fake = FakeXero(tenants)
    store = FakeXeroConnectionStore()
    now = [START]

    async def no_sleep(seconds: float) -> None:
        return None

    service = DataService(runner.deps, runner)
    link = XeroLink(
        runner.deps,
        service,
        store,
        settings(tmp_path, **changes),
        transport=httpx.MockTransport(fake),
        clock=lambda: now[0].timestamp(),
        sleep=no_sleep,
    )
    service.xero = link.connections
    result = Case(link, fake, store, data, trail, engagement.id, now)
    original = data.save_check_run

    async def counted(eid: str, run: Any, requests: Any) -> None:
        result.checkups.append(1)
        await original(eid, run, requests)

    data.save_check_run = counted  # type: ignore[method-assign]
    return result


@asynccontextmanager
async def client(c: Case) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(c.xero.data.runner, xero=c.xero) as http:
        yield http


async def authorize(http: httpx.AsyncClient, c: Case) -> str:
    """GET /connect, 返回授权地址里的 state."""
    response = await http.get(API.format(eid=c.eid) + "/connect")
    assert response.status_code == 200, response.text
    return urllib.parse.parse_qs(urllib.parse.urlparse(response.json()["authorize_url"]).query)["state"][0]


async def callback(http: httpx.AsyncClient, **query: str) -> str:
    """浏览器回到回调地址, 返回 302 的 Location."""
    response = await http.get(CALLBACK_PATH, params=query)
    assert response.status_code == 302, response.text
    return response.headers["location"]


async def connect(http: httpx.AsyncClient, c: Case) -> str:
    return await callback(http, code=CODE, state=await authorize(http, c))


async def xero_source(http: httpx.AsyncClient, c: Case) -> dict[str, Any]:
    response = await http.get(f"/api/v1/engagements/{c.eid}/data/summary")
    assert response.status_code == 200, response.text
    source: dict[str, Any] = response.json()["sources"]["xero"]
    return source


def error_page(c: Case, reason: str) -> str:
    return f"{FRONT}/engagements/{c.eid}/data?xero=error&reason={reason}"


# ── 验收 2.1, 2.2: 授权地址与回调 ──


async def test_connect_returns_web_app_authorize_url_with_state(tmp_path: Path) -> None:
    """有 client secret 时按 Web app (不带 PKCE); scope 含 offline_access, 付费接口为 live 时另申请 Journals 与
    Bank Statements Plus; 回调地址为后端回调 (XERO_REDIRECT_URI 缺省值)."""
    c = case(tmp_path)
    async with client(c) as http:
        response = await http.get(API.format(eid=c.eid) + "/connect")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(response.json()["authorize_url"]).query)
    assert query["client_id"] == [CLIENT_ID] and query["redirect_uri"] == [
        "http://localhost:8000/api/v1/integrations/xero/callback"
    ]
    scopes = query["scope"][0].split()
    assert {"offline_access", "accounting.journals.read", "finance.bankstatementsplus.read"} <= set(scopes)
    assert "code_challenge" not in query and len(query["state"][0]) >= 32


@pytest.mark.parametrize(("key", "code"), [("", "SETTINGS_KEY_MISSING"), ("not-a-fernet-key", "SETTINGS_KEY_INVALID")])
async def test_connect_refused_without_usable_settings_key(tmp_path: Path, key: str, code: str) -> None:
    """主密钥没配或不对时 connect 当场报 503 (不等回调后才发现令牌存不了), 不登记 state."""
    c = case(tmp_path, SETTINGS_SECRET_KEY=key)
    async with client(c) as http:
        response = await http.get(API.format(eid=c.eid) + "/connect")
    assert (response.status_code, response.json()["detail"]["code"]) == (503, code)
    assert c.xero.states.pending == {}


async def test_pkce_when_no_client_secret(tmp_path: Path) -> None:
    """没配 secret 时按 PKCE: 授权地址带 code_challenge, 换令牌时不带 Basic, 表单带 client_id 与 verifier, verifier
    与授权地址的 challenge 对得上; 连接成功."""
    c = case(tmp_path, XERO_CLIENT_SECRET="")
    async with client(c) as http:
        response = await http.get(API.format(eid=c.eid) + "/connect")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(response.json()["authorize_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        location = await callback(http, code=CODE, state=query["state"][0])
    assert location == f"{FRONT}/engagements/{c.eid}/data?xero=connected"
    form = c.fake.forms[0]
    assert form["client_id"] == [CLIENT_ID] and code_challenge(form["code_verifier"][0]) == query["code_challenge"][0]


async def test_callback_rejects_bad_expired_reused_state_and_denial(tmp_path: Path) -> None:
    """验收 2.1: 不认识的 state 回前端首页, 拒绝授权, 过期 (超过 10 分钟) 与重复使用的回该项目的数据管理页, 都带
    xero=error 与原因代码; 这些情形都不存连接."""
    c = case(tmp_path)
    async with client(c) as http:
        assert await callback(http, code=CODE, state="not-issued") == f"{FRONT}/?xero=error&reason=state_invalid"
        assert await callback(http, code=CODE) == f"{FRONT}/?xero=error&reason=state_invalid"
        denied = await authorize(http, c)
        assert await callback(http, error="access_denied", state=denied) == error_page(c, "denied")
        stale = await authorize(http, c)
        c.advance(timedelta(minutes=11))
        assert await callback(http, code=CODE, state=stale) == error_page(c, "state_expired")
        assert c.store.rows == {} and c.fake.issued == 0
        state = await authorize(http, c)
        assert await callback(http, code=CODE, state=state) == f"{FRONT}/engagements/{c.eid}/data?xero=connected"
        assert await callback(http, code=CODE, state=state) == error_page(c, "state_used")
        assert await callback(http, code=CODE, state=denied) == error_page(c, "state_used")
    assert c.fake.issued == 1


async def test_callback_saves_connection_and_overview_connected(tmp_path: Path) -> None:
    """验收 2.2: 正确回调后保存连接 (令牌只存密文), 概览 sources.xero 为 connected, 组织名 Demo Company (AU);
    tenants 返回授权给到的组织; 写 data 留痕 xero_connect."""
    c = case(tmp_path)
    async with client(c) as http:
        assert await xero_source(http, c) == {
            "status": "not_connected",
            "organisation": None,
            "last_synced_at": None,
            "scope": [],
        }
        await connect(http, c)
        source = await xero_source(http, c)
        tenants = (await http.get(API.format(eid=c.eid) + "/tenants")).json()
    assert (source["status"], source["organisation"]) == ("connected", "Demo Company (AU)")
    assert tenants == [{"id": DEMO["tenantId"], "name": DEMO["tenantName"]}]
    connection, sealed = c.store.rows[c.eid]
    assert (connection.tenant_id, connection.connected_by, connection.scope) == (DEMO["tenantId"], "U-1", SCOPE)
    assert ACCESS not in sealed and REFRESH not in sealed
    assert c.actions() == ["xero_connect"]


async def test_callback_reports_locked_busy_and_no_organisation(tmp_path: Path) -> None:
    """回调时项目忙 (另一写操作持锁) 报 busy, 已锁定报 locked, 授权里没有组织报 no_organisation, 换令牌失败报
    exchange_failed, 主密钥没配报 settings_key; 项目不存在了报 not_found (跳前端首页). 这些情形都不存连接."""
    c = case(tmp_path)
    engagements = c.xero.deps.engagement_store
    assert isinstance(engagements, FakeEngagementStore)
    original = list(engagements.engagements)
    async with client(c) as http:
        state = await authorize(http, c)
        c.xero.data.runner.acquire(c.eid)
        try:
            assert await callback(http, code=CODE, state=state) == error_page(c, "busy")
        finally:
            c.xero.data.runner.release(c.eid)
        state = await authorize(http, c)
        engagements.engagements = [e.model_copy(update={"locked": True}) for e in original]
        assert await callback(http, code=CODE, state=state) == error_page(c, "locked")
        engagements.engagements = list(original)
        state = await authorize(http, c)
        engagements.engagements = []
        assert await callback(http, code=CODE, state=state) == f"{FRONT}/?xero=error&reason=not_found"
        engagements.engagements = list(original)
        c.fake.tenants = []
        assert await callback(http, code=CODE, state=await authorize(http, c)) == error_page(c, "no_organisation")
        c.fake.tenants = [DEMO]
        assert await callback(http, code="wrong", state=await authorize(http, c)) == error_page(c, "exchange_failed")
        state = await authorize(http, c)
        c.xero.settings = settings(tmp_path, SETTINGS_SECRET_KEY="")  # 授权之后主密钥没了
        assert await callback(http, code=CODE, state=state) == error_page(c, "settings_key")
    assert c.store.rows == {}


async def test_callback_unexpected_error_still_redirects(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回调时意外出错 (如存库失败): 不让浏览器停在后端的 500 页, 照样 302 回该项目的数据管理页, 原因
    internal_error; 日志只记异常类型, 不记消息."""
    c = case(tmp_path)

    async def broken(eid: str, connection: Any, tokens_encrypted: str) -> None:
        raise OSError("disk full at /var/lib/synthetic")  # 合成的消息, 核对它不进日志

    monkeypatch.setattr(c.store, "put", broken)
    async with client(c) as http:
        location = await callback(http, code=CODE, state=await authorize(http, c))
    assert location == error_page(c, "internal_error")
    messages = [r.getMessage() for r in caplog.records if r.name == "backend.services.xero_link"]
    assert any("OSError" in m for m in messages) and not any("disk full" in m for m in messages)
    assert not c.xero.data.runner.is_running(c.eid)


# ── 验收 2.3: 同步 ──


async def test_sync_writes_six_tables_like_dataset_load(tmp_path: Path) -> None:
    """验收 2.3: 期间 2026-07-01 至 11-30 的项目同步后, 六张表的行与该快照作为数据集载入时一致 (行数 62, 38, 1017,
    42, 2, 146); 数据版本加 1, 体检只跑一次, Xero 不提供录入人与审批人, 提出补交审批日志. 发运单不写. 同步期间这些表为
    converting, processing 计数; 再同步或导入报 ENGAGEMENT_BUSY."""
    c = case(tmp_path)
    expected = build_tables(xero_snapshot())
    before = c.data.data_version
    async with client(c) as http:
        await connect(http, c)
        c.fake.gate = asyncio.Event()
        response = await http.post(API.format(eid=c.eid) + "/sync")
        assert (response.status_code, response.json()) == (202, {"tables": [k.value for k in XERO_TABLES]})
        await asyncio.sleep(0)
        summary = (await http.get(f"/api/v1/engagements/{c.eid}/data/summary")).json()
        converting = [t["kind"] for t in summary["tables"] if t["status"] == "converting"]
        assert (converting, summary["processing"]) == ([k.value for k in XERO_TABLES], 6)
        again = await http.post(API.format(eid=c.eid) + "/sync")
        upload = await http.post(
            f"/api/v1/engagements/{c.eid}/data/imports", files={"files": ("accounts.csv", b"code,name\n1,Cash\n")}
        )
        tenant = await http.put(API.format(eid=c.eid) + "/tenant", json={"tenant_id": DEMO["tenantId"]})
        codes = [again.json()["detail"]["code"], upload.json()["detail"]["code"], tenant.json()["detail"]["code"]]
        assert codes == ["ENGAGEMENT_BUSY"] * 3 and tenant.status_code == 409
        c.fake.gate.set()
        await c.drain()
        source = await xero_source(http, c)
    written, options = c.data.written[-1]
    assert {k: len(v) for k, v in written.items()} == EXPECTED_ROWS
    assert all(digest(list(written[k])) == digest(expected[k.value]) for k in XERO_TABLES)
    assert options["source"] == "xero" and options["actor"] == "U-1"
    assert (c.data.data_version, len(c.checkups)) == (before + 1, 1)
    assert "REQ-approval_log" in c.data.data_requests
    assert source["scope"] == [k.value for k in XERO_TABLES] and source["last_synced_at"] is not None
    assert [e.payload["table_kind"] for e in c.trail.entries if e.payload.get("action") == "sync"] == [
        k.value for k in XERO_TABLES
    ]
    [done] = [e for e in c.trail.entries if e.payload.get("action") == "xero_synced"]
    assert done.payload == {"action": "xero_synced", "tables": [k.value for k in XERO_TABLES], "skipped": {}}


BANK = (TableKind.bank_accounts, TableKind.bank_transactions)
NOT_BANK = [k for k in XERO_TABLES if k not in BANK]


def bank_state(c: Case) -> Any:
    """已有的两张银行表: 行数与版本 (同步不写它们时都不变)."""
    return [(c.data.table_stats.get(k), [v for v in c.data.table_versions if v.kind is k]) for k in BANK]


@pytest.mark.parametrize("mode", ["off", "mock"])
async def test_sync_without_paid_endpoints_keeps_imported_bank_tables(tmp_path: Path, mode: str) -> None:
    """付费接口 off 与 mock 时 (Q5, mock 在网页同步里同 off) 授权不申请付费 scope, 同步不取 Journals 与对账单:
    总账按单据反推并补齐; 没有对账单, 银行账户与银行流水这一对都不写 (只写账户, 已导入流水的账户 ID 可能对不上;
    也不拿空表盖掉已导入的流水), 已有的两张银行表保留. 202 的计划写的表为四张; 留痕 xero_synced 写明这两张没写的
    原因 paid_off; 同步期间概览也只把这四张显示为 converting."""
    c = case(tmp_path, XERO_PAID_ENDPOINTS=mode)
    before = bank_state(c)
    async with client(c) as http:
        response = await http.get(API.format(eid=c.eid) + "/connect")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(response.json()["authorize_url"]).query)
        scopes = set(query["scope"][0].split())
        assert not scopes & {"accounting.journals.read", "finance.bankstatementsplus.read"}
        await callback(http, code=CODE, state=query["state"][0])
        c.fake.gate = asyncio.Event()
        started = await http.post(API.format(eid=c.eid) + "/sync")
        await asyncio.sleep(0)
        summary = (await http.get(f"/api/v1/engagements/{c.eid}/data/summary")).json()
        converting = [t["kind"] for t in summary["tables"] if t["status"] == "converting"]
        assert (converting, summary["processing"]) == ([k.value for k in NOT_BANK], 4)
        c.fake.gate.set()
        await c.drain()
        source = await xero_source(http, c)
    assert started.json()["tables"] == [k.value for k in NOT_BANK]
    written, _ = c.data.written[-1]
    assert list(written) == NOT_BANK and len(written[TableKind.accounts]) == 62
    assert source["scope"] == [k.value for k in NOT_BANK] and bank_state(c) == before
    assert not [u for _, u in c.fake.requests if u.endswith("/2.0/Journals") or u.startswith(FINANCE_URL)]
    [done] = [e for e in c.trail.entries if e.payload.get("action") == "xero_synced"]
    assert done.payload["tables"] == [k.value for k in NOT_BANK]
    assert done.payload["skipped"] == {k.value: "paid_off" for k in BANK}


async def test_live_statements_forbidden_skips_bank_pair(tmp_path: Path) -> None:
    """live 下对账单接口返回 403: 202 的计划写的表照列六张, 实际只写四张, 银行账户与银行流水都不写, 已有的两张银行
    表保留; 概览 scope 照实列出写了的表; 总账仍按 Journals 取 (1017 行); 留痕 xero_synced 写明原因
    statements_forbidden."""
    c = case(tmp_path)
    before = bank_state(c)
    async with client(c) as http:
        await connect(http, c)
        c.fake.statements_status = 403
        started = await http.post(API.format(eid=c.eid) + "/sync")
        await c.drain()
        source = await xero_source(http, c)
    assert started.json()["tables"] == [k.value for k in XERO_TABLES]
    written, _ = c.data.written[-1]
    assert (
        list(written) == NOT_BANK
        and len(written[TableKind.journal_entries]) == EXPECTED_ROWS[TableKind.journal_entries]
    )
    assert source["scope"] == [k.value for k in NOT_BANK] and bank_state(c) == before
    [done] = [e for e in c.trail.entries if e.payload.get("action") == "xero_synced"]
    assert done.payload["tables"] == [k.value for k in NOT_BANK]
    assert done.payload["skipped"] == {k.value: "statements_forbidden" for k in BANK}


async def test_live_journals_forbidden_rebuilds_ledger_from_documents(tmp_path: Path) -> None:
    """live 下只有 Journals 返回 403 (应用开通了对账单而没有 Journals): 同步照常, 总账按单据反推并补齐 (行数不是
    Journals 的 1017), 银行这一对按对账单照写, 六张都写; 不记同步失败, xero_synced 的 skipped 为空."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        c.fake.fail["Journals"] = 403
        started = await http.post(API.format(eid=c.eid) + "/sync")
        await c.drain()
        source = await xero_source(http, c)
    assert started.json()["tables"] == [k.value for k in XERO_TABLES]
    assert "xero_sync_failed" not in c.actions()
    written, _ = c.data.written[-1]
    assert list(written) == list(XERO_TABLES)
    assert 0 < len(written[TableKind.journal_entries]) != EXPECTED_ROWS[TableKind.journal_entries]
    assert {k: len(written[k]) for k in BANK} == {k: EXPECTED_ROWS[k] for k in BANK}
    assert source["scope"] == [k.value for k in XERO_TABLES]
    [done] = [e for e in c.trail.entries if e.payload.get("action") == "xero_synced"]
    assert done.payload == {"action": "xero_synced", "tables": [k.value for k in XERO_TABLES], "skipped": {}}


@pytest.mark.parametrize("refresh_status", [400, 401])
async def test_background_invalid_grant_marks_expired(tmp_path: Path, refresh_status: int) -> None:
    """起作业时令牌有效, 后台取数时某资源返回 401, 再刷新得 invalid_grant: 连接置 expired, 留痕 xero_sync_failed 的
    reason 为 expired, 不写数据, 锁已放. 令牌接口对刷新回 401 (而不是 Xero 实际的 400) 时也一样, 不当成资源无权限
    (forbidden)."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        c.fake.fail["Invoices"] = 401
        c.fake.refresh_error = "invalid_grant"
        c.fake.refresh_status = refresh_status
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
        source = await xero_source(http, c)
    assert source["status"] == "expired" and c.store.rows[c.eid][0].status == "expired"
    [failed] = [e for e in c.trail.entries if e.payload.get("action") == "xero_sync_failed"]
    assert failed.payload["reason"] == "expired"
    assert c.data.written == [] and not c.xero.data.runner.is_running(c.eid)


async def test_free_resource_forbidden_fails_sync(tmp_path: Path) -> None:
    """付费接口以外的资源 (这里是发票) 返回 403: 不拿空表写入, 同步失败, 留痕 reason 为 forbidden 并带资源名."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        c.fake.fail["Invoices"] = 403
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
    [failed] = [e for e in c.trail.entries if e.payload.get("action") == "xero_sync_failed"]
    assert failed.payload == {
        "action": "xero_sync_failed",
        "reason": "forbidden",
        "status": None,
        "resources": ["invoices"],
    }
    assert c.data.written == [] and not c.xero.data.runner.is_running(c.eid)


async def test_unexpected_errors_in_sync_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """后台同步中的意外错误也写 xero_sync_failed: 转换时出了别的异常记 mapping (不写数据); 取数时主密钥不可用记
    settings_key; 写库失败记 write, 再交给后台任务记日志. 各情形锁都放了."""
    c = case(tmp_path)

    def broken(snapshot: Any) -> Any:
        raise AttributeError("synthetic conversion bug")

    async def keyless(*args: Any, **kwargs: Any) -> Any:
        raise DataRejected("SETTINGS_KEY_INVALID", "合成")

    async with client(c) as http:
        await connect(http, c)
        monkeypatch.setattr("backend.services.xero_link.build_tables", broken)
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
        monkeypatch.undo()
        monkeypatch.setattr("backend.services.xero_link.fetch", keyless)
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
        monkeypatch.undo()

        async def failing(*args: Any, **kwargs: Any) -> int:
            raise OSError("synthetic write failure")

        monkeypatch.setattr(c.data, "write_tables", failing)
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
    reasons = [e.payload["reason"] for e in c.trail.entries if e.payload.get("action") == "xero_sync_failed"]
    assert reasons == ["mapping", "settings_key", "write"] and c.data.written == []
    assert any(r.exc_info and r.exc_info[0] is OSError for r in caplog.records)
    logged = [r.getMessage() for r in caplog.records if r.name == "backend.services.xero_link"]
    assert any("AttributeError" in m for m in logged)  # 转换兜底只记异常类型, 消息可能含往来名称
    assert not any("synthetic conversion bug" in m for m in logged)
    assert not c.xero.data.runner.is_running(c.eid)


async def test_sync_failure_writes_nothing_and_records_trail(tmp_path: Path) -> None:
    """Xero 一直报 5xx: 重试耗尽后不写数据, 写 data 留痕 xero_sync_failed (只有原因代码与状态码), 锁随之释放."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        c.fake.fail["Invoices"] = 503
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
    assert c.data.written == [] and not c.xero.data.runner.is_running(c.eid)
    [failed] = [e for e in c.trail.entries if e.payload.get("action") == "xero_sync_failed"]
    assert failed.payload == {"action": "xero_sync_failed", "reason": "xero_error", "status": None}


# ── 验收 2.4: 刷新令牌失效 ──


async def test_invalid_grant_marks_expired_and_sync_rejected(tmp_path: Path) -> None:
    """验收 2.4: 访问令牌快到期, 刷新时假服务器返回 invalid_grant: 同步当场被拒 (409 XERO_RECONNECT_REQUIRED, 提示
    重新连接), 状态变 expired, 不起作业; 重新连接后恢复 connected."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        c.advance(timedelta(minutes=29, seconds=30))  # 访问令牌剩不到 60 秒, 同步前先刷新
        c.fake.refresh_error = "invalid_grant"
        response = await http.post(API.format(eid=c.eid) + "/sync")
        assert (response.status_code, response.json()["detail"]["code"]) == (409, "XERO_RECONNECT_REQUIRED")
        assert (await xero_source(http, c))["status"] == "expired"
        assert (await http.post(API.format(eid=c.eid) + "/sync")).json()["detail"]["code"] == "XERO_RECONNECT_REQUIRED"
        c.fake.refresh_error = None
        await connect(http, c)
        assert (await xero_source(http, c))["status"] == "connected"
    assert c.data.written == [] and not c.xero.data.runner.is_running(c.eid)


async def test_token_endpoint_down_before_sync_is_502(tmp_path: Path) -> None:
    """起作业前要刷新访问令牌, 令牌接口返回 503 (不是 invalid_grant): 同步当场报 502 XERO_UNAVAILABLE, 不起作业,
    连接仍为 connected, 锁已放."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        c.advance(timedelta(minutes=29, seconds=30))
        c.fake.token_status = 503
        response = await http.post(API.format(eid=c.eid) + "/sync")
        source = await xero_source(http, c)
    assert (response.status_code, response.json()["detail"]["code"]) == (502, "XERO_UNAVAILABLE")
    assert source["status"] == "connected" and c.data.written == [] and not c.xero.data.runner.is_running(c.eid)


async def test_refresh_token_older_than_sixty_days_is_expired(tmp_path: Path) -> None:
    """刷新令牌换得超过 60 天: 概览为 expired, 同步被拒; 刷新过 (令牌轮换) 的从刷新时起算, 新令牌加密写库."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        c.advance(timedelta(days=59))
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202  # 访问令牌早已过期: 先刷新
        await c.drain()
        assert ("set_tokens", c.eid) in c.store.calls and c.fake.issued == 2
        c.advance(timedelta(days=59))
        assert (await xero_source(http, c))["status"] == "connected"
        c.advance(timedelta(days=2))
        assert (await xero_source(http, c))["status"] == "expired"
        response = await http.post(API.format(eid=c.eid) + "/sync")
    assert response.json()["detail"]["code"] == "XERO_RECONNECT_REQUIRED"


# ── 验收 2.5: 断开 ──


async def test_disconnect_revokes_deletes_tokens_and_keeps_data(tmp_path: Path) -> None:
    """验收 2.5: 断开时调了撤销接口 (带当前的刷新令牌), 令牌与连接已删, 已同步的数据与版本保留, 概览回到
    not_connected; 撤销失败也删本地令牌, revoked 为 false. 锁定的项目也能断开."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        await http.post(API.format(eid=c.eid) + "/sync")
        await c.drain()
        version, written = c.data.data_version, len(c.data.table_versions)
        current = c.fake.refresh
        response = await http.delete(API.format(eid=c.eid))
        assert (response.status_code, response.json()) == (200, {"revoked": True})
        assert c.fake.revoked == [current] and c.store.rows == {}
        assert (await xero_source(http, c))["status"] == "not_connected"
        assert (c.data.data_version, len(c.data.table_versions)) == (version, written)
        assert (await http.delete(API.format(eid=c.eid))).json()["detail"]["code"] == "XERO_NOT_CONNECTED"
        await connect(http, c)
        c.fake.revoke_status = 503
        engagements = c.xero.deps.engagement_store
        assert isinstance(engagements, FakeEngagementStore)
        engagements.engagements = [e.model_copy(update={"locked": True}) for e in engagements.engagements]
        assert (await http.get(API.format(eid=c.eid) + "/connect")).status_code == 423
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 423
        assert (await http.delete(API.format(eid=c.eid))).json() == {"revoked": False}
    assert c.store.rows == {}
    revoked = [e.payload["revoked"] for e in c.trail.entries if e.payload.get("action") == "xero_disconnect"]
    assert revoked == [True, False]


# ── 验收 2.6: 两个组织 ──


async def test_two_organisations_require_choice_before_sync(tmp_path: Path) -> None:
    """验收 2.6: 授权给到两个组织时不自动选, 概览组织名为空; 同步被拒 (409 XERO_TENANT_REQUIRED); tenants 列出两个;
    选不在列表里的 422; 选定后概览显示组织名, 同步正常. 选定写 data 留痕 xero_tenant."""
    c = case(tmp_path, tenants=(OTHER, DEMO))
    async with client(c) as http:
        await connect(http, c)
        source = await xero_source(http, c)
        assert (source["status"], source["organisation"]) == ("connected", None)
        response = await http.post(API.format(eid=c.eid) + "/sync")
        assert (response.status_code, response.json()["detail"]["code"]) == (409, "XERO_TENANT_REQUIRED")
        tenants = (await http.get(API.format(eid=c.eid) + "/tenants")).json()
        assert [t["name"] for t in tenants] == [OTHER["tenantName"], DEMO["tenantName"]]
        wrong = await http.put(API.format(eid=c.eid) + "/tenant", json={"tenant_id": "not-authorised"})
        assert (wrong.status_code, wrong.json()["detail"]["code"]) == (422, "INVALID_VALUE")
        chosen = await http.put(API.format(eid=c.eid) + "/tenant", json={"tenant_id": DEMO["tenantId"]})
        assert chosen.json()["organisation"] == DEMO["tenantName"]
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
    assert {k: len(v) for k, v in c.data.written[-1][0].items()} == EXPECTED_ROWS
    assert c.actions()[:2] == ["xero_connect", "xero_tenant"]


# ── 验收 2.7: 密钥扫描 ──


@pytest.mark.parametrize("mode", ["web", "pkce"])
async def test_tokens_code_and_secret_never_leak(tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str) -> None:
    """验收 2.7: Web app 与 PKCE 各走一遍连接 (含回显授权码与 secret 的换令牌失败, 拒绝, 重复使用), 同步 (中途刷新
    令牌), 后台同步失败, 失效, 断开; 各接口的响应体与响应头 (含 Location), 日志 (DEBUG 级, 含 httpx 与异常栈), 全部
    留痕, 概览, 连接对象, 令牌密文与 state 登记里都没有令牌, 授权码, client secret, Basic 凭据, 每次授权的 state 与
    PKCE verifier. 授权地址那一条响应 (本来就带 state) 与测试客户端自己发往回调地址的请求日志 (代替浏览器) 不算在内."""
    caplog.set_level(logging.DEBUG)
    c = case(tmp_path, **({"XERO_CLIENT_SECRET": ""} if mode == "pkce" else {}))
    texts: list[str] = []
    states: list[str] = []
    verifiers: list[str] = []

    def keep(response: httpx.Response) -> None:
        texts.extend([response.text, json.dumps(dict(response.headers))])

    async def issue(http: httpx.AsyncClient) -> str:
        """取授权地址 (这条响应不进扫描), 记下 state 与登记的 verifier."""
        state = await authorize(http, c)
        states.append(state)
        verifiers.extend(
            v.verifier for v in c.xero.states.pending.values() if v.verifier and v.verifier not in verifiers
        )
        return state

    async with client(c) as http:
        c.fake.echo_on_exchange = True
        keep(await http.get(CALLBACK_PATH, params={"code": "wrong", "state": await issue(http)}))
        c.fake.echo_on_exchange = False
        keep(await http.get(CALLBACK_PATH, params={"error": "access_denied", "state": await issue(http)}))
        state = await issue(http)
        keep(await http.get(CALLBACK_PATH, params={"code": CODE, "state": state}))
        keep(await http.get(CALLBACK_PATH, params={"code": CODE, "state": state}))  # 重复使用
        keep(await http.get(API.format(eid=c.eid) + "/tenants"))
        c.advance(timedelta(hours=1))
        keep(await http.post(API.format(eid=c.eid) + "/sync"))
        await c.drain()
        keep(await http.get(f"/api/v1/engagements/{c.eid}/data/summary"))
        c.fake.fail["Invoices"] = 503  # 后台同步失败: 留痕 xero_sync_failed
        keep(await http.post(API.format(eid=c.eid) + "/sync"))
        await c.drain()
        c.advance(timedelta(hours=1))
        c.fake.refresh_error = "invalid_grant"
        keep(await http.post(API.format(eid=c.eid) + "/sync"))
        c.fake.revoke_status = 400
        keep(await http.delete(API.format(eid=c.eid)))
        c.fake.refresh_error = None
        keep(await http.get(CALLBACK_PATH, params={"code": CODE, "state": await issue(http)}))
        texts += [conn.model_dump_json() + sealed for conn, sealed in c.store.rows.values()]
        keep(await http.delete(API.format(eid=c.eid)))
    texts += [r.getMessage() for r in caplog.records if not ("http://test/" in r.getMessage() and r.name == "httpx")]
    texts += [r.exc_text or "" for r in caplog.records]
    texts += [e.model_dump_json() for e in c.trail.entries]
    texts += [repr(c.xero.states.pending), repr(c.xero.states.used), repr(c.xero)]
    assert c.fake.issued >= 3 and {"xero_connect", "xero_sync_failed", "xero_disconnect", "sync"} <= {
        e.payload.get("action") for e in c.trail.entries
    }
    assert len(states) == 4 and (len(verifiers) == 4 if mode == "pkce" else verifiers == [])
    values = {"secret": SECRETS, "state": states, "verifier": verifiers}
    leaked = sorted({kind for kind, found in values.items() for s in found if any(s in text for text in texts)})
    assert leaked == [], f"有密钥出现在输出里 (只报类别): {leaked}"


async def test_stored_connection_holds_only_ciphertext(tmp_path: Path) -> None:
    """连接对象里没有令牌, 库里只有 Fernet 密文 (换了主密钥就解不开, 同步报 SETTINGS_KEY_INVALID, 要重新连接)."""
    c = case(tmp_path)
    async with client(c) as http:
        await connect(http, c)
        connection, sealed = c.store.rows[c.eid]
        assert ACCESS not in connection.model_dump_json() and REFRESH not in sealed
        assert sealed.startswith("gAAAA")  # Fernet 令牌的版本字节
        c.xero.settings = settings(tmp_path)  # 换了主密钥
        response = await http.post(API.format(eid=c.eid) + "/sync")
        assert (response.status_code, response.json()["detail"]["code"]) == (503, "SETTINGS_KEY_INVALID")
        assert not c.xero.data.runner.is_running(c.eid)
        gone = await http.delete(API.format(eid=c.eid))  # 令牌解不开也能断开: 删连接, Xero 侧撤销不了
    assert (gone.status_code, gone.json()) == (200, {"revoked": False})
    assert c.store.rows == {} and c.fake.revoked == []


# ── 其余 ──


async def test_cli_token_file_untouched_by_web_connection(tmp_path: Path) -> None:
    """命令行的令牌文件与网页连接互不读写: 网页连接, 同步, 断开前后文件内容不变."""
    c = case(tmp_path)
    path = c.xero.settings.XERO_TOKEN_PATH
    path.parent.mkdir(parents=True)
    path.write_text('{"access_token": "cli", "refresh_token": "cli", "expires_at": 0}', encoding="utf-8")
    before = path.read_bytes()
    async with client(c) as http:
        await connect(http, c)
        await http.post(API.format(eid=c.eid) + "/sync")
        await c.drain()
        await http.delete(API.format(eid=c.eid))
    assert path.read_bytes() == before and sorted(p.name for p in path.parent.iterdir()) == ["token.json"]


def test_access_log_redacts_callback_query() -> None:
    """uvicorn 访问日志里回调地址的查询串 (带 code 与 state) 被抹掉, 其余请求原样."""
    redact = RedactCallbackQuery()

    def message(path: str) -> str:
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "GET", path, "1.1", 302),
            None,
        )
        assert redact.filter(record)
        return record.getMessage()

    assert CODE not in message(f"{CALLBACK_PATH}?code={CODE}&state=abc") and "<redacted>" in message(
        f"{CALLBACK_PATH}?x=1"
    )
    assert message("/api/v1/health?x=1").endswith('"GET /api/v1/health?x=1 HTTP/1.1" 302')
    # 部署在反向代理前缀下 (uvicorn --root-path) 时记的路径带前缀, 照样抹掉
    assert message(f"/audit{CALLBACK_PATH}?code={CODE}&state=abc").endswith(
        f'/audit{CALLBACK_PATH}?<redacted> HTTP/1.1" 302'
    )


def test_create_app_installs_access_log_filter() -> None:
    """组装根 create_app() 给 uvicorn.access 装上 RedactCallbackQuery (回调的 code 与 state 不进访问日志的防线),
    重复调用只装一次."""
    from backend.main import create_app

    access = logging.getLogger("uvicorn.access")
    kept = list(access.filters)
    try:
        for f in kept:
            access.removeFilter(f)
        create_app()
        create_app()
        assert [type(f) for f in access.filters] == [RedactCallbackQuery]
    finally:
        for f in list(access.filters):
            access.removeFilter(f)
        for f in kept:
            access.addFilter(f)


def test_settings_defaults_split_web_and_cli_redirects() -> None:
    """Q4: XERO_REDIRECT_URI 缺省为后端回调地址, 命令行另用 XERO_CLI_REDIRECT_URI (原来的本机地址); 前端地址缺省取
    FRONTEND_ORIGIN."""
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert defaults.XERO_REDIRECT_URI.endswith(CALLBACK_PATH)
    assert defaults.XERO_CLI_REDIRECT_URI == "http://localhost:8765/callback"
    assert defaults.frontend_url == defaults.FRONTEND_ORIGIN


async def test_states_belong_to_one_engagement_and_user(tmp_path: Path) -> None:
    """state 绑定发起时的项目与用户: 连接记在发起人名下 (不是回调时的登录用户), 回调不靠登录."""
    c = case(tmp_path)
    async with client(c) as http:
        state = await authorize(http, c)
        http.headers.pop("Authorization")
        assert await callback(http, code=CODE, state=state) == f"{FRONT}/engagements/{c.eid}/data?xero=connected"
        assert c.store.rows[c.eid][0].connected_by == "U-1"
        other = await c.xero.authorize(c.eid, "U-7")  # 合成: 另一位审计师发起的授权
        assert await callback(http, code=CODE, state=httpx.URL(other).params["state"]) == (
            f"{FRONT}/engagements/{c.eid}/data?xero=connected"
        )
    assert c.store.rows[c.eid][0].connected_by == "U-7"
    assert [e.actor for e in c.trail.entries if e.payload.get("action") == "xero_connect"] == ["U-1", "U-7"]


async def test_reconnect_keeps_sync_record_and_chosen_organisation(tmp_path: Path) -> None:
    """重新连接 (如授权失效后) 整行替换令牌与组织列表: 原来选的组织仍在授权里时保留, 最近一次同步的时间与表保留;
    选的组织不在新授权里时清空, 等重新选."""
    c = case(tmp_path, tenants=(OTHER, DEMO))
    async with client(c) as http:
        await connect(http, c)
        await http.put(API.format(eid=c.eid) + "/tenant", json={"tenant_id": DEMO["tenantId"]})
        assert (await http.post(API.format(eid=c.eid) + "/sync")).status_code == 202
        await c.drain()
        synced = await xero_source(http, c)
        c.advance(timedelta(days=61))
        assert (await xero_source(http, c))["status"] == "expired"
        await connect(http, c)
        again = await xero_source(http, c)
        c.fake.tenants = [OTHER, THIRD]
        await connect(http, c)
        dropped = await xero_source(http, c)
        response = await http.post(API.format(eid=c.eid) + "/sync")
    assert again == synced and synced["status"] == "connected" and synced["last_synced_at"] is not None
    assert (dropped["organisation"], dropped["last_synced_at"]) == (None, synced["last_synced_at"])
    assert response.json()["detail"]["code"] == "XERO_TENANT_REQUIRED"
