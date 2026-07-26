from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError
from supabase import Client, create_client

from app.intake_persistence.exceptions import (
    ActiveDraftNotFoundError,
    ConcurrentIntakeUpdateError,
    DialogStateCorruptedError,
    IdempotencyConflictError,
    IntakePersistenceRepositoryError,
    RequestNotEditableError,
    RequestOwnershipError,
)
from app.intake_persistence.models import (
    IdempotencyRecord,
    PersistenceMessageLog,
    PersistentDialogState,
    SavedIntakeStep,
    SaveIntakeStepCommand,
)
from app.repositories.supabase import SupabaseRequestRepository
from app.schemas.common import RequestStatus
from app.schemas.request import RequestCreate, RequestRead


class IntakePersistenceRepository(Protocol):
    def find_active_requests(self, user_id: UUID) -> list[RequestRead]: ...

    def create_request(self, request: RequestCreate) -> RequestRead: ...

    def get_or_create_active_request(
        self, user_id: UUID
    ) -> tuple[list[RequestRead], bool]: ...

    def get_request(self, request_id: UUID) -> RequestRead | None: ...

    def get_dialog_state(self, user_id: UUID) -> PersistentDialogState | None: ...

    def find_idempotency(self, user_id: UUID, key: str) -> IdempotencyRecord | None: ...

    def save_step(self, command: SaveIntakeStepCommand) -> SavedIntakeStep: ...

    def list_message_logs(self, user_id: UUID) -> list[PersistenceMessageLog]: ...

    def append_message_log(self, log: PersistenceMessageLog) -> None: ...

    def clear_dialog_state(self, user_id: UUID) -> None: ...

    def health_check(self) -> None: ...


@dataclass
class InMemoryIntakeStorage:
    requests: dict[UUID, RequestRead] = field(default_factory=dict)
    dialog_states: dict[UUID, Any] = field(default_factory=dict)
    message_logs: list[PersistenceMessageLog] = field(default_factory=list)
    idempotency: dict[tuple[UUID, str], IdempotencyRecord] = field(default_factory=dict)
    fail_at: str | None = None
    lock: RLock = field(default_factory=RLock)


class InMemoryIntakePersistenceRepository:
    def __init__(
        self,
        storage: InMemoryIntakeStorage | None = None,
        initial_requests: list[RequestRead] | None = None,
    ) -> None:
        self.storage = storage or InMemoryIntakeStorage()
        for request in initial_requests or []:
            self.storage.requests[request.id] = request.model_copy(deep=True)

    def find_active_requests(self, user_id: UUID) -> list[RequestRead]:
        with self.storage.lock:
            return sorted(
                [
                    item.model_copy(deep=True)
                    for item in self.storage.requests.values()
                    if item.user_id == user_id and item.status == RequestStatus.DRAFT
                ],
                key=lambda item: (item.updated_at, str(item.id)),
                reverse=True,
            )

    def create_request(self, request: RequestCreate) -> RequestRead:
        with self.storage.lock:
            now = datetime.now(UTC)
            created = RequestRead(
                **request.model_dump(),
                id=uuid4(),
                status=RequestStatus.DRAFT,
                request_number=None,
                created_at=now,
                updated_at=now,
                confirmed_at=None,
                version=1,
            )
            self.storage.requests[created.id] = created.model_copy(deep=True)
            return created

    def get_or_create_active_request(
        self, user_id: UUID
    ) -> tuple[list[RequestRead], bool]:
        with self.storage.lock:
            active = [
                item.model_copy(deep=True)
                for item in self.storage.requests.values()
                if item.user_id == user_id and item.status == RequestStatus.DRAFT
            ]
            if active:
                return active, False
            return [self.create_request(RequestCreate(user_id=user_id, data={}))], True

    def get_request(self, request_id: UUID) -> RequestRead | None:
        with self.storage.lock:
            item = self.storage.requests.get(request_id)
            return item.model_copy(deep=True) if item else None

    def get_dialog_state(self, user_id: UUID) -> PersistentDialogState | None:
        with self.storage.lock:
            value = self.storage.dialog_states.get(user_id)
            if value is None:
                return None
            try:
                return PersistentDialogState.model_validate(deepcopy(value))
            except ValidationError as exc:
                raise DialogStateCorruptedError(
                    "Сохранённое состояние диалога повреждено"
                ) from exc

    def find_idempotency(self, user_id: UUID, key: str) -> IdempotencyRecord | None:
        with self.storage.lock:
            value = self.storage.idempotency.get((user_id, key))
            return value.model_copy(deep=True) if value else None

    def save_step(self, command: SaveIntakeStepCommand) -> SavedIntakeStep:
        with self.storage.lock:
            current = self.storage.requests.get(command.request_id)
            if current is None:
                raise IntakePersistenceRepositoryError("Request was not found")
            if current.version != command.expected_version:
                raise ConcurrentIntakeUpdateError(
                    "Черновик был изменён другим процессом; загрузите его заново"
                )
            new_version = current.version + 1
            updated = current.model_copy(
                update={
                    "request_type": command.request_type,
                    "category_code": command.category_code,
                    "title": command.title,
                    "data": deepcopy(command.request_data),
                    "version": new_version,
                    "updated_at": datetime.now(UTC),
                },
                deep=True,
            )
            dialog = command.dialog_state.model_copy(
                update={"state_version": new_version}, deep=True
            )
            self._maybe_fail("request")
            self._maybe_fail("dialog")
            self._maybe_fail("incoming_log")
            self._maybe_fail("outgoing_log")
            self.storage.requests[command.request_id] = updated
            self.storage.dialog_states[dialog.user_id] = dialog.model_dump(mode="json")
            self.storage.message_logs.extend(
                [
                    command.incoming_log.model_copy(deep=True),
                    command.outgoing_log.model_copy(deep=True),
                ]
            )
            if command.idempotency_record is not None:
                record = command.idempotency_record.model_copy(deep=True)
                record.result.request_version = new_version
                record.result.dialog_state = dialog
                self.storage.idempotency[(record.user_id, record.key)] = record
            return SavedIntakeStep(
                request_version=new_version,
                dialog_state=dialog,
            )

    def list_message_logs(self, user_id: UUID) -> list[PersistenceMessageLog]:
        with self.storage.lock:
            return [
                item.model_copy(deep=True)
                for item in self.storage.message_logs
                if item.user_id == user_id
            ]

    def append_message_log(self, log: PersistenceMessageLog) -> None:
        with self.storage.lock:
            self.storage.message_logs.append(log.model_copy(deep=True))

    def clear_dialog_state(self, user_id: UUID) -> None:
        with self.storage.lock:
            self.storage.dialog_states.pop(user_id, None)

    def health_check(self) -> None:
        return None

    def _maybe_fail(self, stage: str) -> None:
        if self.storage.fail_at == stage:
            raise IntakePersistenceRepositoryError(
                f"Simulated atomic save failure at {stage}"
            )


class SupabaseIntakePersistenceRepository:
    """Supabase adapter using migration 007 RPC for atomic step saves."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._requests = SupabaseRequestRepository(client)

    @classmethod
    def from_credentials(
        cls, url: str, service_role_key: str
    ) -> SupabaseIntakePersistenceRepository:
        return cls(create_client(url, service_role_key))

    def find_active_requests(self, user_id: UUID) -> list[RequestRead]:
        try:
            response = (
                self._client.table("requests")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("status", "draft")
                .order("updated_at", desc=True)
                .execute()
            )
            return [RequestRead.model_validate(row) for row in response.data]
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to list active intake drafts"
            ) from exc

    def create_request(self, request: RequestCreate) -> RequestRead:
        try:
            return self._requests.create_request(request)
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to create intake draft"
            ) from exc

    def get_or_create_active_request(
        self, user_id: UUID
    ) -> tuple[list[RequestRead], bool]:
        active = self.find_active_requests(user_id)
        if active:
            return active, False
        try:
            return [self.create_request(RequestCreate(user_id=user_id, data={}))], True
        except Exception:
            # Migration 007 unique index makes a concurrent creator the winner.
            active = self.find_active_requests(user_id)
            if active:
                return active, False
            raise

    def get_request(self, request_id: UUID) -> RequestRead | None:
        try:
            return self._requests.get_request(request_id)
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to read intake draft"
            ) from exc

    def get_dialog_state(self, user_id: UUID) -> PersistentDialogState | None:
        try:
            response = (
                self._client.table("dialog_states")
                .select("state_data")
                .eq("user_id", str(user_id))
                .limit(1)
                .execute()
            )
            if not response.data:
                return None
            return PersistentDialogState.model_validate(response.data[0]["state_data"])
        except ValidationError as exc:
            raise DialogStateCorruptedError(
                "Сохранённое состояние диалога повреждено"
            ) from exc
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to read intake dialog state"
            ) from exc

    def find_idempotency(self, user_id: UUID, key: str) -> IdempotencyRecord | None:
        try:
            response = (
                self._client.table("message_logs")
                .select("idempotency_fingerprint,idempotency_result")
                .eq("user_id", str(user_id))
                .eq("direction", "incoming")
                .eq("idempotency_key", key)
                .limit(1)
                .execute()
            )
            if not response.data:
                return None
            row = response.data[0]
            return IdempotencyRecord(
                user_id=user_id,
                key=key,
                fingerprint=row["idempotency_fingerprint"],
                result=row["idempotency_result"],
            )
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to read intake idempotency record"
            ) from exc

    def save_step(self, command: SaveIntakeStepCommand) -> SavedIntakeStep:
        payload = command.model_dump(mode="json")
        try:
            response = self._client.rpc("save_intake_step", payload).execute()
            data = (
                response.data[0] if isinstance(response.data, list) else response.data
            )
            return SavedIntakeStep(
                request_version=data["request_version"],
                dialog_state=data["dialog_state"],
                replayed=bool(data.get("replayed", False)),
            )
        except Exception as exc:
            message = str(exc).casefold()
            if "42501" in message or "ownership_mismatch" in message:
                raise RequestOwnershipError(
                    "Заявка принадлежит другому пользователю"
                ) from exc
            if "55000" in message or "request_not_editable" in message:
                raise RequestNotEditableError(
                    "Заявка недоступна для редактирования"
                ) from exc
            if "p0002" in message or "request_not_found" in message:
                raise ActiveDraftNotFoundError("Заявка не найдена") from exc
            if "idempotency_conflict" in message:
                raise IdempotencyConflictError(
                    "Idempotency key уже использован с другим обновлением"
                ) from exc
            if "23505" in message and command.idempotency_record is not None:
                try:
                    existing = self.find_idempotency(
                        command.idempotency_record.user_id,
                        command.idempotency_record.key,
                    )
                except IntakePersistenceRepositoryError:
                    existing = None
                if existing is not None:
                    if existing.fingerprint != command.idempotency_record.fingerprint:
                        raise IdempotencyConflictError(
                            "Idempotency key уже использован с другим обновлением"
                        ) from exc
                    return SavedIntakeStep(
                        request_version=existing.result.request_version,
                        dialog_state=existing.result.dialog_state,
                        replayed=True,
                    )
            if "40001" in message or "concurrent_intake_update" in message:
                raise ConcurrentIntakeUpdateError(
                    "Черновик был изменён другим процессом; загрузите его заново"
                ) from exc
            raise IntakePersistenceRepositoryError(
                "Failed to save intake step atomically; apply migration 007"
            ) from exc

    def list_message_logs(self, user_id: UUID) -> list[PersistenceMessageLog]:
        try:
            response = (
                self._client.table("message_logs")
                .select(
                    "user_id,request_id,direction,message_type,message_id,"
                    "idempotency_key,idempotency_fingerprint,field_code,"
                    "intake_status,payload,duration_ms,metadata,created_at"
                )
                .eq("user_id", str(user_id))
                .order("created_at")
                .execute()
            )
            return [PersistenceMessageLog.model_validate(row) for row in response.data]
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to read intake message logs"
            ) from exc

    def append_message_log(self, log: PersistenceMessageLog) -> None:
        payload = log.model_dump(mode="json")
        payload.update(
            {
                "user_message": "[intake system error]",
                "error": "Intake persistence operation failed",
            }
        )
        try:
            self._client.table("message_logs").insert(payload).execute()
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to append intake error log"
            ) from exc

    def clear_dialog_state(self, user_id: UUID) -> None:
        try:
            self._client.table("dialog_states").update(
                {"active_request_id": None, "state_data": {}}
            ).eq("user_id", str(user_id)).execute()
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Failed to clear intake dialog state"
            ) from exc

    def health_check(self) -> None:
        try:
            self._client.table("requests").select("id,version").limit(1).execute()
            self._client.table("message_logs").select("id,idempotency_key").limit(
                1
            ).execute()
        except Exception as exc:
            raise IntakePersistenceRepositoryError(
                "Intake persistence schema is unavailable; apply migration 007"
            ) from exc
