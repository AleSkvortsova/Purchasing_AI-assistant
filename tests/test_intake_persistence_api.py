from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_intake_persistence_orchestrator,
    get_optional_intake_persistence_orchestrator,
)
from app.intake_persistence.exceptions import ConcurrentIntakeUpdateError
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.main import app
from app.schemas.common import RequestStatus
from app.schemas.request import RequestRead

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def storage() -> InMemoryIntakeStorage:
    return InMemoryIntakeStorage()


@pytest.fixture
def orchestrator(storage) -> PersistentIntakeOrchestrator:
    return PersistentIntakeOrchestrator(InMemoryIntakePersistenceRepository(storage))


@pytest.fixture
def client(orchestrator) -> Iterator[TestClient]:
    app.dependency_overrides[get_intake_persistence_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_optional_intake_persistence_orchestrator] = lambda: (
        orchestrator
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def payload(values=None, *, key="message-1", request_id=None, user_id=USER_ID):
    return {
        "user_id": str(user_id),
        "request_id": str(request_id) if request_id else None,
        "idempotency_key": key,
        "update": {
            "values": values or {},
            "source": "user",
            "explicit_correction": False,
            "evidence_by_field": {},
        },
    }


def test_health_first_step_continue_and_active(client: TestClient) -> None:
    assert client.get("/api/v1/intake-sessions/health").json()["status"] == "ok"
    first = client.post(
        "/api/v1/intake-sessions/step",
        json=payload({"procurement_type": "goods", "item_name": "Монитор"}),
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["created_new_request"] is True
    second = client.post(
        "/api/v1/intake-sessions/step",
        json=payload({"category_code": "G03"}, key="message-2"),
    )
    assert second.status_code == 200
    assert second.json()["request_id"] == first_body["request_id"]
    active = client.get(f"/api/v1/intake-sessions/{USER_ID}/active")
    assert active.status_code == 200
    assert active.json()["intake_result"]["draft"]["category_code"] == "G03"


def test_api_idempotency_replay_and_conflict(client: TestClient) -> None:
    first = client.post(
        "/api/v1/intake-sessions/step",
        json=payload({"item_name": "Монитор"}),
    )
    replay = client.post(
        "/api/v1/intake-sessions/step",
        json=payload({"item_name": "Монитор"}),
    )
    conflict = client.post(
        "/api/v1/intake-sessions/step",
        json=payload({"item_name": "Ноутбук"}),
    )
    assert first.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["request_version"] == first.json()["request_version"]
    assert conflict.status_code == 409


def test_api_active_not_found_and_validation(client: TestClient) -> None:
    assert client.get(f"/api/v1/intake-sessions/{USER_ID}/active").status_code == 404
    invalid = client.post(
        "/api/v1/intake-sessions/step",
        json={"user_id": "not-a-uuid", "update": {"values": {}}},
    )
    assert invalid.status_code == 422


def test_api_ownership_error(client: TestClient, storage) -> None:
    now = datetime.now(UTC)
    foreign = RequestRead(
        id=uuid4(),
        user_id=OTHER_USER,
        data={},
        status=RequestStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    storage.requests[foreign.id] = foreign
    response = client.post(
        "/api/v1/intake-sessions/step",
        json=payload({}, request_id=foreign.id),
    )
    assert response.status_code == 403


def test_api_concurrent_update_is_409(storage) -> None:
    now = datetime.now(UTC)
    existing = RequestRead(
        id=uuid4(),
        user_id=USER_ID,
        data={},
        status=RequestStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    storage.requests[existing.id] = existing

    class ConcurrentRepository(InMemoryIntakePersistenceRepository):
        def save_step(self, command):
            raise ConcurrentIntakeUpdateError("stale version")

    orchestrator = PersistentIntakeOrchestrator(ConcurrentRepository(storage))
    app.dependency_overrides[get_intake_persistence_orchestrator] = lambda: orchestrator
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/intake-sessions/step",
            json=payload({"item_name": "Монитор"}, request_id=existing.id),
        )
    app.dependency_overrides.clear()
    assert response.status_code == 409


def test_api_partial_first_step_is_safe_503() -> None:
    storage = InMemoryIntakeStorage(fail_at="dialog")
    orchestrator = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage)
    )
    app.dependency_overrides[get_intake_persistence_orchestrator] = lambda: orchestrator
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/intake-sessions/step",
            json=payload({"item_name": "Монитор"}),
        )
    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["detail"]["recovery_required"] is True
    assert "traceback" not in response.text.casefold()


def test_endpoint_does_not_call_openai(client: TestClient, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("OpenAI must not be called")

    monkeypatch.setattr("openai.OpenAI", forbidden)
    response = client.post(
        "/api/v1/intake-sessions/step",
        json=payload({"item_name": "Монитор"}),
    )
    assert response.status_code == 200
