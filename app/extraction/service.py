from time import perf_counter

from app.extraction.exceptions import (
    ApprovalExtractionProviderError,
    ApprovalExtractionValidationError,
)
from app.extraction.models import (
    ApprovalEvaluationResult,
    ApprovalExtractionResult,
    NormalizedApprovalExtraction,
)
from app.extraction.postprocessing import ApprovalExtractionPostProcessor
from app.extraction.provider import ApprovalExtractionProvider
from app.rules.service import ApprovalRuleService


class ApprovalContextExtractionService:
    def __init__(
        self,
        provider: ApprovalExtractionProvider,
        *,
        min_confidence: float = 0.70,
    ) -> None:
        self._provider = provider
        self._post_processor = ApprovalExtractionPostProcessor(
            min_confidence=min_confidence
        )

    def extract(self, text: str) -> ApprovalExtractionResult:
        started = perf_counter()
        source_text = text.strip()
        if not source_text:
            raise ApprovalExtractionValidationError(
                "Approval context text must not be blank"
            )
        try:
            raw = self._provider.extract(source_text)
            processed = self._post_processor.process(source_text, raw)
        except ApprovalExtractionProviderError:
            raise
        except Exception as exc:
            extraction = NormalizedApprovalExtraction(
                source_text=source_text,
                contradictions=[str(exc)],
            )
            return ApprovalExtractionResult(
                status="failed",
                extraction=extraction,
                warnings=["Не удалось нормализовать извлечённые данные"],
                provider_metadata=self._metadata(),
                duration_ms=_duration_ms(started),
            )

        return ApprovalExtractionResult(
            status=processed.status,
            extraction=processed.extraction,
            approval_context=processed.approval_context,
            clarification_questions=processed.clarification_questions,
            warnings=processed.extraction.warnings,
            provider_metadata=self._metadata(),
            duration_ms=_duration_ms(started),
        )

    def _metadata(self) -> dict:
        metadata = getattr(self._provider, "last_metadata", {})
        return dict(metadata) if isinstance(metadata, dict) else {}


class ApprovalEvaluationOrchestrator:
    def __init__(
        self,
        extraction_service: ApprovalContextExtractionService,
        approval_service: ApprovalRuleService | None,
    ) -> None:
        self._extraction_service = extraction_service
        self._approval_service = approval_service

    def extract_and_evaluate(self, text: str) -> ApprovalEvaluationResult:
        extraction_result = self._extraction_service.extract(text)
        route_result = None
        if (
            extraction_result.status == "extracted"
            and extraction_result.approval_context is not None
            and self._approval_service is not None
        ):
            route_result = self._approval_service.evaluate(
                extraction_result.approval_context
            )
        return ApprovalEvaluationResult(
            extraction_result=extraction_result,
            approval_route_result=route_result,
        )


def _duration_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
