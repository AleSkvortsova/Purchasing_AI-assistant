from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_request_service
from app.main import app
from app.repositories.memory import InMemoryRequestRepository
from app.schemas.common import RequestStatus, RequestType
from app.schemas.request import RequestRead
from app.services.requests import RequestService

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
def repository() -> InMemoryRequestRepository:
    return InMemoryRequestRepository()


@pytest.fixture
def service(repository: InMemoryRequestRepository) -> RequestService:
    return RequestService(repository)


@pytest.fixture
def client(service: RequestService) -> Iterator[TestClient]:
    app.dependency_overrides[get_request_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def request_payload() -> dict[str, object]:
    return {
        "user_id": str(USER_ID),
        "request_type": "product",
        "category_code": "G02",
        "title": "Мониторы для отдела продаж",
        "data": {"quantity": 10, "unit": "шт."},
    }


def test_create_draft(client: TestClient) -> None:
    response = client.post("/api/v1/requests", json=request_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft"
    assert body["request_number"] is None
    assert body["title"] == "Мониторы для отдела продаж"


def test_read_draft(client: TestClient) -> None:
    created = client.post("/api/v1/requests", json=request_payload()).json()

    response = client.get(f"/api/v1/requests/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_partial_update_merges_draft_data(client: TestClient) -> None:
    created = client.post("/api/v1/requests", json=request_payload()).json()

    response = client.patch(
        f"/api/v1/requests/{created['id']}",
        json={"title": "Мониторы 27 дюймов", "data": {"diagonal": 27}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Мониторы 27 дюймов"
    assert body["data"] == {"quantity": 10, "unit": "шт.", "diagonal": 27}


def test_get_missing_request_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/requests/{uuid4()}")

    assert response.status_code == 404
    assert "was not found" in response.json()["detail"]


def test_non_draft_update_is_forbidden() -> None:
    now = datetime.now(UTC)
    existing = RequestRead(
        id=uuid4(),
        user_id=USER_ID,
        request_type=RequestType.PRODUCT,
        category_code="G02",
        title="Registered request",
        data={},
        request_number="REQ-1",
        status=RequestStatus.NEW,
        created_at=now,
        updated_at=now,
        confirmed_at=now,
    )
    service = RequestService(InMemoryRequestRepository([existing]))
    app.dependency_overrides[get_request_service] = lambda: service

    with TestClient(app) as client:
        response = client.patch(
            f"/api/v1/requests/{existing.id}",
            json={"title": "Forbidden update"},
        )

    app.dependency_overrides.clear()
    assert response.status_code == 409
    assert response.json()["detail"] == "Only draft requests can be updated"
