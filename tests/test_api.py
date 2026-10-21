"""登录、项目归属与决定错误码. 不启动数据库或编排图."""

import functools
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    CorrectionNotice,
    Engagement,
    Misstatement,
    MisstatementUpdate,
    Phase,
    ReviewRecord,
    TrailEntry,
    TrailKind,
    User,
)
from backend.core.security import create_access_token, decode_access_token, hash_password
from backend.main import create_app
from backend.persistence.stores import CsvDatasetLoader, PostgresUserStore
from tests.external import REPO
from tests.fakes import FakeEngagementStore, FakeFinancialStore, FakeOutputStore, FakeTrail


@functools.cache
def hashed(password: str) -> str:
    """口令的哈希, 每个口令只算一次: argon2 一次约 0.25 秒, 原先每个用例建用户表都重算 (盐是随机的, 同一份哈希
    照样验得过)."""
    return hash_password(password)


def _engagement(eid: str, owner: str) -> Engagement:
    now = datetime.now(UTC)
    return Engagement(
        id=eid,
        client_id="CLIENT-1",
        period_start=date(2025, 7, 1),
        period_end=date(2026, 6, 30),
        dataset="demo",
        owner_id=owner,
        client_profile_version=1,
        phase=Phase.P0_Acceptance,
        created_at=now,
        updated_at=now,
    )


class _Users:
    """内存用户表: 预置 rca@example.com (密码 secret). 与 UserStore 契约一致, 查找与判重不区分大小写."""

    def __init__(self) -> None:
        self.user = User(id="USR-1", email="rca@example.com", full_name="Jordan Ellis")
        self.rows: dict[str, tuple[User, str]] = {self.user.email: (self.user, hashed("secret"))}

    async def by_email(self, email: str) -> tuple[User, str] | None:
        # 同 PostgresUserStore: 不区分大小写; 多个只差大小写的账号时只认完全相同的那个
        matches = [row for key, row in self.rows.items() if key.lower() == email.lower()]
        if len(matches) == 1:
            return matches[0]
        return self.rows.get(email)

    async def get(self, user_id: str) -> User | None:
        return next((user for user, _ in self.rows.values() if user.id == user_id), None)

    async def create(self, email: str, full_name: str, password_hash: str) -> User | None:
        # 同 PostgresUserStore: 判重独立于 by_email 的取舍, 任一大小写变体存在即算占用
        if any(key.lower() == email.lower() for key in self.rows):
            return None
        user = User(id=f"USR-{len(self.rows) + 1}", email=email, full_name=full_name)
        self.rows[email] = (user, password_hash)
        return user

    def make_legacy_uppercase(self) -> None:
        """把预置账号改成注册功能之前可能存在的写法: 库里存的是含大写的邮箱."""
        user, hashed = self.rows.pop(self.user.email)
        legacy = user.model_copy(update={"email": "RCA@Example.com"})
        self.rows[legacy.email] = (legacy, hashed)


class _Engagements:
    def __init__(self) -> None:
        self.mine = _engagement("ENG-2026-001", "USR-1")
        self.theirs = _engagement("ENG-2026-002", "USR-2")

    async def get(self, eid: str) -> Engagement | None:
        if eid == self.mine.id:
            return self.mine
        if eid == self.theirs.id:
            return self.theirs
        return None


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    users = _Users()
    decisions = SimpleNamespace(submit=AsyncMock(), update_client_profile=AsyncMock())
    trail = FakeTrail()
    deps = SimpleNamespace(users=users, engagement_store=_Engagements(), trail=trail)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        app.state.deps = deps
        app.state.runner = SimpleNamespace(start=AsyncMock(), resume=AsyncMock())
        app.state.query = SimpleNamespace(gate=AsyncMock(return_value=None), overview=AsyncMock())
        app.state.decisions = decisions
        yield

    monkeypatch.setattr("backend.main.lifespan", lifespan)
    with TestClient(create_app()) as client:
        token = create_access_token(users.user.id)
        yield SimpleNamespace(client=client, decisions=decisions, token=token, users=users, trail=trail)


def _auth(api: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {api.token}"}


def test_login_and_me(api: SimpleNamespace) -> None:
    ok = api.client.post("/api/v1/auth/login", data={"username": "rca@example.com", "password": "secret"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["token_type"] == "bearer"
    assert (
        api.client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {body['access_token']}"}).json()["email"]
        == "rca@example.com"
    )


def test_login_bad_credentials(api: SimpleNamespace) -> None:
    bad = api.client.post("/api/v1/auth/login", data={"username": "rca@example.com", "password": "nope"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == {"code": "BAD_CREDENTIALS", "message": "账号或密码不正确"}


def test_login_name_ignores_case_and_spaces(api: SimpleNamespace) -> None:
    ok = api.client.post("/api/v1/auth/login", data={"username": "  RCA@Example.com ", "password": "secret"})
    assert ok.status_code == 200


def test_register_returns_token_for_new_account(api: SimpleNamespace) -> None:
    created = api.client.post(
        "/api/v1/auth/register", json={"username": " New.Auditor ", "full_name": " Mei Tran ", "password": "longenough"}
    )
    assert created.status_code == 201
    token = created.json()["access_token"]
    me = api.client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert (me["email"], me["full_name"]) == ("new.auditor", "Mei Tran")
    # 注册后用同一账号 (大小写不同) 登录
    again = api.client.post("/api/v1/auth/login", data={"username": "NEW.AUDITOR", "password": "longenough"})
    assert again.status_code == 200


def test_register_rejects_taken_username(api: SimpleNamespace) -> None:
    taken = api.client.post(
        "/api/v1/auth/register", json={"username": "RCA@example.com", "full_name": "Someone", "password": "longenough"}
    )
    assert taken.status_code == 409
    assert taken.json()["detail"]["code"] == "USERNAME_TAKEN"
    # 原账号的密码不变
    ok = api.client.post("/api/v1/auth/login", data={"username": "rca@example.com", "password": "secret"})
    assert ok.status_code == 200


@pytest.mark.parametrize(
    ("username", "full_name", "password", "field"),
    [
        ("   ", "Mei Tran", "longenough", "账号"),
        ("mei tran", "Mei Tran", "longenough", "账号"),
        ("a" * 255, "Mei Tran", "longenough", "账号"),
        ("mei", "  ", "longenough", "姓名"),
        ("mei", "M" * 101, "longenough", "姓名"),
        ("mei", "Mei Tran", "short12", "密码"),
        ("mei", "Mei Tran", "p" * 129, "密码"),
    ],
)
def test_register_rejects_invalid_fields(
    api: SimpleNamespace, username: str, full_name: str, password: str, field: str
) -> None:
    bad = api.client.post(
        "/api/v1/auth/register", json={"username": username, "full_name": full_name, "password": password}
    )
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert detail["code"] == "INVALID_VALUE"
    assert detail["message"].startswith(field)


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/api/v1/auth/register", {"json": {"username": "mei", "password": "do-not-echo-me"}}),
        ("/api/v1/auth/login", {"data": {"password": "do-not-echo-me"}}),
    ],
)
def test_auth_validation_errors_do_not_echo_password(api: SimpleNamespace, path: str, kwargs: dict[str, Any]) -> None:
    bad = api.client.post(path, **kwargs)
    assert bad.status_code == 422
    assert "do-not-echo-me" not in bad.text


class _StoredUsers(PostgresUserStore):
    """真实 PostgresUserStore.by_email, 只把查库换成固定的行 (均为同一登录名的大小写变体)."""

    def __init__(self, emails: list[str]) -> None:
        self.data = [
            {
                "id": f"USR-{index}",
                "email": email,
                "full_name": f"Auditor {index}",
                "password_hash": hashed(f"password-{index}"),
            }
            for index, email in enumerate(emails)
        ]

    async def rows(self, eid: str, statement: Any) -> list[dict[str, Any]]:
        return self.data


async def test_store_picks_exact_spelling_among_legacy_case_variants() -> None:
    users = _StoredUsers(["rca@example.com", "RCA@Example.com"])
    found = await users.by_email("RCA@Example.com")
    assert found is not None and found[0].id == "USR-1"
    assert await users.by_email("Rca@Example.com") is None  # 两个都不完全相同, 不猜


@pytest.mark.parametrize("emails", [["rca@example.com", "RCA@Example.com"], ["Rca@Example.com", "RCA@Example.com"]])
def test_login_uses_exact_spelling_when_legacy_case_variants_exist(api: SimpleNamespace, emails: list[str]) -> None:
    api.client.app.state.deps.users = _StoredUsers(emails)
    ok = api.client.post("/api/v1/auth/login", data={"username": " RCA@Example.com ", "password": "password-1"})
    assert ok.status_code == 200
    assert decode_access_token(ok.json()["access_token"]) == "USR-1"


def test_login_never_signs_in_the_other_case_variant(api: SimpleNamespace) -> None:
    users = _StoredUsers(["rca@example.com", "RCA@Example.com"])
    users.data[1]["password_hash"] = users.data[0]["password_hash"]  # 密码相同时也要按写法区分账号
    api.client.app.state.deps.users = users
    ok = api.client.post("/api/v1/auth/login", data={"username": "RCA@Example.com", "password": "password-0"})
    assert decode_access_token(ok.json()["access_token"]) == "USR-1"


def test_legacy_uppercase_account_still_logs_in_and_blocks_case_variant(api: SimpleNamespace) -> None:
    api.users.make_legacy_uppercase()
    ok = api.client.post("/api/v1/auth/login", data={"username": "RCA@Example.com", "password": "secret"})
    assert ok.status_code == 200
    taken = api.client.post(
        "/api/v1/auth/register", json={"username": "rca@example.com", "full_name": "New", "password": "new-password"}
    )
    assert taken.status_code == 409


def test_register_blocked_when_only_case_variants_exist(api: SimpleNamespace) -> None:
    api.users.rows = {
        key: (User(id=f"USR-{index}", email=key, full_name="Legacy"), hashed("secret"))
        for index, key in enumerate(["Rca@Example.com", "RCA@Example.com"])
    }
    taken = api.client.post(
        "/api/v1/auth/register", json={"username": "rca@example.com", "full_name": "New", "password": "new-password"}
    )
    assert taken.status_code == 409


@pytest.mark.parametrize("separator", ["\u3000", "\t", "\n", "\u00a0"])
def test_register_rejects_unicode_whitespace_inside_username(api: SimpleNamespace, separator: str) -> None:
    bad = api.client.post(
        "/api/v1/auth/register", json={"username": f"user{separator}name", "full_name": "Name", "password": "password"}
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "INVALID_VALUE"


def test_register_counts_characters_and_keeps_password_spaces(api: SimpleNamespace) -> None:
    # 长度按字符数; 账号首尾的全角空格去掉; 密码不去空白, 首尾空格是密码的一部分
    password = " " + "密" * 126 + " "
    created = api.client.post(
        "/api/v1/auth/register",
        json={"username": "\u3000" + "名" * 254 + "\u3000", "full_name": "姓" * 100, "password": password},
    )
    assert created.status_code == 201
    assert api.client.post("/api/v1/auth/login", data={"username": "名" * 254, "password": password}).status_code == 200
    stripped = api.client.post("/api/v1/auth/login", data={"username": "名" * 254, "password": password.strip()})
    assert stripped.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "name", "password": {"nested": "do-not-echo-me"}},
        {"username": [], "full_name": None, "password": "do-not-echo-me"},
        {"username": "name", "full_name": "Name", "password": "do-not-echo-me", "extra": "do-not-echo-me"},
        ["do-not-echo-me"],
    ],
)
def test_register_malformed_bodies_do_not_echo_password(api: SimpleNamespace, payload: Any) -> None:
    bad = api.client.post("/api/v1/auth/register", json=payload)
    assert bad.status_code == 422
    assert "do-not-echo-me" not in bad.text


def test_register_broken_json_does_not_echo_password(api: SimpleNamespace) -> None:
    bad = api.client.post(
        "/api/v1/auth/register", content='{"password": "do-not-echo-me",', headers={"Content-Type": "application/json"}
    )
    assert bad.status_code == 422
    assert "do-not-echo-me" not in bad.text


def test_register_accepts_boundary_lengths(api: SimpleNamespace) -> None:
    ok = api.client.post(
        "/api/v1/auth/register", json={"username": "a" * 254, "full_name": "M" * 100, "password": "p" * 8}
    )
    assert ok.status_code == 201


def test_missing_token(api: SimpleNamespace) -> None:
    assert api.client.get("/api/v1/users/me").status_code == 401


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("ENG-2026-002", "NOT_FOUND"),  # 别人的项目
        ("ENG-missing", "NOT_FOUND"),  # 不存在的项目
    ],
)
def test_engagement_routes_return_404(api: SimpleNamespace, path: str, code: str) -> None:
    response = api.client.get(f"/api/v1/engagements/{path}", headers=_auth(api))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == code


def test_gate_not_at_gate_returns_null(api: SimpleNamespace) -> None:
    """需求 R346: 自己的项目不在闸门是常态, GET /gate 回 200, 响应体 null (不再是 404 NOT_AT_GATE)."""
    response = api.client.get("/api/v1/engagements/ENG-2026-001/gate", headers=_auth(api))
    assert (response.status_code, response.json()) == (200, None)


def test_gate_material_missing_is_404_not_found(api: SimpleNamespace) -> None:
    """停在闸门而材料缺失 (query.gate 抛 LookupError) 时 GET /gate 仍为 404 NOT_FOUND."""
    api.client.app.state.query.gate.side_effect = LookupError("停在 PLN-15 闸门, 缺材料")
    response = api.client.get("/api/v1/engagements/ENG-2026-001/gate", headers=_auth(api))
    assert (response.status_code, response.json()["detail"]["code"]) == (404, "NOT_FOUND")


TRAIL_ROWS = [  # 依次为 TR-1 到 TR-5
    ("ENG-2026-001", TrailKind.llm_call, "REV-02 SI-2026-0612 第 1 次调用大模型: passed"),
    ("ENG-2026-001", TrailKind.decision, "ACC-01 approve"),
    ("ENG-2026-002", TrailKind.llm_call, "别的项目"),
    ("ENG-2026-001", TrailKind.llm_call, "REV-02 SI-2026-0618 第 1 次调用大模型: passed"),
    ("ENG-2026-001", TrailKind.deferral, "PLN-01/03 稍后处理"),
]


def _add_trail(api: SimpleNamespace) -> None:
    at = datetime(2026, 7, 1, tzinfo=UTC)
    for n, (eid, kind, summary) in enumerate(TRAIL_ROWS, 1):
        entry = TrailEntry(id=f"TR-{n}", engagement_id=eid, kind=kind, actor="A6", summary=summary, created_at=at)
        api.trail.entries.append(entry)


def test_trail_list_and_detail(api: SimpleNamespace) -> None:
    """留痕列表按类型筛出本项目的 TrailEntry (键恰为其字段, 不多带别的), 详情与列表中同一条相同; 不存在或别的项目的
    留痕 404 NOT_FOUND. 防列表恒空, 详情恒 404, 或输出多出视图层的键."""
    _add_trail(api)
    base = "/api/v1/engagements/ENG-2026-001/trail"
    listed = api.client.get(base, params={"kind": "llm_call"}, headers=_auth(api))
    assert listed.status_code == 200
    assert [(e["id"], e["kind"]) for e in listed.json()] == [("TR-1", "llm_call"), ("TR-4", "llm_call")]
    assert all(set(e) == set(TrailEntry.model_fields) for e in listed.json())
    detail = api.client.get(f"{base}/TR-4", headers=_auth(api))
    assert (detail.status_code, detail.json()) == (200, listed.json()[1])
    for missing in ("TR-none", "TR-3"):  # TR-3 是别的项目的
        response = api.client.get(f"{base}/{missing}", headers=_auth(api))
        assert (response.status_code, response.json()["detail"]["code"]) == (404, "NOT_FOUND")


@pytest.mark.parametrize(
    ("params", "ids"),
    [
        ({"limit": 2}, ["TR-1", "TR-2"]),  # 原有调用: 从最早一条起
        ({"limit": 2, "newest": "true"}, ["TR-4", "TR-5"]),
        ({"limit": 2, "newest": "true", "before": "TR-4"}, ["TR-1", "TR-2"]),
        ({"kind": "llm_call", "newest": "true", "before": "TR-4"}, ["TR-1"]),
        ({"kind": ["decision", "deferral"], "newest": "true", "limit": 2}, ["TR-2", "TR-5"]),  # kind 可重复
    ],
)
def test_trail_list_pages(api: SimpleNamespace, params: dict[str, Any], ids: list[str]) -> None:
    """GET /trail 把 newest, before 与一组 kind 传给 AuditTrail.list (分页语义由 test_fakes.trail_pages_hold 守), 仍按
    写入顺序返回. 防路由丢掉新参数, 或不带新参数时改了原来的取法."""
    _add_trail(api)
    listed = api.client.get("/api/v1/engagements/ENG-2026-001/trail", params=params, headers=_auth(api))
    assert listed.status_code == 200
    assert [e["id"] for e in listed.json()] == ids


@pytest.mark.parametrize(
    ("before", "status"),
    [("TR-none", 404), ("TR-3", 404), ("", 422)],  # 不存在; 别的项目的; 空串
)
def test_trail_cursor_must_be_a_trail_of_this_engagement(api: SimpleNamespace, before: str, status: int) -> None:
    """游标不是本项目的留痕 404 NOT_FOUND (同单条详情), 空串 422. 防无效游标被当成已到最早一条 (200 [], 前端据此
    显示项目已创建)."""
    _add_trail(api)
    params = {"newest": "true", "before": before}
    response = api.client.get("/api/v1/engagements/ENG-2026-001/trail", params=params, headers=_auth(api))
    assert response.status_code == status
    if status == 404:
        assert response.json()["detail"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("VERSION_MISMATCH", 409),
        ("ENGAGEMENT_BUSY", 409),
        ("RESUME_REQUIRED", 409),
        ("FEATURES_MISMATCH", 409),
        ("NOT_AT_GATE", 409),
        ("DECISION_NOT_ALLOWED", 422),
        ("MISSING_FIELD", 422),
        ("FIELD_NOT_ALLOWED", 422),
        ("INVALID_VALUE", 422),
        ("ENGAGEMENT_LOCKED", 423),
        ("TASK_NOT_FOUND", 404),
        ("NOT_AWAITING_CORRECTION", 409),
    ],
)
@pytest.mark.parametrize("route", ["gate-decisions", "client-profile", "correction-notices"])
def test_gate_decision_error_mapping(api: SimpleNamespace, code: str, status: int, route: str) -> None:
    """DecisionRejected 按错误码表映射, 原样带消息; 闸门的两条写入路由 (提交决定, 更新客户资料) 与提交更正通知 (需求
    R163) 同一张表: NOT_AT_GATE 为 409 (需求 R346: 不再按方法区分), NOT_AWAITING_CORRECTION 为 409."""
    rejected = DecisionRejected(code, "目标对象已更新到 version 3")
    url = f"/api/v1/engagements/ENG-2026-001/{route}"
    if route == "gate-decisions":
        api.decisions.submit.side_effect = rejected
        body = {"gate": "ACC-01", "target_id": "SCORECARD", "target_version": 1, "decision": "approve"}
        response = api.client.post(url, headers=_auth(api), json=body)
    elif route == "correction-notices":
        api.decisions.submit.side_effect = rejected
        response = api.client.post(url, headers=_auth(api), json=POSTED)
    else:
        api.decisions.update_client_profile.side_effect = rejected
        response = api.client.put(url, headers=_auth(api), json=CLIENT_PROFILE_BODY)
    assert response.status_code == status
    assert response.json()["detail"] == {"code": code, "message": "目标对象已更新到 version 3"}


CLIENT_PROFILE_BODY = {  # PUT /client-profile 的一份合规请求体
    "version": 2,
    "legal_name": "X Pty Ltd",
    "abn": "51824753556",
    "entity_type": "company",
    "industry_anzsic": "4512",
    "listed": False,
    "directors": ["A"],
}


def test_review_record_carries_misstatement_update(api: SimpleNamespace) -> None:
    """R159: 复核决定的请求体可带人给的错报更新载荷, 原样交给 DecisionSubmit (字段规则与取值在那里校验, 第 7 节)."""
    update = {"misstatement_id": "M-D-CSH02-091", "base_version": 1, "kind": "classification", "type": "judgmental"}
    body = {"task_id": "T-1", "target_version": 1, "decision": "override", "reason": "r", "override_conclusion": "c"}
    path = "/api/v1/engagements/ENG-2026-001/review-records"
    response = api.client.post(path, headers=_auth(api), json=body | {"misstatement_update": update})
    assert response.status_code == 202
    [call] = api.decisions.submit.await_args_list
    record = call.args[1]
    assert isinstance(record, ReviewRecord) and record.misstatement_update == MisstatementUpdate.model_validate(update)


# ── 更正通知与可载入的更正分录 (需求 R163) ──

NOTICE_URL = "/api/v1/engagements/ENG-2026-001/correction-notices"
POSTED = {"kind": "adjustments_posted", "misstatement_ids": ["M-D-XJ499"], "adjustment_entry_nos": ["AJE-X01"]}


def test_correction_notice_filled_by_server(api: SimpleNamespace) -> None:
    """验收 1: 202 {id: D-{uuid4}}; 交给 DecisionSubmit 的通知 id 同响应, decided_by 为当前用户, decided_at 为服务端
    时刻 (带时区, 不早于请求前), 其余字段原样; 请求体带 decided_by 422 (服务端填, 不收)."""
    before = datetime.now(UTC)
    response = api.client.post(NOTICE_URL, headers=_auth(api), json=POSTED)
    assert response.status_code == 202
    [call] = api.decisions.submit.await_args_list
    eid, notice = call.args
    assert isinstance(notice, CorrectionNotice) and eid == "ENG-2026-001"
    assert response.json() == {"id": notice.id} and notice.id.startswith("D-") and len(notice.id) == 38
    assert (notice.decided_by, notice.kind, notice.misstatement_ids, notice.adjustment_entry_nos) == (
        "USR-1",
        "adjustments_posted",
        ["M-D-XJ499"],
        ["AJE-X01"],
    )
    assert notice.reason is None and notice.decided_at.tzinfo is not None and notice.decided_at >= before
    forged = api.client.post(NOTICE_URL, headers=_auth(api), json=POSTED | {"decided_by": "USR-9"})
    assert forged.status_code == 422


def test_correction_notice_body_checked(api: SimpleNamespace) -> None:
    """请求体的种类不认识 422, 不交 DecisionSubmit; 不是本人的项目 404."""
    response = api.client.post(NOTICE_URL, headers=_auth(api), json=POSTED | {"kind": "posted"})
    assert response.status_code == 422 and not api.decisions.submit.await_args_list
    other = api.client.post(NOTICE_URL.replace("001", "002"), headers=_auth(api), json=POSTED)
    assert other.status_code == 404


def use_engagement(api: SimpleNamespace, **fields: Any) -> SimpleNamespace:
    """本人的项目改几个字段 (如数据集), 返回应用的依赖 (SimpleNamespace, 测试按需加存储)."""
    deps = api.client.app.state.deps
    deps.engagement_store.mine = deps.engagement_store.mine.model_copy(update=fields)
    return deps  # type: ignore[no-any-return]


# Xero演示数据集设计 "补造的更正分录" 一表: 凭证号, 行数, 摘要 (同一笔各行摘要相同; 都记 2026-09-30)
XERO_ADJUSTMENTS = [
    ("AJE-X01", 3, "Audit adjustment AJE-X01: journal #499 removed; no office equipment held at 30 September 2026"),
    (
        "AJE-X02",
        2,
        "Audit adjustment AJE-X02: superannuation guarantee at 12% on wages of 33,500.00 and accrued wages of 2,010.00",
    ),
    ("AJE-X03", 2, "Audit adjustment AJE-X03: wages accrued for 28 to 30 September 2026"),
    ("AJE-X04", 2, "Audit adjustment AJE-X04: annual leave accrued to 30 September 2026"),
    (
        "AJE-X05",
        2,
        "Audit adjustment AJE-X05: interest accrued on the Business Savings Account to 30 September 2026, "
        "as confirmed by the bank",
    ),
]


@pytest.mark.external  # Xero 补造的更正分录
@pytest.mark.parametrize(("loaded", "expected"), [([], []), (["AJE-X01", "AJE-X05"], ["AJE-X01", "AJE-X05"])])
def test_adjustment_entries_of_xero(api: SimpleNamespace, loaded: list[str], expected: list[str]) -> None:
    """验收 2: Xero 的 AJE-X01 至 AJE-X05 共 5 笔, 凭证号, 入账日, 摘要与行数同设计文档 (XERO_ADJUSTMENTS); 总账里还
    没有时都未载入, D-CN1 载入 AJE-X01, AJE-X05 之后这两笔为已载入 (按总账里有没有该凭证号的行判断)."""
    from tests.xero_fakes import XERO_DATASET, XeroFakeDatasetLoader, xero_adjustments

    lines = {("journal_entry", r.id): r.model_dump() for r in xero_adjustments(loaded)} if loaded else {}
    deps = use_engagement(api, dataset=XERO_DATASET)
    deps.financial_store = FakeFinancialStore(records=lines)
    deps.datasets = XeroFakeDatasetLoader(FakeFinancialStore())
    response = api.client.get("/api/v1/engagements/ENG-2026-001/adjustment-entries", headers=_auth(api))
    assert response.status_code == 200
    rows = response.json()
    assert [(r["entry_no"], r["posting_date"], r["line_count"], r["description"]) for r in rows] == [
        (entry_no, "2026-09-30", line_count, description) for entry_no, line_count, description in XERO_ADJUSTMENTS
    ]
    assert [r["entry_no"] for r in rows if r["loaded"]] == expected


class UnreadDatasets:
    """调一下就失败的载入器: 没有数据集的项目不该读它."""

    def adjustment_entries(self, dataset: str) -> list[Any]:
        raise AssertionError(f"不该读数据集 {dataset}")


@pytest.mark.parametrize(
    ("dataset", "datasets"),
    [("demo", lambda: CsvDatasetLoader(REPO / "data")), (None, UnreadDatasets)],
    ids=["numbat", "no_dataset"],
)
def test_adjustment_entries_empty_without_adjustments(
    api: SimpleNamespace, dataset: str | None, datasets: Callable[[], Any]
) -> None:
    """验收 2: Numbat (仓库里的 data/demo, 真实 CSV 载入器: 没有更正分录文件) 为 []; 没有数据集的新客户为 [] 且不读
    数据集 (载入器一调就失败)."""
    deps = use_engagement(api, dataset=dataset)
    deps.datasets = datasets()
    deps.financial_store = FakeFinancialStore()
    response = api.client.get("/api/v1/engagements/ENG-2026-001/adjustment-entries", headers=_auth(api))
    assert (response.status_code, response.json()) == (200, [])


def test_misstatements_carry_lifecycle_and_notices(api: SimpleNamespace) -> None:
    """验收 3: items 为各错报的最新版本, 带生命周期字段与科目代码; correction_notices 为 Store 中的全部更正通知, 按
    提交时间; 重要性与意见取总览."""
    early, late = datetime(2026, 10, 20, 3, 30, tzinfo=UTC), datetime(2026, 10, 22, 4, tzinfo=UTC)
    first = CorrectionNotice(id="D-CN1", decided_by="USR-1", decided_at=early, **POSTED)
    second = CorrectionNotice(
        id="D-STOP", kind="stop_waiting", misstatement_ids=["M-1"], reason="r", decided_by="USR-1", decided_at=late
    )
    verifying = Misstatement.model_validate(
        MISSTATEMENT.model_dump() | {"version": 4, "status": "verifying", "correction_notice_id": "D-CN1"}
    )
    deps = use_engagement(api)
    deps.output_store = FakeOutputStore([MISSTATEMENT, verifying, second, first])
    api.client.app.state.query.overview = AsyncMock(
        return_value=SimpleNamespace(materiality_cents=32431, opinion_route=None)
    )
    response = api.client.get("/api/v1/engagements/ENG-2026-001/misstatements", headers=_auth(api))
    assert response.status_code == 200
    body = response.json()
    [item] = body["items"]
    assert (item["version"], item["status"], item["account_code"], item["correction_notice_id"]) == (
        4,
        "verifying",
        "710",
        "D-CN1",
    )
    assert [n["id"] for n in body["correction_notices"]] == ["D-CN1", "D-STOP"]
    assert (body["materiality_cents"], body["schedule"]) == (32431, None)


MISSTATEMENT = Misstatement(  # 第 1 版 (identified), 由人对判断认定; 科目代码由草稿带入
    id="M-D-XJ499",
    judgment_id="J-GEN-02-1-XJ-499",
    exception_id=None,
    review_record_id="D-1",
    type="factual",
    account="Office Equipment",
    assertion="Accuracy",
    amount_cents=75000,
    direction="overstated",
    account_code="710",
)


# ── 工作语言 (需求 R339) ──


def language_store(api: SimpleNamespace, **fields: Any) -> FakeEngagementStore:
    """本人的项目 (改几个字段, 如锁定) 换成能改工作语言的假项目存储, 返回它."""
    deps = api.client.app.state.deps
    store = FakeEngagementStore(engagements=[deps.engagement_store.mine.model_copy(update=fields)])
    deps.engagement_store = store
    return store


LANGUAGE_URL = "/api/v1/engagements/ENG-2026-001/working-language"


def test_working_language_change_is_trailed_once(api: SimpleNamespace) -> None:
    """验收 2: en 改为 zh 返回改后的项目, decision 留痕一行 (action working_language, from en, to zh, 写入人为当前
    用户); 再提交 zh (与当前相同) 不改也不留痕; 改回 en 再留一行."""
    store = language_store(api)
    changed = api.client.put(LANGUAGE_URL, headers=_auth(api), json={"language": "zh"})
    assert (changed.status_code, changed.json()["working_language"]) == (200, "zh")
    assert store.engagements[0].working_language == "zh"
    same = api.client.put(LANGUAGE_URL, headers=_auth(api), json={"language": "zh"})
    assert (same.status_code, same.json()["working_language"]) == (200, "zh")
    api.client.put(LANGUAGE_URL, headers=_auth(api), json={"language": "en"})
    rows = [(e.kind, e.actor, e.payload) for e in api.trail.entries]
    assert rows == [
        (TrailKind.decision, "USR-1", {"action": "working_language", "from": "en", "to": "zh"}),
        (TrailKind.decision, "USR-1", {"action": "working_language", "from": "zh", "to": "en"}),
    ]


CREATE = {"period_start": "2025-07-01", "period_end": "2026-06-30", "dataset": "demo"}  # POST /engagements 的请求体


@pytest.mark.parametrize(
    ("fields", "method", "url", "body", "status", "code"),
    [
        ({}, "PUT", LANGUAGE_URL, {"language": "fr"}, 422, "INVALID_VALUE"),
        ({}, "PUT", LANGUAGE_URL, {"language": "ZH"}, 422, "INVALID_VALUE"),
        ({"locked": True}, "PUT", LANGUAGE_URL, {"language": "zh"}, 423, "ENGAGEMENT_LOCKED"),
        ({}, "PUT", LANGUAGE_URL.replace("001", "002"), {"language": "zh"}, 404, "NOT_FOUND"),  # 不是本人的项目
        ({}, "POST", "/api/v1/engagements", {**CREATE, "working_language": "fr"}, 422, "INVALID_VALUE"),
    ],
    ids=["unknown", "case", "locked", "not_mine", "create_unknown"],
)
def test_working_language_change_rejected(
    api: SimpleNamespace, fields: dict[str, Any], method: str, url: str, body: dict[str, str], status: int, code: str
) -> None:
    """验收 1, 2: 取值不在 zh, en 422 INVALID_VALUE (区分大小写; 建项目与修改同一个校验 parse_language, 建项目时
    不建); 项目锁定 423 ENGAGEMENT_LOCKED; 别人的项目 404. 都不改项目, 不留痕."""
    store = language_store(api, **fields)
    response = api.client.request(method, url, headers=_auth(api), json=body)
    assert (response.status_code, response.json()["detail"]["code"]) == (status, code)
    assert store.engagements[0].working_language == "en" and api.trail.entries == []


def test_working_language_change_undone_when_trail_fails(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """审核 R339-01: 留痕写失败时把语言改回原值再抛错: 项目仍是 en, 也没有留痕, 不留下没有留痕的修改."""
    store = language_store(api)
    monkeypatch.setattr(api.trail, "record", AsyncMock(side_effect=RuntimeError("trail down")))
    with pytest.raises(RuntimeError, match="trail down"):
        api.client.put(LANGUAGE_URL, headers=_auth(api), json={"language": "zh"})
    assert store.engagements[0].working_language == "en" and api.trail.entries == []
