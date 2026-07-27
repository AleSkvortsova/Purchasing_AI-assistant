from collections.abc import Iterator
from datetime import date, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_request_lifecycle_service
from app.intake.models import IntakeFieldUpdate
from app.intake.service import RequestIntakeService
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.main import app
from app.request_lifecycle.exceptions import LifecyclePersistenceError
from app.request_lifecycle.repositories import InMemoryRequestLifecycleRepository
from app.request_lifecycle.service import RequestLifecycleService
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from scripts.validate_approval_rules import load_rule_seed

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER = UUID("22222222-2222-4222-8222-222222222222")


def core() -> RequestIntakeService:
    _, base, additional = load_rule_seed()
    return RequestIntakeService(
        ApprovalRuleService(InMemoryApprovalRuleRepository(base, additional))
    )


@pytest.fixture
def lifecycle_context():
    storage = InMemoryIntakeStorage()
    intake_core = core()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), intake_core
    )
    service = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), intake_core
    )
    return storage, intake, service


@pytest.fixture
def client(lifecycle_context) -> Iterator[TestClient]:
    _, _, service = lifecycle_context
    app.dependency_overrides[get_request_lifecycle_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def ready(intake):
    return intake.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={
                "procurement_type": "goods",
                "category_code": "G03",
                "item_name": "Монитор",
                "quantity": "2",
                "unit": "шт.",
                "specifications": "27 дюймов",
                "analogs_allowed": True,
                "amount": "180000",
                "budget_status": "budgeted",
                "desired_delivery_date": (
                    date.today() + timedelta(days=30)
                ).isoformat(),
                "delivery_location": "Офис",
                "business_justification": "Рабочие места",
                "department": "ИТ",
                "contact_person": "Анна",
            }
        ),
    )


def command(version, key="command-1", user_id=USER_ID):
    return {
        "user_id": str(user_id),
        "expected_version": version,
        "idempotency_key": key,
    }


def test_confirmation_confirm_replay_and_by_number(client, lifecycle_context) -> None:
    _, intake, _ = lifecycle_context
    saved = ready(intake)
    confirmation = client.get(
        f"/api/v1/requests/{saved.request_id}/confirmation",
        params={"user_id": str(USER_ID)},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["confirmable"] is True
    confirmed = client.post(
        f"/api/v1/requests/{saved.request_id}/confirm",
        json=command(saved.request_version),
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["status"] == "new"
    replay = client.post(
        f"/api/v1/requests/{saved.request_id}/confirm",
        json=command(1),
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    found = client.get(
        f"/api/v1/requests/by-number/{body['request_number']}",
        params={"user_id": str(USER_ID)},
    )
    assert found.status_code == 200
    assert found.json()["id"] == str(saved.request_id)
    foreign = client.get(
        f"/api/v1/requests/by-number/{body['request_number']}",
        params={"user_id": str(OTHER_USER)},
    )
    assert foreign.status_code == 403
    unknown = client.get(
        "/api/v1/requests/by-number/PR-2026-999999",
        params={"user_id": str(USER_ID)},
    )
    assert unknown.status_code == 404


def test_not_ready_ownership_stale_and_validation(client, lifecycle_context) -> None:
    _, intake, _ = lifecycle_context
    saved = intake.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"item_name": "Мышь"})
    )
    not_ready = client.post(
        f"/api/v1/requests/{saved.request_id}/confirm",
        json=command(saved.request_version),
    )
    assert not_ready.status_code == 409
    assert not_ready.json()["detail"]["confirmation_view"]["confirmable"] is False
    ownership = client.get(
        f"/api/v1/requests/{saved.request_id}/confirmation",
        params={"user_id": str(OTHER_USER)},
    )
    assert ownership.status_code == 403
    stale = client.post(
        f"/api/v1/requests/{saved.request_id}/cancel",
        json=command(1, "stale"),
    )
    assert stale.status_code == 409
    invalid = client.post(
        f"/api/v1/requests/{saved.request_id}/cancel",
        json={"user_id": str(USER_ID), "expected_version": 0, "idempotency_key": ""},
    )
    assert invalid.status_code == 422


def test_return_to_editing_cancel_and_idempotency_conflict(
    client, lifecycle_context
) -> None:
    _, intake, _ = lifecycle_context
    saved = ready(intake)
    editing = client.post(
        f"/api/v1/requests/{saved.request_id}/return-to-editing",
        json=command(saved.request_version, "edit"),
    )
    assert editing.status_code == 200
    assert editing.json()["intake_status"] == "editing"
    cancelled = client.post(
        f"/api/v1/requests/{saved.request_id}/cancel",
        json={
            **command(editing.json()["version"], "cancel"),
            "reason": "Не актуально",
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    conflict = client.post(
        f"/api/v1/requests/{saved.request_id}/cancel",
        json={**command(1, "cancel"), "reason": "Другая причина"},
    )
    assert conflict.status_code == 409


def test_lifecycle_api_never_calls_openai(
    client, lifecycle_context, monkeypatch
) -> None:
    _, intake, _ = lifecycle_context
    saved = ready(intake)

    def forbidden(*args, **kwargs):
        raise AssertionError("OpenAI must not be called")

    monkeypatch.setattr("openai.OpenAI", forbidden)
    response = client.get(
        f"/api/v1/requests/{saved.request_id}/confirmation",
        params={"user_id": str(USER_ID)},
    )
    assert response.status_code == 200


def test_lifecycle_api_not_found_and_persistence_failure(lifecycle_context) -> None:
    storage, _, _ = lifecycle_context
    normal = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core()
    )
    app.dependency_overrides[get_request_lifecycle_service] = lambda: normal
    with TestClient(app) as test_client:
        missing = test_client.get(
            "/api/v1/requests/33333333-3333-4333-8333-333333333333/confirmation",
            params={"user_id": str(USER_ID)},
        )
    assert missing.status_code == 404

    class FailedRepository(InMemoryRequestLifecycleRepository):
        def load_for_lifecycle(self, request_id):
            raise LifecyclePersistenceError("Хранилище lifecycle недоступно")

    failed = RequestLifecycleService(FailedRepository(storage), core())
    app.dependency_overrides[get_request_lifecycle_service] = lambda: failed
    with TestClient(app) as test_client:
        unavailable = test_client.get(
            "/api/v1/requests/33333333-3333-4333-8333-333333333333/confirmation",
            params={"user_id": str(USER_ID)},
        )
    app.dependency_overrides.clear()
    assert unavailable.status_code == 503
    assert "traceback" not in unavailable.text.casefold()
