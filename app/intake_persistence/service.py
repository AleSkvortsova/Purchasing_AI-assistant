import hashlib
import json
from datetime import date
from decimal import Decimal
from enum import Enum
from time import perf_counter
from uuid import UUID

from app.intake.models import IntakeFieldUpdate, IntakeStatus
from app.intake.service import RequestIntakeService
from app.intake.validators import IntakeFieldValidator
from app.intake_persistence.exceptions import (
    ActiveDraftNotFoundError,
    ConcurrentIntakeUpdateError,
    IdempotencyConflictError,
    IntakePersistenceRepositoryError,
    MultipleActiveDraftsError,
    PersistencePartialFailureError,
    RequestNotEditableError,
    RequestOwnershipError,
)
from app.intake_persistence.mappers import IntakePersistenceMapper
from app.intake_persistence.models import (
    IdempotencyRecord,
    MessageEnvelope,
    PersistenceMessageLog,
    PersistentIntakeStepResult,
    SaveIntakeStepCommand,
)
from app.intake_persistence.repositories import IntakePersistenceRepository
from app.schemas.common import RequestStatus
from app.schemas.request import RequestRead


class PersistentIntakeOrchestrator:
    def __init__(
        self,
        repository: IntakePersistenceRepository,
        intake_service: RequestIntakeService | None = None,
        mapper: IntakePersistenceMapper | None = None,
    ) -> None:
        self.repository = repository
        self.intake_service = intake_service or RequestIntakeService()
        self.mapper = mapper or IntakePersistenceMapper()

    def process_structured_step(
        self,
        user_id: UUID | str,
        update: IntakeFieldUpdate,
        request_id: UUID | None = None,
        incoming_message: MessageEnvelope | None = None,
        idempotency_key: str | None = None,
        *,
        _allow_concurrent_retry: bool = True,
    ) -> PersistentIntakeStepResult:
        started = perf_counter()
        normalized_user_id = UUID(str(user_id))
        fingerprint = _fingerprint(update, request_id)
        if idempotency_key:
            replay = self.repository.find_idempotency(
                normalized_user_id, idempotency_key
            )
            if replay is not None:
                if replay.fingerprint != fingerprint:
                    raise IdempotencyConflictError(
                        "Idempotency key уже использован с другим обновлением"
                    )
                if request_id is not None and replay.result.request_id != request_id:
                    raise IdempotencyConflictError(
                        "Idempotency key уже связан с другой заявкой"
                    )
                result = replay.result.model_copy(deep=True)
                result.replayed = True
                result.persistence_status = "replayed"
                result.metadata["idempotency_protected"] = True
                return result

        request, created = self._resolve_request(normalized_user_id, request_id)
        dialog = self.repository.get_dialog_state(normalized_user_id)
        if dialog is not None and dialog.intake_status in {
            IntakeStatus.COMPLETED,
            IntakeStatus.CANCELLED,
        }:
            if dialog.request_id == request.id:
                raise PersistencePartialFailureError(
                    "Активный черновик имеет завершённое состояние диалога"
                )
            dialog = None
        if dialog is not None and (
            dialog.user_id != normalized_user_id or dialog.request_id != request.id
        ):
            raise PersistencePartialFailureError(
                "Dialog state относится к другой активной заявке"
            )
        if dialog is not None and dialog.state_version != request.version:
            raise PersistencePartialFailureError(
                "Версия dialog state не совпадает с версией черновика"
            )
        draft = self.mapper.request_to_draft(request)
        intake_result = self.intake_service.process_step(draft, update)
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        next_version = request.version + 1
        dialog_state = self.mapper.result_to_dialog_state(
            normalized_user_id,
            request.id,
            intake_result,
            next_version,
        )
        patch = self.mapper.draft_to_request_update(
            intake_result.draft,
            intake_result,
            existing_data=request.data,
            audit_metadata={
                "last_idempotency_key": idempotency_key,
                "last_message_id": (
                    incoming_message.message_id if incoming_message else None
                ),
            },
        )
        provisional = PersistentIntakeStepResult(
            request_id=request.id,
            user_id=normalized_user_id,
            request_version=next_version,
            created_new_request=created,
            replayed=False,
            intake_result=intake_result,
            dialog_state=dialog_state,
            persistence_status="saved",
            metadata={
                "idempotency_protected": idempotency_key is not None,
                "atomic_step_save": True,
            },
        )
        incoming = _incoming_log(
            normalized_user_id,
            request.id,
            update,
            incoming_message,
            idempotency_key,
            fingerprint,
        )
        outgoing = _outgoing_log(
            normalized_user_id,
            request.id,
            intake_result,
            duration_ms,
        )
        idempotency_record = (
            IdempotencyRecord(
                user_id=normalized_user_id,
                key=idempotency_key,
                fingerprint=fingerprint,
                result=provisional,
            )
            if idempotency_key
            else None
        )
        command = SaveIntakeStepCommand(
            request_id=request.id,
            expected_version=request.version,
            request_type=(
                patch.request_type.value if patch.request_type is not None else None
            ),
            category_code=patch.category_code,
            title=patch.title,
            request_data=patch.data or {},
            dialog_state=dialog_state,
            incoming_log=incoming,
            outgoing_log=outgoing,
            idempotency_record=idempotency_record,
        )
        try:
            saved = self.repository.save_step(command)
        except ConcurrentIntakeUpdateError:
            replay = (
                self.repository.find_idempotency(normalized_user_id, idempotency_key)
                if idempotency_key
                else None
            )
            if replay is not None and replay.fingerprint == fingerprint:
                replayed_result = replay.result.model_copy(deep=True)
                replayed_result.replayed = True
                replayed_result.persistence_status = "replayed"
                return replayed_result
            if _allow_concurrent_retry:
                return self.process_structured_step(
                    normalized_user_id,
                    update,
                    request.id,
                    incoming_message,
                    idempotency_key,
                    _allow_concurrent_retry=False,
                )
            raise
        except IntakePersistenceRepositoryError:
            self._append_error_log(
                normalized_user_id,
                request.id,
                duration_ms,
            )
            if created:
                provisional.request_version = request.version
                provisional.persistence_status = "partial_failure"
                provisional.warnings.append(
                    "Черновик создан, но шаг не сохранён; повторите запрос"
                )
                provisional.metadata["recovery_required"] = True
                return provisional
            raise
        provisional.request_version = saved.request_version
        provisional.dialog_state = saved.dialog_state
        if saved.replayed and idempotency_key:
            replay = self.repository.find_idempotency(
                normalized_user_id, idempotency_key
            )
            if replay is not None and replay.fingerprint == fingerprint:
                replayed_result = replay.result.model_copy(deep=True)
                replayed_result.replayed = True
                replayed_result.persistence_status = "replayed"
                return replayed_result
        return provisional

    def _append_error_log(
        self, user_id: UUID, request_id: UUID, duration_ms: int
    ) -> None:
        try:
            self.repository.append_message_log(
                PersistenceMessageLog(
                    user_id=user_id,
                    request_id=request_id,
                    direction="outgoing",
                    message_type="system_error",
                    duration_ms=duration_ms,
                    metadata={
                        "safe_error": "intake_step_persistence_failed",
                        "contains_secrets": False,
                    },
                )
            )
        except IntakePersistenceRepositoryError:
            pass

    def get_active_session(self, user_id: UUID | str) -> PersistentIntakeStepResult:
        normalized_user_id = UUID(str(user_id))
        requests = self.repository.find_active_requests(normalized_user_id)
        if not requests:
            raise ActiveDraftNotFoundError("Активный черновик не найден")
        if len(requests) > 1:
            raise MultipleActiveDraftsError([str(item.id) for item in requests])
        request = requests[0]
        draft = self.mapper.request_to_draft(request)
        intake_result = self.intake_service.process_step(draft, IntakeFieldUpdate())
        dialog = self.repository.get_dialog_state(normalized_user_id)
        if dialog is None:
            dialog = self.mapper.result_to_dialog_state(
                normalized_user_id,
                request.id,
                intake_result,
                request.version,
            )
        if dialog.request_id != request.id:
            raise PersistencePartialFailureError(
                "Dialog state относится к другой активной заявке"
            )
        if dialog.intake_status in {IntakeStatus.COMPLETED, IntakeStatus.CANCELLED}:
            raise PersistencePartialFailureError(
                "Активный черновик имеет завершённое состояние диалога"
            )
        if dialog.state_version != request.version:
            raise PersistencePartialFailureError(
                "Версия dialog state не совпадает с версией черновика"
            )
        return PersistentIntakeStepResult(
            request_id=request.id,
            user_id=normalized_user_id,
            request_version=request.version,
            intake_result=intake_result,
            dialog_state=dialog,
            persistence_status="saved",
            metadata={"reconstructed": True},
        )

    def _resolve_request(
        self, user_id: UUID, request_id: UUID | None
    ) -> tuple[RequestRead, bool]:
        if request_id is not None:
            request = self.repository.get_request(request_id)
            if request is None:
                raise ActiveDraftNotFoundError("Заявка не найдена")
            if request.user_id != user_id:
                raise RequestOwnershipError("Заявка принадлежит другому пользователю")
            if request.status != RequestStatus.DRAFT:
                raise RequestNotEditableError("Заявка недоступна для редактирования")
            return request, False
        active, created = self.repository.get_or_create_active_request(user_id)
        if len(active) > 1:
            raise MultipleActiveDraftsError([str(item.id) for item in active])
        return active[0], created


def _fingerprint(update: IntakeFieldUpdate, request_id: UUID | None) -> str:
    validator = IntakeFieldValidator()
    normalized_values = {}
    for field_code, value in update.values.items():
        try:
            value = validator.normalize(field_code, value)
        except ValueError:
            pass
        normalized_values[field_code] = _canonical_json_value(value)
    payload = {
        "update": {
            "values": normalized_values,
            "source": update.source.value,
            "explicit_correction": update.explicit_correction,
            "evidence_by_field": update.evidence_by_field,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_value(value):
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        return str(value.normalize())
    if isinstance(value, (date, UUID)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _canonical_json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return value


def _incoming_log(
    user_id: UUID,
    request_id: UUID,
    update: IntakeFieldUpdate,
    envelope: MessageEnvelope | None,
    idempotency_key: str | None,
    fingerprint: str,
) -> PersistenceMessageLog:
    return PersistenceMessageLog(
        user_id=user_id,
        request_id=request_id,
        direction="incoming",
        message_type="structured_update",
        message_id=envelope.message_id if envelope else update.message_id,
        idempotency_key=idempotency_key,
        idempotency_fingerprint=fingerprint if idempotency_key else None,
        payload={"update": update.model_dump(mode="json")},
        metadata=_safe_metadata(envelope.metadata if envelope else {}),
    )


def _outgoing_log(
    user_id: UUID,
    request_id: UUID,
    result,
    duration_ms: int,
) -> PersistenceMessageLog:
    if result.status == "conflict":
        message_type = "conflict"
    elif result.request_card is not None:
        message_type = "card"
    else:
        message_type = "question"
    return PersistenceMessageLog(
        user_id=user_id,
        request_id=request_id,
        direction="outgoing",
        message_type=message_type,
        field_code=result.next_question.field_code if result.next_question else None,
        intake_status=result.status,
        payload={
            "status": result.status.value,
            "next_question": (
                result.next_question.model_dump(mode="json")
                if result.next_question
                else None
            ),
        },
        duration_ms=duration_ms,
        metadata={"contains_secrets": False},
    )


def _safe_metadata(metadata: dict) -> dict:
    sensitive_markers = {
        "api_key",
        "authorization",
        "password",
        "secret",
        "system_prompt",
        "token",
    }
    return {
        key: value
        for key, value in metadata.items()
        if not any(marker in key.casefold() for marker in sensitive_markers)
    }
