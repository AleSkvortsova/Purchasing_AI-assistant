from typing import Protocol
from uuid import UUID

from app.schemas.request import RequestCreate, RequestRead, RequestUpdate


class RequestRepository(Protocol):
    def create_request(self, request: RequestCreate) -> RequestRead: ...

    def get_request(self, request_id: UUID) -> RequestRead | None: ...

    def update_request(
        self,
        request_id: UUID,
        update: RequestUpdate,
    ) -> RequestRead | None: ...

    def get_active_draft_for_user(self, user_id: UUID) -> RequestRead | None: ...

    def health_check(self) -> None: ...
