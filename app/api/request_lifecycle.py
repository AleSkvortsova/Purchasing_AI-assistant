from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api.dependencies import get_request_lifecycle_service
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
from app.request_lifecycle.models import ConfirmationView, LifecycleCommandResult
from app.request_lifecycle.service import RequestLifecycleService
from app.schemas.request import RequestRead

router = APIRouter(prefix="/requests", tags=["request-lifecycle"])
LifecycleServiceDependency = Annotated[
    RequestLifecycleService, Depends(get_request_lifecycle_service)
]


class LifecycleCommandRequest(BaseModel):
    user_id: UUID
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key must not be blank")
        return normalized


class CancelRequestCommand(LifecycleCommandRequest):
    reason: str | None = Field(default=None, max_length=1000)


@router.get("/by-number/{request_number}", response_model=RequestRead)
def get_by_request_number(
    request_number: str,
    user_id: UUID,
    service: LifecycleServiceDependency,
) -> RequestRead:
    try:
        return service.get_by_request_number(request_number, user_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{request_id}/confirmation", response_model=ConfirmationView)
def get_confirmation_view(
    request_id: UUID,
    user_id: UUID,
    service: LifecycleServiceDependency,
) -> ConfirmationView:
    try:
        return service.get_confirmation_view(request_id, user_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{request_id}/confirm", response_model=LifecycleCommandResult)
def confirm_request(
    request_id: UUID,
    command: LifecycleCommandRequest,
    service: LifecycleServiceDependency,
) -> LifecycleCommandResult:
    try:
        return service.confirm_request(
            request_id,
            command.user_id,
            command.expected_version,
            command.idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{request_id}/return-to-editing", response_model=LifecycleCommandResult)
def return_to_editing(
    request_id: UUID,
    command: LifecycleCommandRequest,
    service: LifecycleServiceDependency,
) -> LifecycleCommandResult:
    try:
        return service.return_to_editing(
            request_id,
            command.user_id,
            command.expected_version,
            command.idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{request_id}/cancel", response_model=LifecycleCommandResult)
def cancel_request(
    request_id: UUID,
    command: CancelRequestCommand,
    service: LifecycleServiceDependency,
) -> LifecycleCommandResult:
    try:
        return service.cancel_draft(
            request_id,
            command.user_id,
            command.expected_version,
            command.idempotency_key,
            command.reason,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LifecycleOwnershipError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LifecycleRequestNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RequestNotReadyError):
        detail = {"message": str(exc)}
        if exc.confirmation_view is not None:
            detail["confirmation_view"] = exc.confirmation_view.model_dump(mode="json")
        return HTTPException(status_code=409, detail=detail)
    if isinstance(
        exc,
        (
            LifecycleConcurrentUpdateError,
            LifecycleIdempotencyConflictError,
            LifecycleTransitionError,
            RequestAlreadyCancelledError,
            RequestAlreadyRegisteredError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, LifecyclePersistenceError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail="Request lifecycle operation failed")
