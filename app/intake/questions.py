from app.intake.field_registry import RequestFieldRegistry
from app.intake.models import (
    CompletenessResult,
    NextQuestion,
    RequestDraftData,
)


class NextQuestionSelector:
    def __init__(self, registry: RequestFieldRegistry | None = None) -> None:
        self.registry = registry or RequestFieldRegistry()

    def select(
        self,
        draft: RequestDraftData,
        completeness: CompletenessResult,
        clarification_by_field: dict[str, str] | None = None,
    ) -> NextQuestion | None:
        if draft.conflicts:
            conflict = draft.conflicts[0]
            definition = self.registry.get(conflict.field_code)
            return NextQuestion(
                field_code=conflict.field_code,
                text=conflict.message,
                question_type="confirmation",
                options=["Подтвердить изменение", "Оставить прежнее значение"],
                reason="Требуется подтвердить изменение значения",
                priority=0,
                related_conflict_id=conflict.id,
            )
        for field_code, text in (clarification_by_field or {}).items():
            definition = self.registry.get(field_code)
            if definition is not None:
                return NextQuestion(
                    field_code=field_code,
                    text=text,
                    question_type=definition.question_type,
                    options=list(definition.options),
                    reason="Требуется уточнить извлечённое значение",
                    priority=1,
                )
        candidates = [
            code
            for code in [
                *completeness.missing_fields,
                *completeness.invalid_fields,
            ]
            if self.registry.get(code) is not None
        ]
        if not candidates:
            return None
        definitions = [self.registry.get(code) for code in candidates]
        definition = min(
            (item for item in definitions if item is not None),
            key=lambda item: (item.priority, item.display_order),
        )
        text = (clarification_by_field or {}).get(
            definition.code,
            definition.clarification_question
            if definition.code in completeness.invalid_fields
            else definition.question,
        )
        return NextQuestion(
            field_code=definition.code,
            text=text,
            question_type=definition.question_type,
            options=list(definition.options),
            reason=completeness.reasons_by_field.get(
                definition.code, "Поле требуется для готовности заявки"
            ),
            priority=definition.priority,
        )
