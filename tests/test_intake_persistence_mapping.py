from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.intake.models import (
    FieldConflict,
    FieldValueState,
    IntakeFieldUpdate,
    ProcurementType,
    RequestDraftData,
    UpdateSource,
)
from app.intake.service import RequestIntakeService
from app.intake_persistence.exceptions import (
    IntakePersistenceMappingError,
    UnsupportedIntakeSchemaVersionError,
)
from app.intake_persistence.mappers import (
    INTAKE_SCHEMA_VERSION,
    IntakePersistenceMapper,
)
from app.schemas.common import RequestStatus, RequestType
from app.schemas.request import RequestRead

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def request_record(**changes) -> RequestRead:
    values = {
        "id": uuid4(),
        "user_id": USER_ID,
        "request_type": None,
        "category_code": None,
        "title": None,
        "data": {},
        "request_number": None,
        "status": RequestStatus.DRAFT,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "confirmed_at": None,
        "version": 1,
    }
    values.update(changes)
    return RequestRead.model_validate(values)


@pytest.mark.parametrize(
    ("intake_type", "persistence_type"),
    [
        (ProcurementType.GOODS, RequestType.PRODUCT),
        (ProcurementType.SERVICE, RequestType.SERVICE),
        (ProcurementType.WORK, RequestType.SERVICE),
    ],
)
def test_procurement_type_round_trip(intake_type, persistence_type) -> None:
    mapper = IntakePersistenceMapper()
    draft = RequestDraftData(
        request_id=uuid4(),
        requester_id=USER_ID,
        procurement_type=intake_type,
        item_name="Предмет",
    )
    result = RequestIntakeService().process_step(draft, IntakeFieldUpdate())
    patch = mapper.draft_to_request_update(draft, result)
    assert patch.request_type == persistence_type
    restored = mapper.request_to_draft(
        request_record(
            id=draft.request_id,
            request_type=patch.request_type,
            data=patch.data,
        )
    )
    assert restored.procurement_type == intake_type


def test_mapper_preserves_decimal_date_uuid_state_and_conflict() -> None:
    mapper = IntakePersistenceMapper()
    request_id = uuid4()
    draft = RequestDraftData(
        request_id=request_id,
        requester_id=USER_ID,
        procurement_type="goods",
        amount=Decimal("180000.01"),
        desired_delivery_date=date(2030, 1, 2),
        field_states={
            "amount": FieldValueState(
                field_code="amount",
                value=Decimal("180000.01"),
                source=UpdateSource.USER,
                confirmed=True,
            )
        },
        conflicts=[
            FieldConflict(
                id="c1",
                field_code="amount",
                current_value="180000.01",
                proposed_value="200000",
                message="Подтвердите сумму",
            )
        ],
    )
    result = RequestIntakeService().process_step(draft, IntakeFieldUpdate())
    patch = mapper.draft_to_request_update(draft, result)
    restored = mapper.request_to_draft(
        request_record(
            id=request_id,
            request_type=RequestType.PRODUCT,
            data=patch.data,
        )
    )
    assert restored.amount == Decimal("180000.01")
    assert restored.desired_delivery_date == date(2030, 1, 2)
    assert restored.request_id == request_id
    assert restored.field_states["amount"].confirmed is True
    assert restored.conflicts[0].id == "c1"


def test_title_display_fallback_is_not_persisted() -> None:
    draft = RequestDraftData(item_name="Монитор", title=None)
    result = RequestIntakeService().process_step(draft, IntakeFieldUpdate())
    patch = IntakePersistenceMapper().draft_to_request_update(draft, result)
    assert patch.title is None
    assert "title" not in patch.data["intake"]["draft"]


def test_intake_patch_preserves_unrelated_request_data() -> None:
    draft = RequestDraftData(item_name="Монитор")
    result = RequestIntakeService().process_step(draft, IntakeFieldUpdate())
    patch = IntakePersistenceMapper().draft_to_request_update(
        draft,
        result,
        existing_data={
            "legacy_integration": {"external_id": "ERP-42"},
            "unrelated_flag": True,
        },
    )
    assert patch.data["legacy_integration"] == {"external_id": "ERP-42"}
    assert patch.data["unrelated_flag"] is True


def test_legacy_empty_and_legacy_data_are_supported() -> None:
    mapper = IntakePersistenceMapper()
    empty = mapper.request_to_draft(request_record())
    assert empty.procurement_type is None
    legacy = mapper.request_to_draft(
        request_record(
            request_type=RequestType.PRODUCT,
            category_code="G03",
            data={"quantity": "10", "unit": "шт."},
        )
    )
    assert legacy.procurement_type == ProcurementType.GOODS
    assert legacy.quantity == Decimal("10")


def test_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(UnsupportedIntakeSchemaVersionError):
        IntakePersistenceMapper().request_to_draft(
            request_record(
                data={
                    "schema_version": INTAKE_SCHEMA_VERSION + 1,
                    "intake": {"draft": {}},
                }
            )
        )


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(IntakePersistenceMappingError):
        IntakePersistenceMapper.persistence_type_to_intake(
            RequestType.PRODUCT, "unknown"
        )
