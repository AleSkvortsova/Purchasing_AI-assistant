import hashlib
import json
from copy import deepcopy
from functools import wraps
from time import perf_counter
from typing import Any
from uuid import UUID

from app.intake.models import IntakeFieldUpdate, IntakeStatus, RequestCard
from app.intake.service import RequestIntakeService
from app.intake_persistence.mappers import IntakePersistenceMapper
from app.request_lifecycle.exceptions import (
    LifecycleConcurrentUpdateError,
    LifecycleIdempotencyConflictError,
    LifecycleOwnershipError,
    LifecycleRequestNotFoundError,
    LifecycleTransitionError,
    RequestAlreadyCancelledError,
    RequestAlreadyRegisteredError,
    RequestLifecycleError,
    RequestNotReadyError,
)
from app.request_lifecycle.models import (
    ConfirmationView,
    LifecycleCommandResult,
    LifecycleCommandType,
    LifecycleMutation,
)
from app.request_lifecycle.policies import confirmation_blocking_reasons
from app.request_lifecycle.repositories import RequestLifecycleRepository
from app.rules.models import ApprovalRouteResult
from app.schemas.common import RequestStatus
from app.schemas.request import RequestRead

REGISTERED_SCHEMA_VERSION = 1
REGISTRY_VERSION = "intake-registry-mvp-1"
APPROVAL_RULES_VERSION = "approval-rules-runtime"


def _audit_failures(command_type: LifecycleCommandType):
    def decorator(method):
        @wraps(method)
        def wrapped(
            self,
            request_id,
            user_id,
            expected_version,
            idempotency_key,
            *args,
            **kwargs,
        ):
            started = perf_counter()
            try:
                return method(
                    self,
                    request_id,
                    user_id,
                    expected_version,
                    idempotency_key,
                    *args,
                    **kwargs,
                )
            except RequestLifecycleError as exc:
                self._append_failure_audit(
                    UUID(str(user_id)),
                    UUID(str(request_id)),
                    command_type,
                    idempotency_key,
                    expected_version,
                    exc,
                    started,
                )
                raise

        return wrapped

    return decorator


class RequestLifecycleService:
    def __init__(
        self,
        repository: RequestLifecycleRepository,
        intake_service: RequestIntakeService,
        mapper: IntakePersistenceMapper | None = None,
    ) -> None:
        self.repository = repository
        self.intake_service = intake_service
        self.mapper = mapper or IntakePersistenceMapper()

    def get_confirmation_view(
        self, request_id: UUID | str, user_id: UUID | str
    ) -> ConfirmationView:
        request = self._load_owned(UUID(str(request_id)), UUID(str(user_id)))
        return self._confirmation_view(request)

    def get_lifecycle_state(
        self, request_id: UUID | str, user_id: UUID | str
    ) -> ConfirmationView:
        return self.get_confirmation_view(request_id, user_id)

    @_audit_failures(LifecycleCommandType.CONFIRM)
    def confirm_request(
        self,
        request_id: UUID | str,
        user_id: UUID | str,
        expected_version: int,
        idempotency_key: str,
    ) -> LifecycleCommandResult:
        started = perf_counter()
        normalized_request_id = UUID(str(request_id))
        normalized_user_id = UUID(str(user_id))
        fingerprint = _fingerprint(
            normalized_user_id,
            normalized_request_id,
            LifecycleCommandType.CONFIRM,
            {},
        )
        replay = self._replay(
            normalized_user_id,
            normalized_request_id,
            LifecycleCommandType.CONFIRM,
            idempotency_key,
            fingerprint,
        )
        if replay is not None:
            return replay
        request = self._load_owned(normalized_request_id, normalized_user_id)
        self._require_draft(request)
        self._require_version(request, expected_version)
        view, intake_result = self._evaluate(request)
        if not view.confirmable:
            self.repository.mark_revalidated_dialog(
                normalized_user_id,
                normalized_request_id,
                expected_version,
                intake_result.status,
                intake_result.next_question,
            )
            raise RequestNotReadyError(
                "Заявка не готова к регистрации", confirmation_view=view
            )
        assert intake_result.request_card is not None
        assert intake_result.approval_route is not None
        canonical_patch = self.mapper.draft_to_request_update(
            intake_result.draft,
            intake_result,
            existing_data=request.data,
        )
        request_data = _with_registration_snapshot(
            canonical_patch.data or {}, intake_result
        )
        mutation = LifecycleMutation(
            user_id=normalized_user_id,
            request_id=normalized_request_id,
            command_type=LifecycleCommandType.CONFIRM,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            request_data=request_data,
            request_type=canonical_patch.request_type,
            category_code=canonical_patch.category_code,
            title=canonical_patch.title,
            intake_status=IntakeStatus.COMPLETED,
            request_card=intake_result.request_card,
            approval_route=intake_result.approval_route,
            completeness=intake_result.completeness,
            duration_ms=_duration_ms(started),
        )
        return self.repository.confirm_and_register(mutation).result

    @_audit_failures(LifecycleCommandType.RETURN_TO_EDITING)
    def return_to_editing(
        self,
        request_id: UUID | str,
        user_id: UUID | str,
        expected_version: int,
        idempotency_key: str,
    ) -> LifecycleCommandResult:
        started = perf_counter()
        normalized_request_id = UUID(str(request_id))
        normalized_user_id = UUID(str(user_id))
        fingerprint = _fingerprint(
            normalized_user_id,
            normalized_request_id,
            LifecycleCommandType.RETURN_TO_EDITING,
            {},
        )
        replay = self._replay(
            normalized_user_id,
            normalized_request_id,
            LifecycleCommandType.RETURN_TO_EDITING,
            idempotency_key,
            fingerprint,
        )
        if replay is not None:
            return replay
        request = self._load_owned(normalized_request_id, normalized_user_id)
        self._require_draft(request)
        self._require_version(request, expected_version)
        view, intake_result = self._evaluate(request)
        persisted_status = _persisted_intake_status(request)
        if (
            persisted_status != IntakeStatus.READY_FOR_CONFIRMATION.value
            or intake_result.status != IntakeStatus.READY_FOR_CONFIRMATION
        ):
            raise LifecycleTransitionError(
                "Вернуть к редактированию можно только заявку, ожидающую подтверждения"
            )
        data = deepcopy(request.data)
        data.setdefault("intake", {})["intake_status"] = IntakeStatus.EDITING.value
        editable = [
            field.code
            for field in self.intake_service.registry.all()
            if field.allows_explicit_correction
            and field.code not in {"request_id", "requester_id"}
        ]
        mutation = LifecycleMutation(
            user_id=normalized_user_id,
            request_id=normalized_request_id,
            command_type=LifecycleCommandType.RETURN_TO_EDITING,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            request_data=data,
            request_type=request.request_type,
            category_code=request.category_code,
            title=request.title,
            intake_status=IntakeStatus.EDITING,
            request_card=view.request_card,
            approval_route=view.approval_route,
            editable_field_codes=editable,
            duration_ms=_duration_ms(started),
        )
        return self.repository.return_to_editing(mutation).result

    @_audit_failures(LifecycleCommandType.CANCEL)
    def cancel_draft(
        self,
        request_id: UUID | str,
        user_id: UUID | str,
        expected_version: int,
        idempotency_key: str,
        reason: str | None = None,
    ) -> LifecycleCommandResult:
        started = perf_counter()
        normalized_request_id = UUID(str(request_id))
        normalized_user_id = UUID(str(user_id))
        normalized_reason = _normalize_reason(reason)
        fingerprint = _fingerprint(
            normalized_user_id,
            normalized_request_id,
            LifecycleCommandType.CANCEL,
            {"reason": normalized_reason},
        )
        replay = self._replay(
            normalized_user_id,
            normalized_request_id,
            LifecycleCommandType.CANCEL,
            idempotency_key,
            fingerprint,
        )
        if replay is not None:
            return replay
        request = self._load_owned(normalized_request_id, normalized_user_id)
        self._require_draft(request)
        self._require_version(request, expected_version)
        data = deepcopy(request.data)
        data.setdefault("intake", {})["intake_status"] = IntakeStatus.CANCELLED.value
        data["lifecycle"] = {
            **data.get("lifecycle", {}),
            "cancelled_by": str(normalized_user_id),
            "cancellation_reason": normalized_reason,
        }
        mutation = LifecycleMutation(
            user_id=normalized_user_id,
            request_id=normalized_request_id,
            command_type=LifecycleCommandType.CANCEL,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            request_data=data,
            request_type=request.request_type,
            category_code=request.category_code,
            title=request.title,
            intake_status=IntakeStatus.CANCELLED,
            cancellation_reason=normalized_reason,
            duration_ms=_duration_ms(started),
        )
        return self.repository.cancel_draft(mutation).result

    def get_by_request_number(
        self, request_number: str, user_id: UUID | str
    ) -> RequestRead:
        request = self.repository.get_by_request_number(request_number.strip())
        if request is None:
            raise LifecycleRequestNotFoundError("Заявка не найдена")
        if request.user_id != UUID(str(user_id)):
            raise LifecycleOwnershipError("Заявка принадлежит другому пользователю")
        return request

    def _confirmation_view(self, request: RequestRead) -> ConfirmationView:
        if request.status in {RequestStatus.NEW, RequestStatus.CANCELLED}:
            lifecycle = request.data.get("lifecycle", {})
            card_payload = lifecycle.get("final_request_card")
            route_payload = lifecycle.get("final_approval_route")
            return ConfirmationView(
                request_id=request.id,
                request_version=request.version,
                request_status=request.status,
                intake_status=(
                    IntakeStatus.COMPLETED
                    if request.status == RequestStatus.NEW
                    else IntakeStatus.CANCELLED
                ),
                request_card=(
                    RequestCard.model_validate(card_payload) if card_payload else None
                ),
                approval_route=(
                    ApprovalRouteResult.model_validate(route_payload)
                    if route_payload
                    else None
                ),
                warnings=[],
                editable=False,
                confirmable=False,
                blocking_reasons=[
                    "Заявка уже зарегистрирована"
                    if request.status == RequestStatus.NEW
                    else "Черновик отменён"
                ],
                updated_at=request.updated_at,
            )
        return self._evaluate(request)[0]

    def _evaluate(self, request: RequestRead):
        draft = self.mapper.request_to_draft(request)
        try:
            result = self.intake_service.process_step(draft, IntakeFieldUpdate())
        except Exception as exc:
            raise RequestNotReadyError(
                "Не удалось пересчитать готовность и маршрут согласования"
            ) from exc
        blocking = confirmation_blocking_reasons(
            request.status, result, _persisted_intake_status(request)
        )
        view = ConfirmationView(
            request_id=request.id,
            request_version=request.version,
            request_status=request.status,
            intake_status=result.status,
            request_card=result.request_card,
            approval_route=result.approval_route,
            warnings=result.warnings,
            editable=request.status == RequestStatus.DRAFT,
            confirmable=not blocking,
            blocking_reasons=blocking,
            updated_at=request.updated_at,
        )
        return view, result

    def _replay(
        self,
        user_id: UUID,
        request_id: UUID,
        command_type: LifecycleCommandType,
        key: str,
        fingerprint: str,
    ) -> LifecycleCommandResult | None:
        existing = self.repository.find_lifecycle_idempotency_result(
            user_id, command_type, key
        )
        if existing is None:
            return None
        if existing.fingerprint != fingerprint or existing.request_id != request_id:
            raise LifecycleIdempotencyConflictError(
                "Idempotency key уже использован с другой lifecycle-командой"
            )
        result = existing.result.model_copy(deep=True)
        result.replayed = True
        return result

    def _load_owned(self, request_id: UUID, user_id: UUID) -> RequestRead:
        request = self.repository.load_for_lifecycle(request_id)
        if request is None:
            raise LifecycleRequestNotFoundError("Заявка не найдена")
        if request.user_id != user_id:
            raise LifecycleOwnershipError("Заявка принадлежит другому пользователю")
        return request

    def _append_failure_audit(
        self,
        user_id: UUID,
        request_id: UUID,
        command_type: LifecycleCommandType,
        idempotency_key: str,
        expected_version: int,
        exc: RequestLifecycleError,
        started: float,
    ) -> None:
        if isinstance(exc, (LifecycleOwnershipError, LifecycleRequestNotFoundError)):
            return
        try:
            self.repository.append_lifecycle_failure(
                user_id,
                request_id,
                command_type,
                idempotency_key,
                expected_version,
                type(exc).__name__,
                _duration_ms(started),
            )
        except RequestLifecycleError:
            pass

    @staticmethod
    def _require_version(request: RequestRead, expected_version: int) -> None:
        if request.version != expected_version:
            raise LifecycleConcurrentUpdateError(
                "Версия заявки устарела; обновите карточку"
            )

    @staticmethod
    def _require_draft(request: RequestRead) -> None:
        if request.status == RequestStatus.NEW:
            raise RequestAlreadyRegisteredError("Заявка уже зарегистрирована")
        if request.status == RequestStatus.CANCELLED:
            raise RequestAlreadyCancelledError("Черновик уже отменён")
        if request.status != RequestStatus.DRAFT:
            raise LifecycleTransitionError("Lifecycle-команда неприменима")


def _with_registration_snapshot(data: dict[str, Any], result) -> dict[str, Any]:
    snapshot = deepcopy(data)
    intake = snapshot.setdefault("intake", {})
    intake["intake_status"] = IntakeStatus.COMPLETED.value
    intake["next_question"] = None
    snapshot["lifecycle"] = {
        **snapshot.get("lifecycle", {}),
        "registered_schema_version": REGISTERED_SCHEMA_VERSION,
        "confirmed_by": str(result.draft.requester_id),
        "final_request_card": result.request_card.model_dump(mode="json"),
        "final_approval_route": result.approval_route.model_dump(mode="json"),
        "final_completeness": result.completeness.model_dump(mode="json"),
        "registry_version": REGISTRY_VERSION,
        "approval_rules_version": APPROVAL_RULES_VERSION,
    }
    return snapshot


def _persisted_intake_status(request: RequestRead) -> str | None:
    intake = request.data.get("intake") if isinstance(request.data, dict) else None
    return intake.get("intake_status") if isinstance(intake, dict) else None


def _fingerprint(
    user_id: UUID,
    request_id: UUID,
    command_type: LifecycleCommandType,
    payload: dict[str, Any],
) -> str:
    canonical = {
        "user_id": str(user_id),
        "request_id": str(request_id),
        "command_type": command_type.value,
        "payload": payload,
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    normalized = " ".join(reason.split())
    return normalized or None


def _duration_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))
