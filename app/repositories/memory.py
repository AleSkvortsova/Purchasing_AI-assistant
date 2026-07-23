from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.schemas.common import RequestStatus
from app.schemas.request import RequestCreate, RequestRead, RequestUpdate


class InMemoryRequestRepository:
    def __init__(self, initial_requests: Iterable[RequestRead] = ()) -> None:
        self._requests = {
            request.id: request.model_copy(deep=True) for request in initial_requests
        }

    def create_request(self, request: RequestCreate) -> RequestRead:
        now = datetime.now(UTC)
        created = RequestRead(
            **request.model_dump(),
            id=uuid4(),
            request_number=None,
            status=RequestStatus.DRAFT,
            created_at=now,
            updated_at=now,
            confirmed_at=None,
        )
        self._requests[created.id] = created.model_copy(deep=True)
        return created

    def get_request(self, request_id: UUID) -> RequestRead | None:
        request = self._requests.get(request_id)
        return request.model_copy(deep=True) if request else None

    def update_request(
        self,
        request_id: UUID,
        update: RequestUpdate,
    ) -> RequestRead | None:
        current = self._requests.get(request_id)
        if current is None:
            return None

        values = update.model_dump(exclude_unset=True)
        if "data" in values and values["data"] is not None:
            values["data"] = deepcopy(values["data"])
        updated = current.model_copy(
            update={**values, "updated_at": datetime.now(UTC)},
            deep=True,
        )
        self._requests[request_id] = updated
        return updated.model_copy(deep=True)

    def get_active_draft_for_user(self, user_id: UUID) -> RequestRead | None:
        drafts = [
            request
            for request in self._requests.values()
            if request.user_id == user_id and request.status == RequestStatus.DRAFT
        ]
        if not drafts:
            return None
        latest = max(drafts, key=lambda item: item.updated_at)
        return latest.model_copy(deep=True)

    def health_check(self) -> None:
        return None
