"""登录、项目归属与决定错误码. 不启动数据库或编排图."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import Engagement, Phase, User
from backend.core.security import create_access_token, decode_access_token, hash_password
from backend.main import create_app
from backend.persistence.stores import PostgresUserStore


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
        self.rows: dict[str, tuple[User, str]] = {self.user.email: (self.user, hash_password("secret"))}

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
    deps = SimpleNamespace(users=users, engagement_store=_Engagements())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        app.state.deps = deps
        app.state.runner = SimpleNamespace(start=AsyncMock(), resume=AsyncMock())
        app.state.query = SimpleNamespace(gate=AsyncMock(return_value=None), overview=AsyncMock())
        app.state.decisions = decisions
        yield

    monkeypatch.setattr("backend.main.lifespan", lifespan)
    with TestClient(create_app()) as client:
        yield SimpleNamespace(client=client, decisions=decisions, token=create_access_token(users.user.id), users=users)


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
                "password_hash": hash_password(f"password-{index}"),
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
    hashed = hash_password("secret")
    api.users.rows = {
        key: (User(id=f"USR-{index}", email=key, full_name="Legacy"), hashed)
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


def test_other_owners_engagement_is_not_found(api: SimpleNamespace) -> None:
    response = api.client.get("/api/v1/engagements/ENG-2026-002", headers=_auth(api))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


def test_unknown_engagement_is_not_found(api: SimpleNamespace) -> None:
    response = api.client.get("/api/v1/engagements/ENG-missing", headers=_auth(api))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


def test_get_gate_when_absent(api: SimpleNamespace) -> None:
    response = api.client.get("/api/v1/engagements/ENG-2026-001/gate", headers=_auth(api))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_AT_GATE"


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
    ],
)
def test_gate_decision_error_mapping(api: SimpleNamespace, code: str, status: int) -> None:
    api.decisions.submit.side_effect = DecisionRejected(code, "目标对象已更新到 version 3")
    response = api.client.post(
        "/api/v1/engagements/ENG-2026-001/gate-decisions",
        headers=_auth(api),
        json={"gate": "ACC-01", "target_id": "SCORECARD", "target_version": 1, "decision": "approve"},
    )
    assert response.status_code == status
    assert response.json()["detail"] == {"code": code, "message": "目标对象已更新到 version 3"}
