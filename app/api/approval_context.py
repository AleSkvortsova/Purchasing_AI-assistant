from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import (
    get_approval_evaluation_orchestrator,
    get_approval_extraction_service,
)
from app.core.config import get_settings
from app.extraction.exceptions import ApprovalExtractionProviderError
from app.extraction.models import (
    ApprovalEvaluationResult,
    ApprovalExtractionRequest,
    ApprovalExtractionResult,
)
from app.extraction.service import (
    ApprovalContextExtractionService,
    ApprovalEvaluationOrchestrator,
)

router = APIRouter(prefix="/approval-context", tags=["approval-context"])


@router.get("/health")
def approval_context_health() -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "provider": settings.approval_extraction_provider,
        "configured": settings.approval_extraction_configured,
        "openai_configured": settings.openai_configured,
    }


@router.post("/extract", response_model=ApprovalExtractionResult)
def extract_approval_context(
    request: ApprovalExtractionRequest,
    service: Annotated[
        ApprovalContextExtractionService,
        Depends(get_approval_extraction_service),
    ],
) -> ApprovalExtractionResult:
    try:
        return service.extract(request.text)
    except ApprovalExtractionProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post(
    "/extract-and-evaluate",
    response_model=ApprovalEvaluationResult,
)
def extract_and_evaluate_approval_context(
    request: ApprovalExtractionRequest,
    orchestrator: Annotated[
        ApprovalEvaluationOrchestrator,
        Depends(get_approval_evaluation_orchestrator),
    ],
) -> ApprovalEvaluationResult:
    try:
        return orchestrator.extract_and_evaluate(request.text)
    except ApprovalExtractionProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
