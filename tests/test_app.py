from fastapi.testclient import TestClient

from backend.main import create_app


def test_health() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
