from typing import Any
from uuid import UUID

from supabase import Client, create_client

from app.core.exceptions import RepositoryError
from app.schemas.request import RequestCreate, RequestRead, RequestUpdate


class SupabaseRequestRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    def from_credentials(
        cls,
        url: str,
        service_role_key: str,
    ) -> "SupabaseRequestRepository":
        return cls(create_client(url, service_role_key))

    def create_request(self, request: RequestCreate) -> RequestRead:
        payload = request.model_dump(mode="json")
        try:
            response = self._client.table("requests").insert(payload).execute()
            return self._parse_single(response.data)
        except Exception as exc:
            raise RepositoryError("Failed to create request") from exc

    def get_request(self, request_id: UUID) -> RequestRead | None:
        try:
            response = (
                self._client.table("requests")
                .select("*")
                .eq("id", str(request_id))
                .limit(1)
                .execute()
            )
            return self._parse_optional(response.data)
        except Exception as exc:
            raise RepositoryError("Failed to read request") from exc

    def update_request(
        self,
        request_id: UUID,
        update: RequestUpdate,
    ) -> RequestRead | None:
        payload = update.model_dump(mode="json", exclude_unset=True)
        try:
            response = (
                self._client.table("requests")
                .update(payload)
                .eq("id", str(request_id))
                .execute()
            )
            return self._parse_optional(response.data)
        except Exception as exc:
            raise RepositoryError("Failed to update request") from exc

    def get_active_draft_for_user(self, user_id: UUID) -> RequestRead | None:
        try:
            response = (
                self._client.table("requests")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("status", "draft")
                .order("updated_at", desc=True)
                .limit(1)
                .execute()
            )
            return self._parse_optional(response.data)
        except Exception as exc:
            raise RepositoryError("Failed to read active draft") from exc

    def health_check(self) -> None:
        try:
            self._client.table("requests").select("id").limit(1).execute()
        except Exception as exc:
            raise RepositoryError("Database connection check failed") from exc

    @staticmethod
    def _parse_single(data: list[dict[str, Any]]) -> RequestRead:
        request = SupabaseRequestRepository._parse_optional(data)
        if request is None:
            raise RepositoryError("Supabase returned no request data")
        return request

    @staticmethod
    def _parse_optional(data: list[dict[str, Any]]) -> RequestRead | None:
        if not data:
            return None
        return RequestRead.model_validate(data[0])
