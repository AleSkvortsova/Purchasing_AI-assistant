from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from app.intake.field_registry import CATEGORY_NAMES
from app.intake.models import (
    IntakeStepResult,
    ProcurementType,
    RequestDraftData,
)
from app.intake_persistence.exceptions import (
    IntakePersistenceMappingError,
    UnsupportedIntakeSchemaVersionError,
)
from app.intake_persistence.models import PersistentDialogState
from app.schemas.common import RequestType
from app.schemas.request import RequestRead, RequestUpdate

INTAKE_SCHEMA_VERSION = 1
_DRAFT_META_FIELDS = {"field_states", "conflicts", "warnings"}


class IntakePersistenceMapper:
    def request_to_draft(self, request: RequestRead) -> RequestDraftData:
        data = request.data or {}
        if not isinstance(data, dict):
            raise IntakePersistenceMappingError("requests.data must be an object")
        intake = data.get("intake")
        version = data.get("schema_version")
        if intake is None:
            if version not in {None, INTAKE_SCHEMA_VERSION}:
                raise UnsupportedIntakeSchemaVersionError(
                    f"Unsupported intake schema version: {version}"
                )
            return self._legacy_draft(request, data)
        if version != INTAKE_SCHEMA_VERSION:
            raise UnsupportedIntakeSchemaVersionError(
                f"Unsupported intake schema version: {version}"
            )
        if not isinstance(intake, dict) or not isinstance(intake.get("draft"), dict):
            raise IntakePersistenceMappingError("Invalid persisted intake draft")
        payload = dict(intake["draft"])
        for field in _DRAFT_META_FIELDS:
            if field in intake:
                payload[field] = intake[field]
        payload.update(
            {
                "request_id": request.id,
                "requester_id": request.user_id,
                "title": request.title,
                "category_code": request.category_code,
            }
        )
        payload["procurement_type"] = self.persistence_type_to_intake(
            request.request_type,
            payload.get("procurement_type"),
        )
        try:
            return RequestDraftData.model_validate(payload)
        except ValidationError as exc:
            raise IntakePersistenceMappingError(
                "Persisted intake draft is invalid"
            ) from exc

    def draft_to_request_update(
        self,
        draft: RequestDraftData,
        result: IntakeStepResult,
        *,
        existing_data: dict[str, Any] | None = None,
        audit_metadata: dict[str, Any] | None = None,
    ) -> RequestUpdate:
        draft_payload = draft.model_dump(
            mode="json",
            exclude={
                "request_id",
                "requester_id",
                "title",
                "category_code",
                "field_states",
                "conflicts",
                "warnings",
            },
        )
        data = deepcopy(existing_data or {})
        data.update(
            {
                "schema_version": INTAKE_SCHEMA_VERSION,
                "intake": {
                    "draft": draft_payload,
                    "field_states": {
                        code: state.model_dump(mode="json")
                        for code, state in draft.field_states.items()
                    },
                    "conflicts": [
                        item.model_dump(mode="json") for item in draft.conflicts
                    ],
                    "warnings": list(draft.warnings),
                    "intake_status": result.status.value,
                    "next_question": (
                        result.next_question.model_dump(mode="json")
                        if result.next_question
                        else None
                    ),
                    "audit": dict(audit_metadata or {}),
                },
            }
        )
        return RequestUpdate(
            request_type=self.intake_type_to_persistence(draft.procurement_type),
            category_code=draft.category_code,
            title=draft.title,
            data=data,
        )

    def result_to_dialog_state(
        self,
        user_id,
        request_id,
        result: IntakeStepResult,
        state_version: int,
    ) -> PersistentDialogState:
        question = result.next_question
        return PersistentDialogState(
            user_id=user_id,
            request_id=request_id,
            intake_status=result.status,
            awaiting_field_code=question.field_code if question else None,
            next_question=question,
            related_conflict_id=(question.related_conflict_id if question else None),
            state_version=state_version,
            metadata={"request_status": "draft"},
        )

    @staticmethod
    def intake_type_to_persistence(
        value: ProcurementType | None,
    ) -> RequestType | None:
        if value is None:
            return None
        if value == ProcurementType.GOODS:
            return RequestType.PRODUCT
        if value in {ProcurementType.SERVICE, ProcurementType.WORK}:
            return RequestType.SERVICE
        raise IntakePersistenceMappingError(f"Unknown intake type: {value}")

    @staticmethod
    def persistence_type_to_intake(
        value: RequestType | None,
        persisted_intake_type: str | None = None,
    ) -> ProcurementType | None:
        if persisted_intake_type is not None:
            try:
                return ProcurementType(persisted_intake_type)
            except ValueError as exc:
                raise IntakePersistenceMappingError(
                    f"Unknown persisted intake type: {persisted_intake_type}"
                ) from exc
        if value is None:
            return None
        if value == RequestType.PRODUCT:
            return ProcurementType.GOODS
        if value == RequestType.SERVICE:
            return ProcurementType.SERVICE
        raise IntakePersistenceMappingError(f"Unknown persistence type: {value}")

    def _legacy_draft(
        self, request: RequestRead, data: dict[str, Any]
    ) -> RequestDraftData:
        allowed = set(RequestDraftData.model_fields) - _DRAFT_META_FIELDS
        payload = {key: value for key, value in data.items() if key in allowed}
        payload.update(
            {
                "request_id": request.id,
                "requester_id": request.user_id,
                "procurement_type": self.persistence_type_to_intake(
                    request.request_type
                ),
                "category_code": request.category_code,
                "title": request.title,
            }
        )
        try:
            return RequestDraftData.model_validate(payload)
        except ValidationError as exc:
            raise IntakePersistenceMappingError(
                "Legacy request data is invalid"
            ) from exc


def category_title(category_code: str | None) -> str | None:
    return CATEGORY_NAMES.get(category_code or "")
