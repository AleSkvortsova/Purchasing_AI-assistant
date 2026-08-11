import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import UUID

import pytest

from app.intake.models import IntakeFieldUpdate, IntakeStatus
from app.intake.service import RequestIntakeService
from app.intake_persistence.exceptions import ActiveDraftNotFoundError
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.request_lifecycle.exceptions import (
    LifecycleConcurrentUpdateError,
    LifecycleIdempotencyConflictError,
    LifecycleOwnershipError,
    LifecyclePersistenceError,
    RequestAlreadyRegisteredError,
    RequestNotReadyError,
)
from app.request_lifecycle.repositories import InMemoryRequestLifecycleRepository
from app.request_lifecycle.service import RequestLifecycleService
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from app.schemas.common import RequestStatus
from scripts.validate_approval_rules import load_rule_seed

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER = UUID("22222222-2222-4222-8222-222222222222")


def intake_service() -> RequestIntakeService:
    _, base, additional = load_rule_seed()
    return RequestIntakeService(
        ApprovalRuleService(InMemoryApprovalRuleRepository(base, additional))
    )


def full_update(amount="180000") -> IntakeFieldUpdate:
    return IntakeFieldUpdate(
        values={
            "procurement_type": "goods",
            "category_code": "G03",
            "item_name": "Монитор",
            "quantity": "10",
            "unit": "шт.",
            "specifications": "27 дюймов",
            "analogs_allowed": True,
            "amount": amount,
            "budget_status": "budgeted",
            "desired_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
            "delivery_location": "Офис",
            "business_justification": "Оснащение рабочих мест",
            "department": "ИТ",
            "contact_person": "Анна Петрова",
        }
    )


def ready_fixture():
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    saved = intake.process_structured_step(
        USER_ID, full_update(), idempotency_key="intake-ready"
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    return storage, intake, lifecycle, saved


def test_confirmation_view_recalculates_and_does_not_persist_title_fallback() -> None:
    storage, _, lifecycle, saved = ready_fixture()
    request = storage.requests[saved.request_id]
    request.data["stale_card"] = {"title": "Устаревшая карточка"}
    view = lifecycle.get_confirmation_view(saved.request_id, USER_ID)
    assert view.confirmable is True
    assert view.request_card is not None
    assert view.request_card.title == "Монитор"
    assert view.approval_route is not None
    assert view.approval_route.status == "resolved"
    assert storage.requests[saved.request_id].title is None


def test_confirmation_view_incomplete_conflict_and_ownership() -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    saved = intake.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"amount": "100"})
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    view = lifecycle.get_confirmation_view(saved.request_id, USER_ID)
    assert view.confirmable is False
    assert view.blocking_reasons
    with pytest.raises(LifecycleOwnershipError):
        lifecycle.get_confirmation_view(saved.request_id, OTHER_USER)
    intake.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"amount": "200"}),
        request_id=saved.request_id,
    )
    conflict = lifecycle.get_confirmation_view(saved.request_id, USER_ID)
    assert conflict.intake_status == IntakeStatus.CONFLICT
    assert conflict.confirmable is False


def test_unknown_budget_completes_intake_but_blocks_confirm() -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    update = full_update()
    update.values["budget_status"] = "unknown"
    saved = intake.process_structured_step(
        USER_ID,
        update,
        idempotency_key="unknown-budget-intake",
    )
    assert saved.intake_result.status == IntakeStatus.READY_FOR_CONFIRMATION

    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    view = lifecycle.get_confirmation_view(saved.request_id, USER_ID)
    assert view.confirmable is False
    assert view.approval_route is not None
    assert view.approval_route.status == "needs_clarification"
    assert "Маршрут согласования не разрешён" in view.blocking_reasons
    with pytest.raises(RequestNotReadyError):
        lifecycle.confirm_request(
            saved.request_id,
            USER_ID,
            saved.request_version,
            "confirm-unknown-budget",
        )


@pytest.mark.parametrize(
    ("procurement_type", "item_name", "category_code"),
    [
        ("goods", "потолочные светильники", "G01"),
        ("goods", "офисный стол", "G01"),
        ("goods", "ноутбук", "G02"),
        ("service", "перевозка груза", "S01"),
    ],
)
def test_semantically_incompatible_category_cannot_be_confirmed(
    procurement_type: str,
    item_name: str,
    category_code: str,
) -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    update = full_update()
    update.values.update(
        {
            "procurement_type": procurement_type,
            "item_name": item_name,
            "category_code": category_code,
            "description": f"Закупка: {item_name}",
        }
    )
    saved = intake.process_structured_step(USER_ID, update)
    # Simulate a legacy/manually assembled persisted draft that carries the
    # category value but not the field-level provenance used by the intake UI.
    storage.requests[saved.request_id].data["intake"]["field_states"].pop(
        "category_code", None
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )

    assert saved.intake_result.status != IntakeStatus.READY_FOR_CONFIRMATION
    assert "category_code" in saved.intake_result.completeness.invalid_fields
    view = lifecycle.get_confirmation_view(saved.request_id, USER_ID)
    assert view.confirmable is False
    with pytest.raises(RequestNotReadyError):
        lifecycle.confirm_request(
            saved.request_id,
            USER_ID,
            saved.request_version,
            f"confirm-semantic-mismatch-{procurement_type}-{category_code}",
        )


@pytest.mark.parametrize(
    ("procurement_type", "item_name", "category_code"),
    [
        ("goods", "промышленный вентилятор", "G02"),
        ("goods", "промышленный насос", "G03"),
        ("goods", "упаковочная машина", "G01"),
        ("service", "техническая диагностика неизвестного оборудования", "S14"),
    ],
)
def test_unknown_subject_category_cannot_be_confirmed_without_positive_support(
    procurement_type: str,
    item_name: str,
    category_code: str,
) -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    update = full_update()
    update.values.update(
        {
            "procurement_type": procurement_type,
            "item_name": item_name,
            "category_code": category_code,
            "description": f"Потребность: {item_name}",
        }
    )
    if procurement_type == "service":
        update.values.pop("quantity", None)
        update.values.pop("unit", None)
    saved = intake.process_structured_step(USER_ID, update)
    # Confirmation must recalculate semantic support even for a persisted
    # draft assembled without intake field provenance.
    storage.requests[saved.request_id].data["intake"]["field_states"].pop(
        "category_code", None
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )

    assert saved.intake_result.status != IntakeStatus.READY_FOR_CONFIRMATION
    assert "category_code" in saved.intake_result.completeness.invalid_fields
    view = lifecycle.get_confirmation_view(saved.request_id, USER_ID)
    assert view.confirmable is False
    with pytest.raises(RequestNotReadyError):
        lifecycle.confirm_request(
            saved.request_id,
            USER_ID,
            saved.request_version,
            f"confirm-unsupported-category-{procurement_type}-{category_code}",
        )


def test_confirm_registers_snapshot_dialog_logs_and_replay() -> None:
    storage, intake, lifecycle, saved = ready_fixture()
    storage.requests[saved.request_id].data["unrelated"] = {"preserved": True}
    result = lifecycle.confirm_request(
        saved.request_id, USER_ID, saved.request_version, "confirm-1"
    )
    assert result.status == RequestStatus.NEW
    assert result.intake_status == IntakeStatus.COMPLETED
    assert result.version == saved.request_version + 1
    assert result.request_number.startswith(f"PR-{date.today().year}-")
    request = storage.requests[saved.request_id]
    assert request.registered_at is not None
    assert request.confirmed_at is not None
    assert request.confirmed_by == USER_ID
    assert request.data["unrelated"] == {"preserved": True}
    snapshot = request.data["lifecycle"]
    assert snapshot["registered_schema_version"] == 1
    assert snapshot["final_request_card"]["title"] == "Монитор"
    assert snapshot["final_approval_route"]["status"] == "resolved"
    assert snapshot["final_completeness"]["is_complete"] is True
    json.dumps(snapshot, ensure_ascii=False)
    frozen_snapshot = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    terminal_view = lifecycle.get_lifecycle_state(saved.request_id, USER_ID)
    assert terminal_view.intake_status == IntakeStatus.COMPLETED
    assert terminal_view.request_card.title == "Монитор"
    assert terminal_view.confirmable is False
    dialog = storage.dialog_states[USER_ID]
    assert dialog["intake_status"] == "completed"
    lifecycle_logs = [
        log for log in storage.message_logs if log.metadata.get("lifecycle")
    ]
    assert [log.message_type for log in lifecycle_logs] == [
        "confirm_command",
        "request_registered",
    ]
    replay = lifecycle.confirm_request(saved.request_id, USER_ID, 1, "confirm-1")
    assert replay.replayed is True
    assert replay.version == result.version
    assert replay.request_number == result.request_number
    assert storage.lifecycle_sequence == 1
    assert (
        len([log for log in storage.message_logs if log.metadata.get("lifecycle")]) == 2
    )
    with pytest.raises(RequestAlreadyRegisteredError):
        lifecycle.confirm_request(
            saved.request_id, USER_ID, result.version, "confirm-2"
        )
    assert (
        json.dumps(
            storage.requests[saved.request_id].data["lifecycle"],
            ensure_ascii=False,
            sort_keys=True,
        )
        == frozen_snapshot
    )
    serialized_commands = json.dumps(
        [
            record.model_dump(mode="json")
            for record in storage.lifecycle_idempotency.values()
        ],
        ensure_ascii=False,
    )
    assert "authorization" not in serialized_commands.casefold()
    assert "api_key" not in serialized_commands.casefold()
    with pytest.raises(ActiveDraftNotFoundError):
        intake.get_active_session(USER_ID)
    new_draft = intake.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"item_name": "Ноутбук"})
    )
    assert new_draft.request_id != saved.request_id


def test_confirm_not_ready_and_stale_are_controlled() -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    saved = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    ).process_structured_step(USER_ID, IntakeFieldUpdate(values={"item_name": "Мышь"}))
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    with pytest.raises(RequestNotReadyError) as error:
        lifecycle.confirm_request(
            saved.request_id, USER_ID, saved.request_version, "not-ready"
        )
    assert error.value.confirmation_view.confirmable is False
    assert storage.requests[saved.request_id].status == RequestStatus.DRAFT
    assert storage.requests[saved.request_id].request_number is None
    assert storage.lifecycle_sequence == 0
    assert storage.message_logs[-1].message_type == "lifecycle_conflict"
    with pytest.raises(LifecycleConcurrentUpdateError):
        lifecycle.cancel_draft(saved.request_id, USER_ID, 1, "stale")
    assert storage.lifecycle_sequence == 0


def test_readiness_change_returns_dialog_to_collecting() -> None:
    storage, _, lifecycle, saved = ready_fixture()
    storage.requests[saved.request_id].data["intake"]["draft"][
        "business_justification"
    ] = None
    with pytest.raises(RequestNotReadyError):
        lifecycle.confirm_request(
            saved.request_id, USER_ID, saved.request_version, "registry-change"
        )
    assert storage.dialog_states[USER_ID]["intake_status"] == "collecting"
    assert storage.dialog_states[USER_ID]["awaiting_field_code"] == (
        "business_justification"
    )
    assert storage.requests[saved.request_id].status == RequestStatus.DRAFT


def test_return_to_editing_correction_and_final_snapshot() -> None:
    storage, intake, lifecycle, saved = ready_fixture()
    editing = lifecycle.return_to_editing(
        saved.request_id, USER_ID, saved.request_version, "edit-1"
    )
    assert editing.status == RequestStatus.DRAFT
    assert editing.intake_status == IntakeStatus.EDITING
    assert editing.request_number is None
    assert "amount" in editing.editable_field_codes
    active = intake.get_active_session(USER_ID)
    assert active.request_id == saved.request_id
    assert active.dialog_state.intake_status == IntakeStatus.EDITING
    corrected = intake.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"amount": "220000"}, explicit_correction=True),
        request_id=saved.request_id,
        idempotency_key="corrected",
    )
    assert corrected.intake_result.status == IntakeStatus.READY_FOR_CONFIRMATION
    assert corrected.request_id == saved.request_id
    assert corrected.created_new_request is False
    assert corrected.dialog_state.intake_status == IntakeStatus.READY_FOR_CONFIRMATION
    confirmed = lifecycle.confirm_request(
        saved.request_id, USER_ID, corrected.request_version, "confirm-edited"
    )
    amount_field = next(
        field
        for section in confirmed.request_card.sections
        for field in section.fields
        if field.code == "amount"
    )
    assert "220 000" in amount_field.display_value
    assert (
        storage.requests[saved.request_id].data["intake"]["draft"]["amount"] == "220000"
    )


def test_cancel_is_terminal_replayable_and_preserves_reason() -> None:
    storage = InMemoryIntakeStorage()
    intake = PersistentIntakeOrchestrator(InMemoryIntakePersistenceRepository(storage))
    saved = intake.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"item_name": "Ненужный товар"})
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), RequestIntakeService()
    )
    cancelled = lifecycle.cancel_draft(
        saved.request_id,
        USER_ID,
        saved.request_version,
        "cancel-1",
        "  Потребность   больше не актуальна ",
    )
    assert cancelled.status == RequestStatus.CANCELLED
    assert cancelled.request_number is None
    assert cancelled.cancellation_reason == "Потребность больше не актуальна"
    assert storage.requests[saved.request_id].cancelled_by == USER_ID
    assert (
        InMemoryIntakePersistenceRepository(storage).find_active_requests(USER_ID) == []
    )
    replay = lifecycle.cancel_draft(
        saved.request_id,
        USER_ID,
        1,
        "cancel-1",
        "Потребность больше не актуальна",
    )
    assert replay.replayed is True
    with pytest.raises(LifecycleIdempotencyConflictError):
        lifecycle.cancel_draft(
            saved.request_id, USER_ID, 1, "cancel-1", "Другая причина"
        )


@pytest.mark.parametrize(
    "stage",
    [
        "lifecycle_number",
        "lifecycle_request",
        "lifecycle_dialog",
        "lifecycle_logs",
        "lifecycle_idempotency",
    ],
)
def test_lifecycle_uow_rolls_back_every_critical_stage(stage) -> None:
    storage, _, lifecycle, saved = ready_fixture()
    logs_before = len(storage.message_logs)
    storage.fail_at = stage
    with pytest.raises(LifecyclePersistenceError):
        lifecycle.confirm_request(
            saved.request_id, USER_ID, saved.request_version, f"failure-{stage}"
        )
    request = storage.requests[saved.request_id]
    assert request.status == RequestStatus.DRAFT
    assert request.version == saved.request_version
    assert request.request_number is None
    assert len(storage.message_logs) == logs_before + 2
    assert storage.message_logs[-1].message_type == "lifecycle_error"
    assert storage.lifecycle_idempotency == {}


def test_concurrent_confirmations_get_one_registration_and_unique_numbers() -> None:
    storage, _, lifecycle, saved = ready_fixture()

    def confirm(key):
        try:
            return lifecycle.confirm_request(
                saved.request_id, USER_ID, saved.request_version, key
            )
        except RequestAlreadyRegisteredError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(confirm, ["race-a", "race-b"]))
    winners = [result for result in outcomes if result is not None]
    assert len(winners) == 1
    assert (
        storage.requests[saved.request_id].request_number == winners[0].request_number
    )
    assert storage.lifecycle_sequence == 1


def test_global_sequence_produces_unique_numbers_for_different_users() -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    first = intake.process_structured_step(USER_ID, full_update())
    second = intake.process_structured_step(OTHER_USER, full_update())
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    registered_a = lifecycle.confirm_request(
        first.request_id, USER_ID, first.request_version, "same-key"
    )
    registered_b = lifecycle.confirm_request(
        second.request_id, OTHER_USER, second.request_version, "same-key"
    )
    assert registered_a.request_number.endswith("000001")
    assert registered_b.request_number.endswith("000002")
    assert registered_a.request_number != registered_b.request_number


def test_concurrent_successful_confirmations_get_distinct_numbers() -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    first = intake.process_structured_step(USER_ID, full_update())
    second = intake.process_structured_step(OTHER_USER, full_update())
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda args: lifecycle.confirm_request(*args),
                [
                    (first.request_id, USER_ID, first.request_version, "parallel-a"),
                    (
                        second.request_id,
                        OTHER_USER,
                        second.request_version,
                        "parallel-b",
                    ),
                ],
            )
        )
    numbers = {result.request_number for result in outcomes}
    assert numbers == {
        f"PR-{date.today().year}-000001",
        f"PR-{date.today().year}-000002",
    }


def test_failure_after_number_allocation_leaves_safe_gap_and_allows_retry() -> None:
    storage, _, lifecycle, saved = ready_fixture()
    storage.fail_at = "lifecycle_number"
    with pytest.raises(LifecyclePersistenceError):
        lifecycle.confirm_request(
            saved.request_id, USER_ID, saved.request_version, "failed-number"
        )
    request = storage.requests[saved.request_id]
    assert request.status == RequestStatus.DRAFT
    assert request.request_number is None
    assert storage.dialog_states[USER_ID]["intake_status"] == "ready_for_confirmation"
    assert storage.lifecycle_sequence == 1

    storage.fail_at = None
    retried = lifecycle.confirm_request(
        saved.request_id, USER_ID, saved.request_version, "retry-number"
    )
    assert retried.request_number == f"PR-{date.today().year}-000002"


def test_command_namespaces_and_user_namespaces_are_independent() -> None:
    storage, _, lifecycle, saved = ready_fixture()
    lifecycle.return_to_editing(
        saved.request_id, USER_ID, saved.request_version, "shared"
    )
    cancelled = lifecycle.cancel_draft(
        saved.request_id, USER_ID, saved.request_version + 1, "shared"
    )
    assert cancelled.status == RequestStatus.CANCELLED
    assert len(storage.lifecycle_idempotency) == 2


def test_same_command_key_cannot_replay_result_for_another_request() -> None:
    storage, intake, lifecycle, saved = ready_fixture()
    lifecycle.confirm_request(
        saved.request_id, USER_ID, saved.request_version, "request-bound-key"
    )
    second = intake.process_structured_step(USER_ID, full_update())
    with pytest.raises(LifecycleIdempotencyConflictError):
        lifecycle.confirm_request(
            second.request_id,
            USER_ID,
            second.request_version,
            "request-bound-key",
        )


def test_failure_audit_failure_never_masks_original_error() -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    saved = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    ).process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"item_name": "Мышь"})
    )

    class BrokenFailureAuditRepository(InMemoryRequestLifecycleRepository):
        def append_lifecycle_failure(self, *args, **kwargs):
            raise LifecyclePersistenceError("failure audit unavailable")

    lifecycle = RequestLifecycleService(BrokenFailureAuditRepository(storage), core)
    with pytest.raises(RequestNotReadyError):
        lifecycle.confirm_request(
            saved.request_id, USER_ID, saved.request_version, "audit-fails"
        )
    assert storage.requests[saved.request_id].status == RequestStatus.DRAFT
    assert storage.requests[saved.request_id].request_number is None


def test_corrected_quantity_is_consistent_after_confirm_and_by_number() -> None:
    storage = InMemoryIntakeStorage()
    core = intake_service()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    initial = full_update()
    initial.values["quantity"] = "12"
    created = intake.process_structured_step(USER_ID, initial)
    corrected = intake.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"quantity": "10"}, explicit_correction=True),
        request_id=created.request_id,
    )
    assert storage.requests[created.request_id].data["quantity"] == "10"
    assert (
        storage.requests[created.request_id].data["intake"]["draft"]["quantity"]
        == "10"
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    registered = lifecycle.confirm_request(
        corrected.request_id,
        USER_ID,
        corrected.request_version,
        "confirm-corrected-quantity",
    )
    request = lifecycle.get_by_request_number(registered.request_number, USER_ID)

    assert request.data["quantity"] == "10"
    assert request.data["intake"]["draft"]["quantity"] == "10"
    card_quantity = next(
        field
        for section in request.data["lifecycle"]["final_request_card"]["sections"]
        for field in section["fields"]
        if field["code"] == "quantity"
    )
    assert card_quantity["display_value"] == "10"
    assert request.data["intake"]["intake_status"] == "completed"
    assert request.data["intake"]["next_question"] is None
    assert request.data["request_type"] == "product"
    assert request.data["procurement_type"] == "goods"
    assert request.data["category_code"] == "G03"
    assert request.request_type.value == request.data["request_type"]
    assert request.category_code == request.data["category_code"]
