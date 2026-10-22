from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


def test_health(monkeypatch: pytest.MonkeyPatch) -> None:
    # Health routing is a unit test; recovery with PostgreSQL is tested separately.
    monkeypatch.setattr("backend.orchestration.runner.GraphRunner.recover", AsyncMock())
    monkeypatch.setattr("backend.services.data_service.DataService.recover", AsyncMock())
    monkeypatch.setattr("backend.main.recover_archives", AsyncMock())
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
