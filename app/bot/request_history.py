from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.intake.card import RequestCardBuilder
from app.intake.models import RequestCard, RequestDraftData
from app.intake_persistence.mappers import IntakePersistenceMapper
from app.intake_persistence.repositories import InMemoryIntakeStorage
from app.rules.models import ApprovalRouteResult
from app.schemas.common import RequestStatus
from app.schemas.request import RequestRead
from supabase import Client


class RequestHistoryError(RuntimeError):
    pass


class RequestHistoryRepository(Protocol):
    def list_for_user(self, user_id: UUID, limit: int) -> list[RequestRead]: ...

    def get_owned(self, request_id: UUID, user_id: UUID) -> RequestRead | None: ...


class RequestHistoryItem(BaseModel):
    request_id: UUID
    request_number: str | None
    request_type: str | None
    item_name: str
    status: RequestStatus
    displayed_at: datetime


class RequestHistoryView(BaseModel):
    request: RequestRead
    draft: RequestDraftData
    card: RequestCard


class InMemoryRequestHistoryRepository:
    def __init__(self, storage: InMemoryIntakeStorage) -> None:
        self.storage = storage

    def list_for_user(self, user_id: UUID, limit: int) -> list[RequestRead]:
        with self.storage.lock:
            matches = [
                item.model_copy(deep=True)
                for item in self.storage.requests.values()
                if item.user_id == user_id and item.status != RequestStatus.DRAFT
            ]
        matches.sort(key=_sort_key, reverse=True)
        return matches[:limit]

    def get_owned(self, request_id: UUID, user_id: UUID) -> RequestRead | None:
        with self.storage.lock:
            request = self.storage.requests.get(request_id)
            if (
                request is None
                or request.user_id != user_id
                or request.status == RequestStatus.DRAFT
            ):
                return None
            return request.model_copy(deep=True)


class SupabaseRequestHistoryRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def list_for_user(self, user_id: UUID, limit: int) -> list[RequestRead]:
        try:
            response = (
                self._client.table("requests")
                .select("*")
                .eq("user_id", str(user_id))
                .neq("status", RequestStatus.DRAFT.value)
                .order("registered_at", desc=True, nullsfirst=False)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            requests = [RequestRead.model_validate(row) for row in response.data]
            return sorted(requests, key=_sort_key, reverse=True)[:limit]
        except Exception as exc:
            raise RequestHistoryError("Не удалось получить список заявок") from exc

    def get_owned(self, request_id: UUID, user_id: UUID) -> RequestRead | None:
        try:
            response = (
                self._client.table("requests")
                .select("*")
                .eq("id", str(request_id))
                .eq("user_id", str(user_id))
                .neq("status", RequestStatus.DRAFT.value)
                .limit(1)
                .execute()
            )
            return (
                RequestRead.model_validate(response.data[0]) if response.data else None
            )
        except Exception as exc:
            raise RequestHistoryError("Не удалось получить заявку") from exc


class RequestHistoryService:
    def __init__(
        self,
        repository: RequestHistoryRepository,
        *,
        mapper: IntakePersistenceMapper | None = None,
        card_builder: RequestCardBuilder | None = None,
    ) -> None:
        self._repository = repository
        self._mapper = mapper or IntakePersistenceMapper()
        self._card_builder = card_builder or RequestCardBuilder()

    def list_recent(self, user_id: UUID, limit: int = 5) -> list[RequestHistoryItem]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        requests = self._repository.list_for_user(user_id, limit)
        return [self._summary(item) for item in requests]

    def get(self, request_id: UUID, user_id: UUID) -> RequestHistoryView | None:
        request = self._repository.get_owned(request_id, user_id)
        if request is None:
            return None
        draft = self._mapper.request_to_draft(request)
        lifecycle = request.data.get("lifecycle", {})
        card_payload = lifecycle.get("final_request_card")
        if card_payload:
            card = RequestCard.model_validate(card_payload)
        else:
            route_payload = lifecycle.get("final_approval_route")
            route = (
                ApprovalRouteResult.model_validate(route_payload)
                if route_payload
                else None
            )
            card = self._card_builder.build(draft, route)
        return RequestHistoryView(request=request, draft=draft, card=card)

    def _summary(self, request: RequestRead) -> RequestHistoryItem:
        draft = self._mapper.request_to_draft(request)
        return RequestHistoryItem(
            request_id=request.id,
            request_number=request.request_number,
            request_type=(
                draft.procurement_type.value if draft.procurement_type else None
            ),
            item_name=draft.item_name or request.title or "Заявка на закупку",
            status=request.status,
            displayed_at=request.registered_at or request.created_at,
        )


def _sort_key(request: RequestRead) -> datetime:
    return request.registered_at or request.created_at
