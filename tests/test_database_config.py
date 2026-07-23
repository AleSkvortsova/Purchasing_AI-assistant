from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_database_health_service,
    get_supabase_repository,
)
from app.core.config import get_settings
from app.main import app
from app.repositories.memory import InMemoryRequestRepository
from app.services.database import DatabaseHealthService


def test_database_health_with_available_repository() -> None:
    service = DatabaseHealthService(InMemoryRequestRepository())
    app.dependency_overrides[get_database_health_service] = lambda: service

    with TestClient(app) as client:
        response = client.get("/api/v1/db/health")

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "configured": True,
        "detail": "Database connection is healthy",
    }


def test_application_works_without_supabase(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    get_settings.cache_clear()
    get_supabase_repository.cache_clear()

    try:
        with TestClient(app) as client:
            root_response = client.get("/")
            health_response = client.get("/health")
            db_response = client.get("/api/v1/db/health")
            request_response = client.post(
                "/api/v1/requests",
                json={"user_id": "11111111-1111-4111-8111-111111111111"},
            )

        assert root_response.status_code == 200
        assert health_response.status_code == 200
        assert db_response.status_code == 200
        assert db_response.json() == {
            "status": "not_configured",
            "configured": False,
            "detail": "Supabase is not configured",
        }
        assert request_response.status_code == 503
        assert request_response.json()["detail"] == "Supabase is not configured"
    finally:
        get_settings.cache_clear()
        get_supabase_repository.cache_clear()
