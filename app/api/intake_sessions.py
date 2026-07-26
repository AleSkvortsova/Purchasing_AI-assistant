from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.dependencies import (
    get_intake_persistence_orchestrator,
    get_optional_intake_persistence_orchestrator,
)
from app.intake.models import IntakeFieldUpdate
from app.intake_persistence.exceptions import (
    ActiveDraftNotFoundError,
    ConcurrentIntakeUpdateError,
    DialogStateCorruptedError,
    IdempotencyConflictError,
    IntakePersistenceMappingError,
    IntakePersistenceRepositoryError,
    MultipleActiveDraftsError,
    PersistencePartialFailureError,
    RequestNotEditableError,
    RequestOwnershipError,
    UnsupportedIntakeSchemaVersionError,
)
from app.intake_persistence.models import (
    MessageEnvelope,
    PersistentIntakeStepResult,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator

router = APIRouter(prefix="/intake-sessions", tags=["intake-persistence"])
OrchestratorDependency = Annotated[
    PersistentIntakeOrchestrator,
    Depends(get_intake_persistence_orchestrator),
]
OptionalOrchestratorDependency = Annotated[
    PersistentIntakeOrchestrator | None,
    Depends(get_optional_intake_persistence_orchestrator),
]


class PersistentIntakeStepRequest(BaseModel):
    user_id: UUID
    request_id: UUID | None = None
    idempotency_key: str | None = None
    update: IntakeFieldUpdate
    incoming_message: MessageEnvelope | None = None


@router.get("/health")
def intake_persistence_health(
    orchestrator: OptionalOrchestratorDependency,
) -> dict[str, object]:
    if orchestrator is None:
        return {
            "status": "not_configured",
            "configured": False,
            "migration_required": "007_intake_persistence_orchestration.sql",
        }
    try:
        orchestrator.repository.health_check()
    except Exception:
        return {"status": "error", "configured": True}
    return {"status": "ok", "configured": True}


@router.post("/step", response_model=PersistentIntakeStepResult)
def process_persistent_step(
    payload: PersistentIntakeStepRequest,
    orchestrator: OrchestratorDependency,
) -> PersistentIntakeStepResult:
    try:
        result = orchestrator.process_structured_step(
            payload.user_id,
            payload.update,
            payload.request_id,
            payload.incoming_message,
            payload.idempotency_key,
        )
        if result.persistence_status == "partial_failure":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "Intake step was not fully persisted",
                    "request_id": str(result.request_id),
                    "recovery_required": True,
                },
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{user_id}/active", response_model=PersistentIntakeStepResult)
def get_active_intake_session(
    user_id: UUID,
    orchestrator: OrchestratorDependency,
) -> PersistentIntakeStepResult:
    try:
        return orchestrator.get_active_session(user_id)
    except Exception as exc:
        raise _http_error(exc) from exc


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ActiveDraftNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RequestOwnershipError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, MultipleActiveDraftsError):
        return HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "request_ids": exc.request_ids,
            },
        )
    if isinstance(
        exc,
        (
            ConcurrentIntakeUpdateError,
            IdempotencyConflictError,
            RequestNotEditableError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(
        exc,
        (
            IntakePersistenceMappingError,
            UnsupportedIntakeSchemaVersionError,
            DialogStateCorruptedError,
            ValueError,
        ),
    ):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(
        exc,
        (IntakePersistenceRepositoryError, PersistencePartialFailureError),
    ):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail="Intake persistence failed")
