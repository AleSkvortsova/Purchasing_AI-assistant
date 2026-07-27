from copy import deepcopy
from uuid import UUID

from app.core.exceptions import (
    DraftUpdateForbiddenError,
    RequestNotFoundError,
)
from app.intake.models import RequestDraftData
from app.repositories.request import RequestRepository
from app.schemas.common import RequestStatus
from app.schemas.request import RequestCreate, RequestRead, RequestUpdate


class RequestService:
    def __init__(self, repository: RequestRepository) -> None:
        self._repository = repository

    def create_draft(self, request: RequestCreate) -> RequestRead:
        return self._repository.create_request(request)

    def get_request(self, request_id: UUID) -> RequestRead:
        request = self._repository.get_request(request_id)
        if request is None:
            raise RequestNotFoundError(f"Request {request_id} was not found")
        return request

    def update_draft(
        self,
        request_id: UUID,
        update: RequestUpdate,
    ) -> RequestRead:
        current = self.get_request(request_id)
        if current.status != RequestStatus.DRAFT:
            raise DraftUpdateForbiddenError("Only draft requests can be updated")
        _reject_intake_projection_update(current, update)

        effective_update = update
        if "data" in update.model_fields_set and update.data is not None:
            merged_data = deepcopy(current.data)
            merged_data.update(update.data)
            effective_update = update.model_copy(update={"data": merged_data})

        updated = self._repository.update_request(request_id, effective_update)
        if updated is None:
            raise RequestNotFoundError(f"Request {request_id} was not found")
        return updated


_INTAKE_MANAGED_DATA_KEYS = (
    set(RequestDraftData.model_fields)
    - {"request_id", "requester_id", "field_states", "conflicts", "warnings"}
    | {"required_date", "request_type"}
)
_INTAKE_MANAGED_COLUMNS = {"request_type", "category_code", "title"}


def _reject_intake_projection_update(
    current: RequestRead,
    update: RequestUpdate,
) -> None:
    if not isinstance(current.data.get("intake"), dict):
        return
    changed_columns = update.model_fields_set & _INTAKE_MANAGED_COLUMNS
    changed_data = (
        set(update.data) & _INTAKE_MANAGED_DATA_KEYS
        if "data" in update.model_fields_set and update.data is not None
        else set()
    )
    if changed_columns or changed_data:
        raise DraftUpdateForbiddenError(
            "Intake-managed fields must be updated through an intake step"
        )
