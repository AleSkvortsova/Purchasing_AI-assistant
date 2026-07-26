from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.intake.models import IntakeFieldUpdate, IntakeStatus
from app.intake_persistence.exceptions import (
    ActiveDraftNotFoundError,
    ConcurrentIntakeUpdateError,
    IdempotencyConflictError,
    IntakePersistenceRepositoryError,
    RequestNotEditableError,
    RequestOwnershipError,
)
from app.intake_persistence.models import (
    IdempotencyRecord,
    PersistenceMessageLog,
    PersistentDialogState,
    SaveIntakeStepCommand,
)
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    SupabaseIntakePersistenceRepository,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.schemas.common import RequestStatus
from app.schemas.request import RequestCreate, RequestRead

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class Response:
    def __init__(self, data):
        self.data = data


class RpcCall:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error

    def execute(self):
        if self.error:
            raise RuntimeError(self.error)
        return Response(self.data)


class FakeClient:
    def __init__(self, data="default", error=None):
        self.rpc_name = None
        self.rpc_payload = None
        self.data = data
        self.error = error

    def rpc(self, name, payload):
        self.rpc_name = name
        self.rpc_payload = payload
        data = (
            {
                "request_version": 2,
                "dialog_state": payload["dialog_state"] | {"state_version": 2},
            }
            if self.data == "default"
            else self.data
        )
        return RpcCall(data, self.error)


def command() -> SaveIntakeStepCommand:
    request_id = uuid4()
    dialog = PersistentDialogState(
        user_id=USER_ID,
        request_id=request_id,
        intake_status=IntakeStatus.COLLECTING,
        state_version=2,
    )
    incoming = PersistenceMessageLog(
        user_id=USER_ID,
        request_id=request_id,
        direction="incoming",
        message_type="structured_update",
        payload={
            "amount": Decimal("180000.01"),
            "date": date(2030, 1, 2),
            "user_id": USER_ID,
        },
    )
    outgoing = PersistenceMessageLog(
        user_id=USER_ID,
        request_id=request_id,
        direction="outgoing",
        message_type="question",
    )
    return SaveIntakeStepCommand(
        request_id=request_id,
        expected_version=1,
        request_type="product",
        request_data={"schema_version": 1},
        dialog_state=dialog,
        incoming_log=incoming,
        outgoing_log=outgoing,
    )


def test_supabase_save_uses_atomic_rpc_and_json_payload() -> None:
    client = FakeClient()
    repository = SupabaseIntakePersistenceRepository(client)  # type: ignore[arg-type]
    save_command = command()
    saved = repository.save_step(save_command)
    assert client.rpc_name == "save_intake_step"
    assert client.rpc_payload["request_id"] == str(save_command.request_id)
    assert client.rpc_payload["incoming_log"]["payload"] == {
        "amount": "180000.01",
        "date": "2030-01-02",
        "user_id": str(USER_ID),
    }
    assert saved.request_version == 2


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        ("40001 concurrent_intake_update", ConcurrentIntakeUpdateError),
        ("42501 intake_request_ownership_mismatch", RequestOwnershipError),
        ("55000 intake_request_not_editable", RequestNotEditableError),
        ("P0002 intake_request_not_found", ActiveDraftNotFoundError),
        ("23505 idempotency_conflict", IdempotencyConflictError),
        ("network timeout", IntakePersistenceRepositoryError),
    ],
)
def test_supabase_rpc_errors_are_mapped(error, expected_error) -> None:
    repository = SupabaseIntakePersistenceRepository(  # type: ignore[arg-type]
        FakeClient(error=error)
    )
    with pytest.raises(expected_error):
        repository.save_step(command())


@pytest.mark.parametrize("data", [None, {}, [], {"request_version": 2}])
def test_empty_or_malformed_rpc_response_is_safe_error(data) -> None:
    repository = SupabaseIntakePersistenceRepository(  # type: ignore[arg-type]
        FakeClient(data=data)
    )
    with pytest.raises(IntakePersistenceRepositoryError):
        repository.save_step(command())


def test_unique_violation_replays_existing_idempotency(monkeypatch) -> None:
    in_memory = InMemoryIntakePersistenceRepository()
    persisted = PersistentIntakeOrchestrator(in_memory).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "Монитор"}),
        idempotency_key="key-1",
    )
    record = in_memory.find_idempotency(USER_ID, "key-1")
    assert record is not None
    save_command = command()
    save_command.idempotency_record = IdempotencyRecord(
        user_id=USER_ID,
        key="key-1",
        fingerprint=record.fingerprint,
        result=persisted,
    )
    repository = SupabaseIntakePersistenceRepository(  # type: ignore[arg-type]
        FakeClient(error="23505 duplicate key")
    )
    monkeypatch.setattr(repository, "find_idempotency", lambda *_: record)
    saved = repository.save_step(save_command)
    assert saved.replayed is True
    assert saved.request_version == persisted.request_version


class Query:
    def __init__(self, data):
        self.data = data

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        return Response(self.data)


class ActiveClient:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "requests"
        return Query(self.rows)


def test_supabase_adapter_returns_all_active_drafts() -> None:
    now = datetime.now(UTC)
    rows = [
        RequestRead(
            id=uuid4(),
            user_id=USER_ID,
            data={},
            status=RequestStatus.DRAFT,
            created_at=now,
            updated_at=now,
        ).model_dump(mode="json")
        for _ in range(2)
    ]
    repository = SupabaseIntakePersistenceRepository(  # type: ignore[arg-type]
        ActiveClient(rows)
    )
    assert len(repository.find_active_requests(USER_ID)) == 2


class RaceQuery:
    def __init__(self, client, operation="select"):
        self.client = client
        self.operation = operation

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def insert(self, _payload):
        self.operation = "insert"
        return self

    def execute(self):
        if self.operation == "insert":
            raise RuntimeError("23505 duplicate key value violates unique constraint")
        self.client.select_count += 1
        return Response([] if self.client.select_count == 1 else [self.client.row])


class FindOrCreateRaceClient:
    def __init__(self, row):
        self.row = row
        self.select_count = 0

    def table(self, name):
        assert name == "requests"
        return RaceQuery(self)


def test_find_or_create_recovers_from_concurrent_unique_violation() -> None:
    now = datetime.now(UTC)
    existing = RequestRead(
        id=uuid4(),
        user_id=USER_ID,
        data={},
        status=RequestStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    repository = SupabaseIntakePersistenceRepository(  # type: ignore[arg-type]
        FindOrCreateRaceClient(existing.model_dump(mode="json"))
    )
    active, created = repository.get_or_create_active_request(USER_ID)
    assert created is False
    assert [item.id for item in active] == [existing.id]


class BrokenRequestClient:
    def table(self, name):
        assert name == "requests"
        return RpcCall(None, "network timeout")


def test_request_rest_failure_uses_persistence_error() -> None:
    repository = SupabaseIntakePersistenceRepository(  # type: ignore[arg-type]
        BrokenRequestClient()
    )
    with pytest.raises(IntakePersistenceRepositoryError):
        repository.create_request(RequestCreate(user_id=USER_ID))
    with pytest.raises(IntakePersistenceRepositoryError):
        repository.get_request(uuid4())
