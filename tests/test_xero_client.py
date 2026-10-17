"""Xero 客户端: PKCE, 令牌刷新, 重试, 分页与快照. 全部用假 HTTP 传输 (httpx.MockTransport), 不联网."""

import base64
import json
import os
import urllib.parse
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.adapters.xero import (
    API_URL,
    CONNECTIONS_URL,
    PAGE_SIZE,
    SCOPES,
    TOKEN_URL,
    Period,
    Tokens,
    TokenStore,
    XeroClient,
    XeroError,
    authorize_url,
    choose_tenant,
    code_challenge,
    code_verifier,
    login_scopes,
    pull,
)

NOW = 1_800_000_000.0
Handler = Callable[[httpx.Request], httpx.Response]


def token_reply(access: str = "at-2", refresh: str = "rt-2") -> dict[str, Any]:
    return {"access_token": access, "refresh_token": refresh, "expires_in": 1800, "scope": "offline_access"}


def make(tmp_path: Path, handler: Handler, *, expires_at: float = NOW + 1800, tenant: str = "T1") -> XeroClient:
    store = TokenStore(tmp_path / "token.json")
    store.save(Tokens("at-1", "rt-1", expires_at, "offline_access", tenant, "Demo Company (AU)"))
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    client = XeroClient("CID", store, transport=httpx.MockTransport(handler), sleep=sleep, clock=lambda: NOW)
    client.slept = slept  # type: ignore[attr-defined]
    return client


def test_code_challenge_matches_rfc7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_code_verifier_length_and_charset() -> None:
    verifier = code_verifier()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def test_authorize_url_carries_pkce_parameters() -> None:
    url = authorize_url("CID", "http://localhost:8765/callback", "CH", "ST", SCOPES)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge"] == ["CH"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:8765/callback"]
    assert query["scope"] == [" ".join(SCOPES)] and "offline_access" in query["scope"][0]


def test_authorize_url_without_challenge_omits_pkce() -> None:
    url = authorize_url("CID", "http://localhost:8765/callback", "", "ST", SCOPES)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert "code_challenge" not in query and "code_challenge_method" not in query


async def test_web_app_exchange_uses_basic_auth(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=token_reply())

    client = make(tmp_path, handler)
    client.client_secret = "SECRET"
    await client.exchange("CODE", "", "http://localhost:8765/callback")
    form = urllib.parse.parse_qs(seen[0].content.decode())
    assert seen[0].headers["Authorization"] == "Basic " + base64.b64encode(b"CID:SECRET").decode()
    assert "code_verifier" not in form and "client_id" not in form and "client_secret" not in form
    assert "SECRET" not in repr(client)


def test_token_file_is_private(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "nested" / "token.json")
    store.save(Tokens("a", "r", NOW))
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.load() == Tokens("a", "r", NOW)


async def test_exchange_posts_verifier_without_secret(tmp_path: Path) -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        seen.append(urllib.parse.parse_qs(request.content.decode()))
        return httpx.Response(200, json=token_reply())

    client = make(tmp_path, handler)
    tokens = await client.exchange("CODE", "VERIFIER", "http://localhost:8765/callback")
    assert seen[0]["grant_type"] == ["authorization_code"] and seen[0]["code_verifier"] == ["VERIFIER"]
    assert seen[0]["client_id"] == ["CID"] and "client_secret" not in seen[0]
    assert tokens.access_token == "at-2" and tokens.expires_at == NOW + 1800
    assert client.store.load() == tokens


async def test_refreshes_before_expiry_and_keeps_tenant(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            assert urllib.parse.parse_qs(request.content.decode())["refresh_token"] == ["rt-1"]
            return httpx.Response(200, json=token_reply())
        assert request.headers["Authorization"] == "Bearer at-2"
        assert request.headers["xero-tenant-id"] == "T1"
        return httpx.Response(200, json={"Accounts": []})

    client = make(tmp_path, handler, expires_at=NOW + 10)
    await client.get("Accounts")
    saved = client.store.load()
    assert saved is not None and saved.refresh_token == "rt-2" and saved.tenant_id == "T1"


async def test_401_refreshes_once_then_retries(tmp_path: Path) -> None:
    replies = iter([httpx.Response(401), httpx.Response(200, json=token_reply()), httpx.Response(200, json={"A": 1})])

    client = make(tmp_path, lambda request: next(replies))
    assert await client.get("Organisation") == {"A": 1}


async def test_429_waits_retry_after_then_succeeds(tmp_path: Path) -> None:
    replies = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": True})])
    client = make(tmp_path, lambda request: next(replies))
    assert await client.get("Invoices") == {"ok": True}
    assert client.slept == [7.0]  # type: ignore[attr-defined]


async def test_5xx_exhausts_retries(tmp_path: Path) -> None:
    client = make(tmp_path, lambda request: httpx.Response(503, text="busy"))
    with pytest.raises(XeroError, match="重试 3 次"):
        await client.get("Invoices")
    assert client.slept == [1.0, 2.0, 4.0]  # type: ignore[attr-defined]


async def test_403_is_not_retried(tmp_path: Path) -> None:
    client = make(tmp_path, lambda request: httpx.Response(403, text="forbidden"))
    with pytest.raises(XeroError) as e:
        await client.get("Journals")
    assert e.value.status == 403 and client.calls == 1


async def test_token_errors_do_not_echo_body(tmp_path: Path) -> None:
    client = make(tmp_path, lambda request: httpx.Response(400, json={"error": "invalid_grant", "access_token": "x"}))
    with pytest.raises(XeroError) as e:
        await client.refresh(Tokens("at", "rt", NOW))
    assert "invalid_grant" in str(e.value) and "access_token" not in str(e.value)


async def test_paged_stops_on_short_page(tmp_path: Path) -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        size = PAGE_SIZE if page < 3 else 5
        return httpx.Response(200, json={"Invoices": [{"n": i} for i in range(size)]})

    rows = await make(tmp_path, handler).paged("Invoices", "Invoices")
    assert pages == [1, 2, 3] and len(rows) == 2 * PAGE_SIZE + 5


async def test_journals_use_journal_number_offset(tmp_path: Path) -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        batch = [{"JournalNumber": n} for n in range(offset + 1, min(offset + 100, 150) + 1)]
        return httpx.Response(200, json={"Journals": batch})

    rows = await make(tmp_path, handler).journals()
    assert offsets == [0, 100, 150] and len(rows) == 150


async def test_choose_tenant_requires_single_organisation(tmp_path: Path) -> None:
    connections = [
        {"tenantId": "A", "tenantType": "ORGANISATION", "tenantName": "Test"},
        {"tenantId": "B", "tenantType": "ORGANISATION", "tenantName": "Demo Company (AU)"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CONNECTIONS_URL and "xero-tenant-id" not in request.headers
        return httpx.Response(200, json=connections)

    client = make(tmp_path, handler, tenant="")
    with pytest.raises(XeroError, match="XERO_TENANT_ID"):
        await choose_tenant(client)
    tokens = await choose_tenant(client, "B")
    assert tokens.tenant_name == "Demo Company (AU)"


def fake_api(request: httpx.Request) -> httpx.Response:
    """假 Xero: Journals 403, 报表与列表接口各回两条."""
    path = request.url.path.removeprefix(urllib.parse.urlparse(API_URL).path + "/")
    if path == "Journals":
        return httpx.Response(403, text="Journals not included in plan")
    if path.startswith("Reports/"):
        return httpx.Response(200, json={"Reports": [{"ReportName": "Trial Balance", "Rows": []}]})
    key = {"Organisation": "Organisations"}.get(path, path)
    return httpx.Response(200, json={key: [{"id": 1}, {"id": 2}]})


async def test_pull_writes_snapshot_and_records_unavailable(tmp_path: Path) -> None:
    out = tmp_path / "snap"
    period = Period(date(2026, 7, 1), date(2026, 11, 30), date(2026, 6, 30))
    manifest = await pull(make(tmp_path, fake_api), out, period, paid="live")
    assert manifest["counts"]["invoices"] == 2 and manifest["counts"]["organisation"] == 2
    assert manifest["counts"]["trial_balance_2026-06-30"] == 1 and manifest["counts"]["trial_balance_2026-11-30"] == 1
    assert "journals" in manifest["unavailable"] and not (out / "journals.json").exists()
    assert json.loads((out / "bank_statements.json").read_text()) == {}  # 假科目里没有银行科目
    assert json.loads((out / "manifest.json").read_text())["tenant_id"] == "T1"
    assert json.loads((out / "dataset.json").read_text())["period_end"] == "2026-11-30"


async def test_pull_off_skips_paid_endpoints(tmp_path: Path) -> None:
    out = tmp_path / "snap"
    manifest = await pull(
        make(tmp_path, fake_api), out, Period(date(2026, 7, 1), date(2026, 11, 30), date(2026, 6, 30))
    )
    assert "journals" not in manifest["unavailable"] and not (out / "bank_statements.json").exists()


async def test_bank_statements_split_into_twelve_month_windows(tmp_path: Path) -> None:
    windows: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/finance.xro/1.0/BankStatementsPlus/statements")
        windows.append((request.url.params["FromDate"], request.url.params["ToDate"]))
        return httpx.Response(
            200, json={"bankAccountId": "B", "statements": [{"startDate": request.url.params["FromDate"]}]}
        )

    client = make(tmp_path, handler)
    client.clock = lambda: datetime(2026, 10, 17, tzinfo=UTC).timestamp()
    data = await client.bank_statements("B", date(2024, 7, 1), date(2026, 11, 30))
    assert windows == [("2024-07-01", "2025-06-30"), ("2025-07-01", "2026-06-30"), ("2026-07-01", "2026-10-17")]
    assert len(data["statements"]) == 3


def test_login_scopes_paid_endpoints_on_demand() -> None:
    """付费接口的 scope 按需申请: 应用未获准时申请会被授权页拒绝 (invalid_scope)."""
    assert "accounting.journals.read" not in login_scopes() and "finance.bankstatementsplus.read" not in login_scopes()
    both = login_scopes(journals=True, statements=True)
    assert both[-2:] == ("accounting.journals.read", "finance.bankstatementsplus.read")


def test_token_temp_file_private_while_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """令牌先写进新建即为 0600 的临时文件; 替换失败时临时文件被删掉."""
    seen: list[int] = []
    real_replace = os.replace

    def replace(src: str, dst: Path) -> None:
        seen.append(os.stat(src).st_mode & 0o777)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    old = os.umask(0o022)
    try:
        TokenStore(tmp_path / "token.json").save(Tokens("a", "r", NOW))
    finally:
        os.umask(old)
    assert seen == [0o600]

    def broken(src: str, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        TokenStore(tmp_path / "token.json").save(Tokens("b", "r", NOW))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["token.json"]


async def test_errors_and_manifest_never_echo_tokens(tmp_path: Path) -> None:
    """上游错误正文回显令牌时, 异常信息与 manifest 只留状态码与错误类型 (Codex 审核 X01 的探针)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Journals"):
            return httpx.Response(403, json={"Title": "Forbidden at-1", "Detail": "Authorization Bearer at-1"})
        return fake_api(request)

    out = tmp_path / "snap"
    await pull(
        make(tmp_path, handler), out, Period(date(2026, 7, 1), date(2026, 11, 30), date(2026, 6, 30)), paid="live"
    )
    manifest = (out / "manifest.json").read_text()
    assert "at-1" not in manifest and "HTTP 403 Forbidden ***" in manifest


async def test_pull_refuses_existing_dir_and_leaves_no_partial_snapshot(tmp_path: Path) -> None:
    """已有快照目录不覆盖 (旧的付费数据不会混进新快照); 拉取失败不留下可载入的目录."""
    period = Period(date(2026, 7, 1), date(2026, 11, 30), date(2026, 6, 30))
    existing = tmp_path / "old"
    existing.mkdir()
    with pytest.raises(XeroError, match="已存在"):
        await pull(make(tmp_path, fake_api), existing, period)

    def failing(request: httpx.Request) -> httpx.Response:
        if "Reports/" in request.url.path:
            return httpx.Response(400, json={"Title": "BadRequest"})
        return fake_api(request)

    out = tmp_path / "new"
    with pytest.raises(XeroError):
        await pull(make(tmp_path, failing), out, period)
    assert not out.exists()
    manifest = await pull(make(tmp_path, fake_api), out, period)  # 半成品被清掉后可重拉
    assert out.is_dir() and manifest["counts"]["accounts"] == 2


async def test_long_tokens_redacted_before_truncation(tmp_path: Path) -> None:
    """长令牌先整体替换再截断, 回显的令牌前缀不进 manifest (Codex 审核 X01 第 2 轮的探针)."""
    token = "unique-private-access-token-" + "0123456789abcdef" * 16

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Journals"):
            return httpx.Response(403, json={"Title": "Forbidden " + token, "Type": "ValidationException"})
        return fake_api(request)

    client = make(tmp_path, handler)
    client.store.save(Tokens(token, "refresh-secret", NOW + 1800, "", "T1", "Demo"))
    out = tmp_path / "snapshot"
    await pull(client, out, Period(date(2026, 7, 1), date(2026, 11, 30), date(2026, 6, 30)), paid="live")
    manifest = (out / "manifest.json").read_text()
    assert token[:20] not in manifest and "ValidationException" in manifest


async def test_token_errors_redact_basic_credentials(tmp_path: Path) -> None:
    """令牌接口回显 Basic 凭据 (含只回显一部分) 时, 异常信息里都没有 (Codex 审核 X01 第 2 轮的探针)."""
    secret = "private-client-secret"
    basic = base64.b64encode(f"CID:{secret}".encode()).decode()
    for echoed in (basic, basic[:24]):
        client = make(tmp_path, lambda request, e=echoed: httpx.Response(400, json={"Title": "Rejected Basic " + e}))
        client.client_secret = secret
        with pytest.raises(XeroError) as error:
            await client.refresh(Tokens("at-1", "rt-1", NOW))
        assert echoed not in str(error.value) and "HTTP 400" in str(error.value)
