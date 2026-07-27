from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.intake.models import IntakeStatus, NextQuestion
from app.intake_persistence.models import PersistenceMessageLog, PersistentDialogState
from app.intake_persistence.repositories import InMemoryIntakeStorage
from app.request_lifecycle.exceptions import (
    LifecycleConcurrentUpdateError,
    LifecycleIdempotencyConflictError,
    LifecycleOwnershipError,
    LifecyclePersistenceError,
    LifecycleRequestNotFoundError,
    LifecycleTransitionError,
    RequestAlreadyCancelledError,
    RequestAlreadyRegisteredError,
    RequestNotReadyError,
)
from app.request_lifecycle.models import (
    LifecycleCommandResult,
    LifecycleCommandType,
    LifecycleIdempotencyRecord,
    LifecycleMutation,
    SavedLifecycleMutation,
)
from app.request_lifecycle.numbering import format_request_number
from app.schemas.common import RequestStatus
from app.schemas.request import RequestRead
from supabase import Client, create_client


class RequestLifecycleRepository(Protocol):
    def load_for_lifecycle(self, request_id: UUID) -> RequestRead | None: ...

    def find_lifecycle_idempotency_result(
        self, user_id: UUID, command_type: LifecycleCommandType, key: str
    ) -> LifecycleIdempotencyRecord | None: ...

    def confirm_and_register(
        self, mutation: LifecycleMutation
    ) -> SavedLifecycleMutation: ...

    def return_to_editing(
        self, mutation: LifecycleMutation
    ) -> SavedLifecycleMutation: ...

    def cancel_draft(self, mutation: LifecycleMutation) -> SavedLifecycleMutation: ...

    def mark_revalidated_dialog(
        self,
        user_id: UUID,
        request_id: UUID,
        expected_version: int,
        intake_status: IntakeStatus,
        next_question: NextQuestion | None,
    ) -> None: ...

    def get_by_request_number(self, request_number: str) -> RequestRead | None: ...

    def append_lifecycle_failure(
        self,
        user_id: UUID,
        request_id: UUID,
        command_type: LifecycleCommandType,
        idempotency_key: str,
        expected_version: int,
        error_type: str,
        duration_ms: int,
    ) -> None: ...


class InMemoryRequestLifecycleRepository:
    """Atomic lifecycle UoW sharing the intake in-memory storage."""

    def __init__(self, storage: InMemoryIntakeStorage) -> None:
        self.storage = storage

    def load_for_lifecycle(self, request_id: UUID) -> RequestRead | None:
        with self.storage.lock:
            item = self.storage.requests.get(request_id)
            return item.model_copy(deep=True) if item else None

    def find_lifecycle_idempotency_result(
        self, user_id: UUID, command_type: LifecycleCommandType, key: str
    ) -> LifecycleIdempotencyRecord | None:
        with self.storage.lock:
            item = self.storage.lifecycle_idempotency.get(
                (user_id, command_type.value, key)
            )
            return item.model_copy(deep=True) if item else None

    def confirm_and_register(
        self, mutation: LifecycleMutation
    ) -> SavedLifecycleMutation:
        return self._mutate(mutation)

    def return_to_editing(self, mutation: LifecycleMutation) -> SavedLifecycleMutation:
        return self._mutate(mutation)

    def cancel_draft(self, mutation: LifecycleMutation) -> SavedLifecycleMutation:
        return self._mutate(mutation)

    def mark_revalidated_dialog(
        self,
        user_id: UUID,
        request_id: UUID,
        expected_version: int,
        intake_status: IntakeStatus,
        next_question: NextQuestion | None,
    ) -> None:
        with self.storage.lock:
            request = self.storage.requests.get(request_id)
            if request is None or request.version != expected_version:
                return
            dialog = self.storage.dialog_states.get(user_id)
            if dialog is None:
                return
            updated = deepcopy(dialog)
            updated["intake_status"] = intake_status.value
            updated["awaiting_field_code"] = (
                next_question.field_code if next_question else None
            )
            updated["next_question"] = (
                next_question.model_dump(mode="json") if next_question else None
            )
            updated["state_version"] = request.version
            self.storage.dialog_states[user_id] = updated

    def get_by_request_number(self, request_number: str) -> RequestRead | None:
        with self.storage.lock:
            for request in self.storage.requests.values():
                if request.request_number == request_number:
                    return request.model_copy(deep=True)
        return None

    def append_lifecycle_failure(
        self,
        user_id: UUID,
        request_id: UUID,
        command_type: LifecycleCommandType,
        idempotency_key: str,
        expected_version: int,
        error_type: str,
        duration_ms: int,
    ) -> None:
        with self.storage.lock:
            request = self.storage.requests.get(request_id)
            if request is None or request.user_id != user_id:
                return
            incoming_type = {
                LifecycleCommandType.CONFIRM: "confirm_command",
                LifecycleCommandType.RETURN_TO_EDITING: "return_to_editing_command",
                LifecycleCommandType.CANCEL: "cancel_command",
            }[command_type]
            payload = {
                "command_type": command_type.value,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
            metadata = {"contains_secrets": False, "lifecycle": True}
            self.storage.message_logs.extend(
                [
                    PersistenceMessageLog(
                        user_id=user_id,
                        request_id=request_id,
                        direction="incoming",
                        message_type=incoming_type,
                        payload=payload,
                        duration_ms=duration_ms,
                        metadata=metadata,
                    ),
                    PersistenceMessageLog(
                        user_id=user_id,
                        request_id=request_id,
                        direction="outgoing",
                        message_type=(
                            "lifecycle_error"
                            if error_type == "LifecyclePersistenceError"
                            else "lifecycle_conflict"
                        ),
                        payload={
                            "command_type": command_type.value,
                            "error_type": error_type,
                        },
                        duration_ms=duration_ms,
                        metadata=metadata,
                    ),
                ]
            )

    def _mutate(self, mutation: LifecycleMutation) -> SavedLifecycleMutation:
        with self.storage.lock:
            replay = self._replay(mutation)
            if replay is not None:
                return replay
            current = self.storage.requests.get(mutation.request_id)
            if current is None:
                raise LifecyclePersistenceError("Заявка не найдена")
            if current.user_id != mutation.user_id:
                raise LifecycleOwnershipError("Заявка принадлежит другому пользователю")
            self._check_transition(current, mutation.command_type)
            if current.version != mutation.expected_version:
                raise LifecycleConcurrentUpdateError(
                    "Версия заявки устарела; обновите карточку"
                )
            if mutation.command_type == LifecycleCommandType.CONFIRM:
                lifecycle = mutation.request_data.get("lifecycle", {})
                if (
                    mutation.intake_status != IntakeStatus.COMPLETED
                    or mutation.request_data.get("intake", {}).get("intake_status")
                    != IntakeStatus.READY_FOR_CONFIRMATION.value
                    or mutation.completeness is None
                    or not mutation.completeness.is_complete
                    or mutation.request_card is None
                    or mutation.approval_route is None
                    or mutation.approval_route.status != "resolved"
                    or not lifecycle.get("final_request_card")
                ):
                    raise RequestNotReadyError(
                        "Заявка не готова к регистрации"
                    )

            now = datetime.now(UTC)
            sequence_value = None
            request_number = current.request_number
            if mutation.command_type == LifecycleCommandType.CONFIRM:
                self.storage.lifecycle_sequence += 1
                sequence_value = self.storage.lifecycle_sequence
                request_number = format_request_number(now, sequence_value)
                self._maybe_fail("lifecycle_number")

            new_version = current.version + 1
            status = {
                LifecycleCommandType.CONFIRM: RequestStatus.NEW,
                LifecycleCommandType.RETURN_TO_EDITING: RequestStatus.DRAFT,
                LifecycleCommandType.CANCEL: RequestStatus.CANCELLED,
            }[mutation.command_type]
            request_data = deepcopy(mutation.request_data)
            lifecycle = request_data.setdefault("lifecycle", {})
            if mutation.command_type == LifecycleCommandType.CONFIRM:
                lifecycle["confirmed_at"] = now.isoformat()
                lifecycle["registered_at"] = now.isoformat()
            elif mutation.command_type == LifecycleCommandType.CANCEL:
                lifecycle["cancelled_at"] = now.isoformat()
                lifecycle["cancelled_by"] = str(mutation.user_id)
                lifecycle["cancellation_reason"] = mutation.cancellation_reason
            update = {
                "request_type": mutation.request_type,
                "category_code": mutation.category_code,
                "title": mutation.title,
                "data": request_data,
                "status": status,
                "request_number": request_number,
                "version": new_version,
                "updated_at": now,
            }
            if mutation.command_type == LifecycleCommandType.CONFIRM:
                update.update(
                    registered_at=now,
                    confirmed_at=now,
                    confirmed_by=mutation.user_id,
                )
            elif mutation.command_type == LifecycleCommandType.CANCEL:
                update.update(
                    cancelled_at=now,
                    cancelled_by=mutation.user_id,
                    cancellation_reason=mutation.cancellation_reason,
                )
            updated = current.model_copy(update=update, deep=True)
            dialog = PersistentDialogState(
                user_id=mutation.user_id,
                request_id=mutation.request_id,
                intake_status=mutation.intake_status,
                state_version=new_version,
                metadata={
                    "request_status": status.value,
                    "active": status == RequestStatus.DRAFT,
                },
            )
            result = LifecycleCommandResult(
                request_id=updated.id,
                user_id=updated.user_id,
                request_number=updated.request_number,
                status=updated.status,
                intake_status=mutation.intake_status,
                version=new_version,
                registered_at=updated.registered_at,
                confirmed_at=updated.confirmed_at,
                cancelled_at=updated.cancelled_at,
                cancellation_reason=updated.cancellation_reason,
                request_card=mutation.request_card,
                approval_route=mutation.approval_route,
                editable=mutation.command_type
                == LifecycleCommandType.RETURN_TO_EDITING,
                editable_field_codes=mutation.editable_field_codes,
                instruction=(
                    "Отправьте structured update с изменяемыми полями"
                    if mutation.command_type == LifecycleCommandType.RETURN_TO_EDITING
                    else None
                ),
            )
            incoming, outgoing = _audit_logs(mutation, result)
            record = LifecycleIdempotencyRecord(
                user_id=mutation.user_id,
                request_id=mutation.request_id,
                command_type=mutation.command_type,
                key=mutation.idempotency_key,
                fingerprint=mutation.fingerprint,
                result=result,
            )
            self._maybe_fail("lifecycle_request")
            self._maybe_fail("lifecycle_dialog")
            self._maybe_fail("lifecycle_logs")
            self._maybe_fail("lifecycle_idempotency")
            self.storage.requests[updated.id] = updated
            self.storage.dialog_states[mutation.user_id] = dialog.model_dump(
                mode="json"
            )
            self.storage.message_logs.extend([incoming, outgoing])
            self.storage.lifecycle_idempotency[
                (
                    mutation.user_id,
                    mutation.command_type.value,
                    mutation.idempotency_key,
                )
            ] = record
            return SavedLifecycleMutation(result=result)

    def _replay(self, mutation: LifecycleMutation) -> SavedLifecycleMutation | None:
        existing = self.storage.lifecycle_idempotency.get(
            (mutation.user_id, mutation.command_type.value, mutation.idempotency_key)
        )
        if existing is None:
            return None
        if existing.fingerprint != mutation.fingerprint:
            raise LifecycleIdempotencyConflictError(
                "Idempotency key уже использован с другой lifecycle-командой"
            )
        result = existing.result.model_copy(deep=True)
        result.replayed = True
        return SavedLifecycleMutation(result=result, replayed=True)

    @staticmethod
    def _check_transition(
        request: RequestRead, command_type: LifecycleCommandType
    ) -> None:
        if request.status == RequestStatus.NEW:
            raise RequestAlreadyRegisteredError("Заявка уже зарегистрирована")
        if request.status == RequestStatus.CANCELLED:
            raise RequestAlreadyCancelledError("Черновик уже отменён")
        if request.status != RequestStatus.DRAFT:
            raise LifecycleTransitionError("Lifecycle-команда неприменима")

    def _maybe_fail(self, stage: str) -> None:
        if self.storage.fail_at == stage:
            raise LifecyclePersistenceError(f"Simulated lifecycle failure at {stage}")


class SupabaseRequestLifecycleRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    def from_credentials(
        cls, url: str, service_role_key: str
    ) -> SupabaseRequestLifecycleRepository:
        return cls(create_client(url, service_role_key))

    def load_for_lifecycle(self, request_id: UUID) -> RequestRead | None:
        try:
            response = (
                self._client.table("requests")
                .select("*")
                .eq("id", str(request_id))
                .limit(1)
                .execute()
            )
            return (
                RequestRead.model_validate(response.data[0]) if response.data else None
            )
        except Exception as exc:
            raise LifecyclePersistenceError("Не удалось прочитать заявку") from exc

    def find_lifecycle_idempotency_result(
        self, user_id: UUID, command_type: LifecycleCommandType, key: str
    ) -> LifecycleIdempotencyRecord | None:
        try:
            response = (
                self._client.table("request_lifecycle_commands")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("command_type", command_type.value)
                .eq("idempotency_key", key)
                .limit(1)
                .execute()
            )
            if not response.data:
                return None
            row = response.data[0]
            return LifecycleIdempotencyRecord(
                user_id=row["user_id"],
                request_id=row["request_id"],
                command_type=row["command_type"],
                key=row["idempotency_key"],
                fingerprint=row["fingerprint"],
                result=row["result"],
            )
        except Exception as exc:
            raise LifecyclePersistenceError(
                "Не удалось прочитать lifecycle idempotency"
            ) from exc

    def confirm_and_register(
        self, mutation: LifecycleMutation
    ) -> SavedLifecycleMutation:
        return self._rpc("confirm_request", mutation)

    def return_to_editing(self, mutation: LifecycleMutation) -> SavedLifecycleMutation:
        return self._rpc("return_request_to_editing", mutation)

    def cancel_draft(self, mutation: LifecycleMutation) -> SavedLifecycleMutation:
        return self._rpc("cancel_request", mutation)

    def mark_revalidated_dialog(
        self,
        user_id: UUID,
        request_id: UUID,
        expected_version: int,
        intake_status: IntakeStatus,
        next_question: NextQuestion | None,
    ) -> None:
        try:
            self._client.rpc(
                "mark_request_collecting",
                {
                    "user_id": str(user_id),
                    "request_id": str(request_id),
                    "expected_version": expected_version,
                    "intake_status": intake_status.value,
                    "next_question": (
                        next_question.model_dump(mode="json")
                        if next_question
                        else None
                    ),
                },
            ).execute()
        except Exception as exc:
            raise LifecyclePersistenceError(
                "Не удалось обновить состояние подтверждения"
            ) from exc

    def get_by_request_number(self, request_number: str) -> RequestRead | None:
        try:
            response = (
                self._client.table("requests")
                .select("*")
                .eq("request_number", request_number)
                .limit(1)
                .execute()
            )
            return (
                RequestRead.model_validate(response.data[0]) if response.data else None
            )
        except Exception as exc:
            raise LifecyclePersistenceError("Не удалось прочитать заявку") from exc

    def append_lifecycle_failure(
        self,
        user_id: UUID,
        request_id: UUID,
        command_type: LifecycleCommandType,
        idempotency_key: str,
        expected_version: int,
        error_type: str,
        duration_ms: int,
    ) -> None:
        event = {
            "user_id": str(user_id),
            "request_id": str(request_id),
            "command_type": command_type.value,
            "idempotency_key": idempotency_key,
            "expected_version": expected_version,
            "error_type": error_type,
            "duration_ms": duration_ms,
        }
        try:
            self._client.rpc(
                "record_request_lifecycle_failure", {"event": event}
            ).execute()
        except Exception as exc:
            raise LifecyclePersistenceError(
                "Не удалось сохранить lifecycle failure audit"
            ) from exc

    def _rpc(self, name: str, mutation: LifecycleMutation) -> SavedLifecycleMutation:
        try:
            response = self._client.rpc(
                name, {"command": mutation.model_dump(mode="json")}
            ).execute()
            data = (
                response.data[0] if isinstance(response.data, list) else response.data
            )
            if not isinstance(data, dict) or "result" not in data:
                raise ValueError("Malformed lifecycle RPC response")
            result = LifecycleCommandResult.model_validate(data["result"])
            result.replayed = bool(data.get("replayed", False))
            return SavedLifecycleMutation(result=result, replayed=result.replayed)
        except Exception as exc:
            message = str(exc).casefold()
            if "idempotency_conflict" in message:
                raise LifecycleIdempotencyConflictError(
                    "Idempotency key уже использован с другой lifecycle-командой"
                ) from exc
            if "ownership_mismatch" in message or "42501" in message:
                raise LifecycleOwnershipError(
                    "Заявка принадлежит другому пользователю"
                ) from exc
            if "request_not_found" in message or "p0002" in message:
                raise LifecycleRequestNotFoundError("Заявка не найдена") from exc
            if "request_not_ready" in message:
                raise RequestNotReadyError("Заявка не готова к регистрации") from exc
            if "concurrent_lifecycle_update" in message or "40001" in message:
                raise LifecycleConcurrentUpdateError(
                    "Версия заявки устарела; обновите карточку"
                ) from exc
            if "already_registered" in message:
                raise RequestAlreadyRegisteredError(
                    "Заявка уже зарегистрирована"
                ) from exc
            if "already_cancelled" in message:
                raise RequestAlreadyCancelledError("Черновик уже отменён") from exc
            if (
                "transition_not_allowed" in message
                or "lifecycle_dialog_mismatch" in message
            ):
                raise LifecycleTransitionError("Lifecycle-команда неприменима") from exc
            raise LifecyclePersistenceError(
                "Не удалось атомарно сохранить lifecycle-команду; "
                "примените migration 008"
            ) from exc


def _audit_logs(
    mutation: LifecycleMutation, result: LifecycleCommandResult
) -> tuple[PersistenceMessageLog, PersistenceMessageLog]:
    incoming_type = {
        LifecycleCommandType.CONFIRM: "confirm_command",
        LifecycleCommandType.RETURN_TO_EDITING: "return_to_editing_command",
        LifecycleCommandType.CANCEL: "cancel_command",
    }[mutation.command_type]
    outgoing_type = {
        LifecycleCommandType.CONFIRM: "request_registered",
        LifecycleCommandType.RETURN_TO_EDITING: "request_returned_to_editing",
        LifecycleCommandType.CANCEL: "request_cancelled",
    }[mutation.command_type]
    common = {
        "command_type": mutation.command_type.value,
        "expected_version": mutation.expected_version,
        "resulting_version": result.version,
        "request_number": result.request_number,
    }
    incoming = PersistenceMessageLog(
        user_id=mutation.user_id,
        request_id=mutation.request_id,
        direction="incoming",
        message_type=incoming_type,
        payload={**common, "idempotency_key": mutation.idempotency_key},
        duration_ms=mutation.duration_ms,
        metadata={"contains_secrets": False, "lifecycle": True},
    )
    outgoing = PersistenceMessageLog(
        user_id=mutation.user_id,
        request_id=mutation.request_id,
        direction="outgoing",
        message_type=outgoing_type,
        intake_status=result.intake_status,
        payload={**common, "status": result.status.value},
        duration_ms=mutation.duration_ms,
        metadata={"contains_secrets": False, "lifecycle": True},
    )
    return incoming, outgoing
