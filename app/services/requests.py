from copy import deepcopy
from uuid import UUID

from app.core.exceptions import (
    DraftUpdateForbiddenError,
    RequestNotFoundError,
)
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

        effective_update = update
        if "data" in update.model_fields_set and update.data is not None:
            merged_data = deepcopy(current.data)
            merged_data.update(update.data)
            effective_update = update.model_copy(update={"data": merged_data})

        updated = self._repository.update_request(request_id, effective_update)
        if updated is None:
            raise RequestNotFoundError(f"Request {request_id} was not found")
        return updated
