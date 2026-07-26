import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.intake.models import IntakeFieldUpdate, IntakeStatus
from app.intake.service import RequestIntakeService
from app.intake_persistence.exceptions import (
    ActiveDraftNotFoundError,
    ConcurrentIntakeUpdateError,
    DialogStateCorruptedError,
    IdempotencyConflictError,
    IntakePersistenceRepositoryError,
    MultipleActiveDraftsError,
    PersistencePartialFailureError,
    RequestNotEditableError,
    RequestOwnershipError,
)
from app.intake_persistence.mappers import IntakePersistenceMapper
from app.intake_persistence.models import (
    MessageEnvelope,
    PersistenceMessageLog,
    SaveIntakeStepCommand,
)
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from app.schemas.common import RequestStatus
from app.schemas.request import RequestCreate, RequestRead
from scripts.validate_approval_rules import load_rule_seed

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER = UUID("22222222-2222-4222-8222-222222222222")


def repository(
    storage: InMemoryIntakeStorage | None = None,
    initial: list[RequestRead] | None = None,
) -> InMemoryIntakePersistenceRepository:
    return InMemoryIntakePersistenceRepository(storage, initial)


def request_record(user_id=USER_ID, status=RequestStatus.DRAFT) -> RequestRead:
    now = datetime.now(UTC)
    return RequestRead(
        id=uuid4(),
        user_id=user_id,
        data={},
        status=status,
        created_at=now,
        updated_at=now,
        version=1,
    )


def full_update() -> IntakeFieldUpdate:
    return IntakeFieldUpdate(
        values={
            "procurement_type": "goods",
            "category_code": "G03",
            "item_name": "Монитор",
            "quantity": "10",
            "unit": "шт.",
            "specifications": "27 дюймов",
            "analogs_allowed": True,
            "amount": "180000",
            "budget_status": "budgeted",
            "desired_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
            "delivery_location": "Офис",
            "business_justification": "Оснащение рабочих мест",
            "department": "ИТ",
            "contact_person": "Анна Петрова",
        }
    )


def intake_service_with_rules() -> RequestIntakeService:
    _, base, additional = load_rule_seed()
    return RequestIntakeService(
        ApprovalRuleService(InMemoryApprovalRuleRepository(base, additional))
    )


def test_first_step_creates_and_second_service_instance_resumes() -> None:
    storage = InMemoryIntakeStorage()
    first = PersistentIntakeOrchestrator(repository(storage)).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"procurement_type": "goods"}),
    )
    second = PersistentIntakeOrchestrator(repository(storage)).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "Монитор"}),
    )
    assert first.created_new_request is True
    assert second.created_new_request is False
    assert second.request_id == first.request_id
    assert second.request_version == first.request_version + 1
    assert second.intake_result.draft.procurement_type == "goods"


def test_concurrent_find_or_create_returns_one_draft() -> None:
    storage = InMemoryIntakeStorage()

    def resolve():
        return repository(storage).get_or_create_active_request(USER_ID)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resolve(), range(2)))
    request_ids = {result[0][0].id for result in results}
    assert len(request_ids) == 1
    assert sum(int(created) for _, created in results) == 1
    assert len(repository(storage).find_active_requests(USER_ID)) == 1


def test_request_id_ownership_editability_and_not_found() -> None:
    own = request_record()
    foreign = request_record(OTHER_USER)
    closed = request_record(status=RequestStatus.NEW)
    service = PersistentIntakeOrchestrator(repository(initial=[own, foreign, closed]))
    with pytest.raises(RequestOwnershipError):
        service.process_structured_step(USER_ID, IntakeFieldUpdate(), foreign.id)
    with pytest.raises(RequestNotEditableError):
        service.process_structured_step(USER_ID, IntakeFieldUpdate(), closed.id)
    with pytest.raises(ActiveDraftNotFoundError):
        service.process_structured_step(USER_ID, IntakeFieldUpdate(), uuid4())


def test_multiple_active_drafts_are_never_selected_silently() -> None:
    service = PersistentIntakeOrchestrator(
        repository(initial=[request_record(), request_record()])
    )
    with pytest.raises(MultipleActiveDraftsError) as error:
        service.process_structured_step(USER_ID, IntakeFieldUpdate())
    assert len(error.value.request_ids) == 2


def test_closed_requests_are_not_active() -> None:
    repo = repository(
        initial=[
            request_record(status=RequestStatus.NEW),
            request_record(status=RequestStatus.CANCELLED),
        ]
    )
    result = PersistentIntakeOrchestrator(repo).process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"item_name": "Новая заявка"})
    )
    assert result.created_new_request is True


def test_ready_for_confirmation_remains_active_and_restores_card() -> None:
    storage = InMemoryIntakeStorage()
    saved = PersistentIntakeOrchestrator(repository(storage)).process_structured_step(
        USER_ID, full_update(), idempotency_key="full"
    )
    restored = PersistentIntakeOrchestrator(repository(storage)).get_active_session(
        USER_ID
    )
    assert saved.intake_result.status == IntakeStatus.READY_FOR_CONFIRMATION
    assert restored.request_id == saved.request_id
    assert restored.intake_result.request_card is not None
    assert restored.dialog_state.awaiting_field_code is None


def test_approval_route_is_recalculated_not_persisted() -> None:
    storage = InMemoryIntakeStorage()
    saved = PersistentIntakeOrchestrator(
        repository(storage), intake_service_with_rules()
    ).process_structured_step(USER_ID, full_update())
    persisted = repository(storage).get_request(saved.request_id)
    assert persisted is not None
    assert "approval_route" not in persisted.data["intake"]
    restored = PersistentIntakeOrchestrator(
        repository(storage), intake_service_with_rules()
    ).get_active_session(USER_ID)
    assert restored.intake_result.approval_route is not None
    assert "Финансовый контролёр" in (
        restored.intake_result.approval_route.final_approvers
    )


def test_collecting_and_conflict_dialog_state() -> None:
    storage = InMemoryIntakeStorage()
    service = PersistentIntakeOrchestrator(repository(storage))
    collecting = service.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"amount": "100"})
    )
    assert collecting.dialog_state.awaiting_field_code == "procurement_type"
    conflict = service.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"amount": "200"})
    )
    assert conflict.intake_result.status == IntakeStatus.CONFLICT
    assert conflict.dialog_state.awaiting_field_code == "amount"
    assert conflict.dialog_state.related_conflict_id is not None


def test_corrupted_dialog_state_is_safe_error() -> None:
    storage = InMemoryIntakeStorage()
    service = PersistentIntakeOrchestrator(repository(storage))
    service.process_structured_step(USER_ID, IntakeFieldUpdate())
    storage.dialog_states[USER_ID] = {"unexpected": True}
    with pytest.raises(DialogStateCorruptedError):
        service.get_active_session(USER_ID)


def test_stale_dialog_version_requires_recovery() -> None:
    storage = InMemoryIntakeStorage()
    service = PersistentIntakeOrchestrator(repository(storage))
    saved = service.process_structured_step(USER_ID, IntakeFieldUpdate())
    storage.dialog_states[USER_ID]["state_version"] = saved.request_version - 1
    with pytest.raises(PersistencePartialFailureError, match="Версия dialog state"):
        service.get_active_session(USER_ID)


def test_idempotency_replay_conflict_and_user_namespace() -> None:
    storage = InMemoryIntakeStorage()
    service = PersistentIntakeOrchestrator(repository(storage))
    update = IntakeFieldUpdate(values={"item_name": "Монитор"})
    first = service.process_structured_step(
        USER_ID, update, idempotency_key="message-1"
    )
    logs_before = len(repository(storage).list_message_logs(USER_ID))
    replay = PersistentIntakeOrchestrator(repository(storage)).process_structured_step(
        USER_ID, update, idempotency_key="message-1"
    )
    assert replay.replayed is True
    assert replay.request_version == first.request_version
    assert len(repository(storage).list_message_logs(USER_ID)) == logs_before
    with pytest.raises(IdempotencyConflictError):
        service.process_structured_step(
            USER_ID,
            IntakeFieldUpdate(values={"item_name": "Ноутбук"}),
            idempotency_key="message-1",
        )
    other = service.process_structured_step(
        OTHER_USER, update, idempotency_key="message-1"
    )
    assert other.user_id == OTHER_USER


def test_idempotency_fingerprint_is_canonical_and_survives_restart() -> None:
    storage = InMemoryIntakeStorage()
    first = PersistentIntakeOrchestrator(repository(storage)).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "Монитор", "amount": "180000.0"}),
        idempotency_key="canonical",
    )
    replay = PersistentIntakeOrchestrator(repository(storage)).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"amount": 180000, "item_name": "Монитор"}),
        idempotency_key="canonical",
    )
    assert replay.replayed is True
    assert replay.request_version == first.request_version


def test_no_idempotency_key_is_reported_in_metadata() -> None:
    result = PersistentIntakeOrchestrator(repository()).process_structured_step(
        USER_ID, IntakeFieldUpdate()
    )
    assert result.metadata["idempotency_protected"] is False


def test_inmemory_save_is_atomic_on_each_failure_stage() -> None:
    for stage in ("request", "dialog", "incoming_log", "outgoing_log"):
        storage = InMemoryIntakeStorage(fail_at=stage)
        result = PersistentIntakeOrchestrator(
            repository(storage)
        ).process_structured_step(
            USER_ID,
            IntakeFieldUpdate(values={"item_name": "Монитор"}),
            idempotency_key="retry-key",
        )
        assert result.persistence_status == "partial_failure"
        saved_request = next(iter(storage.requests.values()))
        assert saved_request.version == 1
        assert storage.dialog_states == {}
        assert len(storage.message_logs) == 1
        assert storage.message_logs[0].message_type == "system_error"
        assert storage.message_logs[0].metadata["contains_secrets"] is False
        assert storage.idempotency == {}


def test_retry_after_created_request_partial_failure_is_safe() -> None:
    storage = InMemoryIntakeStorage(fail_at="dialog")
    service = PersistentIntakeOrchestrator(repository(storage))
    failed = service.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "Монитор"}),
        idempotency_key="retry",
    )
    storage.fail_at = None
    retried = service.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "Монитор"}),
        idempotency_key="retry",
    )
    assert failed.persistence_status == "partial_failure"
    assert retried.persistence_status == "saved"
    assert retried.request_version == 2
    assert len(storage.requests) == 1
    assert retried.request_id == failed.request_id


def test_system_error_audit_log_is_best_effort() -> None:
    storage = InMemoryIntakeStorage(fail_at="dialog")

    class UnavailableAuditRepository(InMemoryIntakePersistenceRepository):
        def append_message_log(self, log):
            raise IntakePersistenceRepositoryError("audit store unavailable")

    result = PersistentIntakeOrchestrator(
        UnavailableAuditRepository(storage)
    ).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "Монитор"}),
        idempotency_key="best-effort",
    )
    assert result.persistence_status == "partial_failure"
    assert result.metadata["recovery_required"] is True
    assert storage.message_logs == []


def test_idempotency_key_cannot_replay_another_explicit_request() -> None:
    storage = InMemoryIntakeStorage()
    service = PersistentIntakeOrchestrator(repository(storage))
    update = IntakeFieldUpdate(values={"item_name": "Монитор"})
    service.process_structured_step(USER_ID, update, idempotency_key="bound")
    another = request_record()
    storage.requests[another.id] = another
    with pytest.raises(IdempotencyConflictError, match="другой заявкой"):
        service.process_structured_step(
            USER_ID,
            update,
            request_id=another.id,
            idempotency_key="bound",
        )


def test_optimistic_lock_rejects_stale_command() -> None:
    repo = repository()
    initial = repo.create_request(RequestCreate(user_id=USER_ID))
    draft = IntakePersistenceMapper().request_to_draft(initial)
    result = RequestIntakeService().process_step(
        draft, IntakeFieldUpdate(values={"item_name": "Монитор"})
    )
    mapper = IntakePersistenceMapper()
    patch = mapper.draft_to_request_update(result.draft, result)
    dialog = mapper.result_to_dialog_state(USER_ID, initial.id, result, 2)
    incoming = PersistenceMessageLog(
        user_id=USER_ID,
        request_id=initial.id,
        direction="incoming",
        message_type="structured_update",
    )
    outgoing = PersistenceMessageLog(
        user_id=USER_ID,
        request_id=initial.id,
        direction="outgoing",
        message_type="question",
    )
    command = SaveIntakeStepCommand(
        request_id=initial.id,
        expected_version=1,
        request_type=None,
        request_data=patch.data or {},
        dialog_state=dialog,
        incoming_log=incoming,
        outgoing_log=outgoing,
    )
    repo.save_step(command)
    with pytest.raises(ConcurrentIntakeUpdateError):
        repo.save_step(command)
    assert repo.get_request(initial.id).version == 2  # type: ignore[union-attr]


def test_message_logs_are_safe_and_json_serializable() -> None:
    repo = repository()
    PersistentIntakeOrchestrator(repo).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"amount": "180000"}),
        incoming_message=MessageEnvelope(
            message_id="m1",
            metadata={"channel": "test", "Authorization": "Bearer secret"},
        ),
        idempotency_key="safe-log",
    )
    logs = repo.list_message_logs(USER_ID)
    assert [item.direction for item in logs] == ["incoming", "outgoing"]
    assert logs[0].message_type == "structured_update"
    assert logs[1].duration_ms is not None
    encoded = json.dumps([item.model_dump(mode="json") for item in logs])
    assert "Authorization" not in encoded
    assert "Bearer secret" not in encoded
    assert "API key" not in encoded
