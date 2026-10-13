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
from backend.core.security import create_access_token, hash_password
from backend.main import create_app


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
    def __init__(self) -> None:
        self.user = User(id="USR-1", email="rca@example.com", full_name="Demo Auditor")
        self._hash = hash_password("secret")

    async def by_email(self, email: str) -> tuple[User, str] | None:
        if email == self.user.email:
            return self.user, self._hash
        return None

    async def get(self, user_id: str) -> User | None:
        return self.user if user_id == self.user.id else None


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
        yield SimpleNamespace(client=client, decisions=decisions, token=create_access_token(users.user.id))


def _auth(api: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {api.token}"}


def test_login_and_me(api: SimpleNamespace) -> None:
    ok = api.client.post("/api/v1/auth/login", data={"username": "rca@example.com", "password": "secret"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["token_type"] == "bearer"
    assert api.client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {body['access_token']}"}).json()[
        "email"
    ] == "rca@example.com"


def test_login_bad_credentials(api: SimpleNamespace) -> None:
    bad = api.client.post("/api/v1/auth/login", data={"username": "rca@example.com", "password": "nope"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == {"code": "BAD_CREDENTIALS", "message": "邮箱或密码不正确"}


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
