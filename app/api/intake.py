from fastapi import APIRouter
from pydantic import BaseModel

from app.extraction.models import ApprovalExtractionResult
from app.intake.models import IntakeFieldUpdate, IntakeStepResult, RequestDraftData
from app.intake.service import RequestIntakeService

router = APIRouter(prefix="/intake", tags=["intake"])


class IntakeStepRequest(BaseModel):
    draft: RequestDraftData
    update: IntakeFieldUpdate
    approval_extraction_result: ApprovalExtractionResult | None = None


@router.get("/health")
def intake_health() -> dict[str, str]:
    return {"status": "ok", "mode": "deterministic"}


@router.post("/evaluate-step", response_model=IntakeStepResult)
def evaluate_intake_step(payload: IntakeStepRequest) -> IntakeStepResult:
    return RequestIntakeService().process_step(
        payload.draft,
        payload.update,
        payload.approval_extraction_result,
    )
