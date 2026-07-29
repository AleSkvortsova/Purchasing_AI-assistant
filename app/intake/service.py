from app.extraction.models import ApprovalExtractionResult
from app.intake.card import RequestCardBuilder
from app.intake.completeness import RequestCompletenessService
from app.intake.field_registry import RequestFieldRegistry
from app.intake.merge import RequestMergeService
from app.intake.models import (
    FieldConflict,
    IntakeFieldUpdate,
    IntakeStatus,
    IntakeStepResult,
    RequestDraftData,
    UpdateSource,
)
from app.intake.questions import NextQuestionSelector
from app.rules.models import ApprovalContext
from app.rules.service import ApprovalRuleService


class RequestIntakeService:
    def __init__(
        self,
        approval_rule_service: ApprovalRuleService | None = None,
        registry: RequestFieldRegistry | None = None,
    ) -> None:
        self.registry = registry or RequestFieldRegistry()
        self.merge_service = RequestMergeService(self.registry)
        self.completeness_service = RequestCompletenessService(self.registry)
        self.question_selector = NextQuestionSelector(self.registry)
        self.card_builder = RequestCardBuilder(self.registry)
        self.approval_rule_service = approval_rule_service

    def process_step(
        self,
        draft: RequestDraftData,
        update: IntakeFieldUpdate,
        approval_extraction_result: ApprovalExtractionResult | None = None,
    ) -> IntakeStepResult:
        extraction_update, clarifications = self._extraction_update(
            approval_extraction_result
        )
        first = self.merge_service.merge(draft, extraction_update)
        merged = self.merge_service.merge(first.draft, update)
        current = merged.draft
        invalid = {**first.invalid_fields, **merged.invalid_fields}
        if approval_extraction_result is not None:
            current.warnings = list(
                dict.fromkeys(
                    [
                        *current.warnings,
                        *approval_extraction_result.warnings,
                        *approval_extraction_result.extraction.warnings,
                    ]
                )
            )
            for message in approval_extraction_result.extraction.contradictions:
                field = _contradiction_field(message)
                if not any(item.message == message for item in current.conflicts):
                    current.conflicts.append(
                        FieldConflict(
                            id=f"approval-{len(current.conflicts) + 1}",
                            field_code=field,
                            conflict_type="approval_extraction_conflict",
                            message=message,
                        )
                    )
        completeness = self.completeness_service.evaluate(current, invalid)
        if "quantity" in update.suppressed_extraction_fields:
            clarifications.setdefault(
                "quantity",
                "В сообщении указано несколько товарных позиций. Уточните "
                "количество для одной позиции или оформите разные позиции "
                "отдельными заявками.",
            )
        approval_context = self._approval_context(current)
        approval_route = (
            self.approval_rule_service.evaluate(approval_context)
            if approval_context is not None and self.approval_rule_service is not None
            else None
        )
        if current.conflicts:
            status = IntakeStatus.CONFLICT
        elif completeness.is_complete:
            status = IntakeStatus.READY_FOR_CONFIRMATION
        else:
            status = IntakeStatus.COLLECTING
        question = (
            None
            if status == IntakeStatus.READY_FOR_CONFIRMATION
            else self.question_selector.select(current, completeness, clarifications)
        )
        card = (
            self.card_builder.build(current, approval_route)
            if status == IntakeStatus.READY_FOR_CONFIRMATION
            else None
        )
        return IntakeStepResult(
            status=status,
            draft=current,
            completeness=completeness,
            applied_changes=[*first.applied_changes, *merged.applied_changes],
            conflicts=current.conflicts,
            warnings=current.warnings,
            next_question=question,
            request_card=card,
            approval_context=approval_context,
            approval_route=approval_route,
            metadata={"persistence_performed": False, "openai_called": False},
        )

    @staticmethod
    def _approval_context(draft: RequestDraftData) -> ApprovalContext | None:
        if (
            draft.amount is None
            or draft.budget_status is None
            or any(
                item.field_code in {"amount", "budget_status"}
                for item in draft.conflicts
            )
        ):
            return None
        return ApprovalContext(
            amount=draft.amount,
            budget_status=draft.budget_status,
            urgency=draft.urgency,
            single_supplier=draft.single_supplier is True,
            category_code=draft.category_code,
            has_data_access=draft.has_data_access is True,
            work_on_site=draft.work_on_site is True,
        )

    @staticmethod
    def _extraction_update(
        result: ApprovalExtractionResult | None,
    ) -> tuple[IntakeFieldUpdate, dict[str, str]]:
        if result is None:
            return IntakeFieldUpdate(source=UpdateSource.EXTRACTION), {}
        extraction = result.extraction
        values = {
            key: value
            for key, value in {
                "amount": extraction.amount,
                "budget_status": extraction.budget_status,
                "urgency": extraction.urgency,
                "single_supplier": extraction.single_supplier,
                "category_code": extraction.category_code,
                "has_data_access": extraction.has_data_access,
                "work_on_site": extraction.work_on_site,
            }.items()
            if value is not None
        }
        clarifications: dict[str, str] = {}
        if extraction.money and extraction.money.amount_type == "range":
            values.pop("amount", None)
            if result.clarification_questions:
                clarifications["amount"] = result.clarification_questions[0]
        return (
            IntakeFieldUpdate(
                values=values,
                source=UpdateSource.EXTRACTION,
                evidence_by_field=extraction.evidence_by_field,
            ),
            clarifications,
        )


def _contradiction_field(message: str) -> str:
    normalized = message.casefold()
    if "бюджет" in normalized:
        return "budget_status"
    if "сумм" in normalized:
        return "amount"
    return "comments"
