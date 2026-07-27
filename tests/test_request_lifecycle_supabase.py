from datetime import UTC, datetime
from uuid import UUID

import pytest
from postgrest.exceptions import APIError

from app.intake.models import IntakeStatus
from app.request_lifecycle.exceptions import (
    LifecycleConcurrentUpdateError,
    LifecycleIdempotencyConflictError,
    LifecycleOwnershipError,
    LifecyclePersistenceError,
    LifecycleRequestNotFoundError,
    LifecycleTransitionError,
    RequestAlreadyCancelledError,
    RequestAlreadyRegisteredError,
    RequestNotReadyError,
)
from app.request_lifecycle.models import LifecycleCommandType, LifecycleMutation
from app.request_lifecycle.repositories import SupabaseRequestLifecycleRepository

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("33333333-3333-4333-8333-333333333333")


class Response:
    def __init__(self, data):
        self.data = data


class RpcCall:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error

    def execute(self):
        if self.error:
            if isinstance(self.error, Exception):
                raise self.error
            raise RuntimeError(self.error)
        return Response(self.data)


_DEFAULT = object()


class FakeClient:
    def __init__(self, data=_DEFAULT, error=None):
        self.data = successful_response() if data is _DEFAULT else data
        self.error = error
        self.rpc_name = None
        self.rpc_payload = None

    def rpc(self, name, payload):
        self.rpc_name = name
        self.rpc_payload = payload
        return RpcCall(self.data, self.error)


def successful_response():
    now = datetime.now(UTC).isoformat()
    return {
        "result": {
            "request_id": str(REQUEST_ID),
            "user_id": str(USER_ID),
            "request_number": "PR-2026-000001",
            "status": "new",
            "intake_status": "completed",
            "version": 9,
            "registered_at": now,
            "confirmed_at": now,
            "cancelled_at": None,
            "cancellation_reason": None,
            "replayed": False,
            "request_card": None,
            "approval_route": None,
            "editable": False,
            "editable_field_codes": [],
            "instruction": None,
            "warnings": [],
        },
        "replayed": False,
    }


def mutation(command_type=LifecycleCommandType.CONFIRM):
    return LifecycleMutation(
        user_id=USER_ID,
        request_id=REQUEST_ID,
        command_type=command_type,
        expected_version=8,
        idempotency_key="key-1",
        fingerprint="abc",
        request_data={"schema_version": 1},
        intake_status=IntakeStatus.COMPLETED,
    )


@pytest.mark.parametrize(
    ("method", "rpc_name", "command_type"),
    [
        ("confirm_and_register", "confirm_request", LifecycleCommandType.CONFIRM),
        (
            "return_to_editing",
            "return_request_to_editing",
            LifecycleCommandType.RETURN_TO_EDITING,
        ),
        ("cancel_draft", "cancel_request", LifecycleCommandType.CANCEL),
    ],
)
def test_supabase_lifecycle_uses_single_rpc(method, rpc_name, command_type) -> None:
    client = FakeClient()
    repository = SupabaseRequestLifecycleRepository(client)  # type: ignore[arg-type]
    result = getattr(repository, method)(mutation(command_type))
    assert client.rpc_name == rpc_name
    assert client.rpc_payload == {
        "command": mutation(command_type).model_dump(mode="json")
    }
    assert result.result.request_number == "PR-2026-000001"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("42501 lifecycle_ownership_mismatch", LifecycleOwnershipError),
        ("P0002 lifecycle_request_not_found", LifecycleRequestNotFoundError),
        ("40001 concurrent_lifecycle_update", LifecycleConcurrentUpdateError),
        ("23505 lifecycle_idempotency_conflict", LifecycleIdempotencyConflictError),
        ("55000 request_not_ready", RequestNotReadyError),
        ("55000 request_already_registered", RequestAlreadyRegisteredError),
        ("55000 request_already_cancelled", RequestAlreadyCancelledError),
        ("55000 lifecycle_transition_not_allowed", LifecycleTransitionError),
        ("network timeout", LifecyclePersistenceError),
    ],
)
def test_supabase_lifecycle_errors_are_safe(error, expected) -> None:
    repository = SupabaseRequestLifecycleRepository(  # type: ignore[arg-type]
        FakeClient(error=error)
    )
    with pytest.raises(expected):
        repository.confirm_and_register(mutation())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            APIError(
                {
                    "message": "concurrent_lifecycle_update",
                    "code": "40001",
                    "hint": None,
                    "details": None,
                }
            ),
            LifecycleConcurrentUpdateError,
        ),
        (
            APIError(
                {
                    "message": "lifecycle_ownership_mismatch",
                    "code": "42501",
                    "hint": None,
                    "details": None,
                }
            ),
            LifecycleOwnershipError,
        ),
        (
            APIError(
                {
                    "message": "lifecycle_dialog_mismatch",
                    "code": "55000",
                    "hint": None,
                    "details": None,
                }
            ),
            LifecycleTransitionError,
        ),
    ],
)
def test_supabase_maps_real_postgrest_api_errors(error, expected) -> None:
    repository = SupabaseRequestLifecycleRepository(  # type: ignore[arg-type]
        FakeClient(error=error)
    )
    with pytest.raises(expected):
        repository.confirm_and_register(mutation())


def test_supabase_accepts_real_list_wrapped_replay_shape() -> None:
    payload = successful_response()
    payload["replayed"] = True
    payload["result"]["replayed"] = True
    repository = SupabaseRequestLifecycleRepository(  # type: ignore[arg-type]
        FakeClient(data=[payload])
    )
    saved = repository.confirm_and_register(mutation())
    assert saved.replayed is True
    assert saved.result.replayed is True


@pytest.mark.parametrize(
    ("command_type", "status", "intake_status", "editable"),
    [
        (LifecycleCommandType.RETURN_TO_EDITING, "draft", "editing", True),
        (LifecycleCommandType.CANCEL, "cancelled", "cancelled", False),
    ],
)
def test_supabase_parses_edit_and_cancel_response_shapes(
    command_type, status, intake_status, editable
) -> None:
    payload = successful_response()
    payload["result"].update(
        request_number=None,
        status=status,
        intake_status=intake_status,
        registered_at=None,
        confirmed_at=None,
        editable=editable,
    )
    repository = SupabaseRequestLifecycleRepository(  # type: ignore[arg-type]
        FakeClient(data=[payload])
    )
    method = (
        repository.return_to_editing
        if command_type == LifecycleCommandType.RETURN_TO_EDITING
        else repository.cancel_draft
    )
    saved = method(mutation(command_type))
    assert saved.result.status.value == status
    assert saved.result.intake_status.value == intake_status
    assert saved.result.editable is editable


@pytest.mark.parametrize("data", [None, {}, [], {"replayed": False}])
def test_malformed_lifecycle_rpc_response_is_safe(data) -> None:
    repository = SupabaseRequestLifecycleRepository(  # type: ignore[arg-type]
        FakeClient(data=data)
    )
    with pytest.raises(LifecyclePersistenceError):
        repository.confirm_and_register(mutation())


def test_supabase_failure_audit_uses_safe_rpc() -> None:
    client = FakeClient(data=None)
    repository = SupabaseRequestLifecycleRepository(client)  # type: ignore[arg-type]
    repository.append_lifecycle_failure(
        USER_ID,
        REQUEST_ID,
        LifecycleCommandType.CONFIRM,
        "key-1",
        8,
        "RequestNotReadyError",
        12,
    )
    assert client.rpc_name == "record_request_lifecycle_failure"
    assert client.rpc_payload["event"] == {
        "user_id": str(USER_ID),
        "request_id": str(REQUEST_ID),
        "command_type": "confirm",
        "idempotency_key": "key-1",
        "expected_version": 8,
        "error_type": "RequestNotReadyError",
        "duration_ms": 12,
    }
