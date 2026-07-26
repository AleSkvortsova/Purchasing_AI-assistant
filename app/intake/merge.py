from dataclasses import dataclass, field
from uuid import uuid4

from app.intake.field_registry import RequestFieldRegistry
from app.intake.models import (
    AppliedChange,
    FieldConflict,
    FieldValueState,
    IntakeFieldUpdate,
    RequestDraftData,
    UpdateSource,
)
from app.intake.validators import IntakeFieldValidator

SOURCE_PRIORITY = {
    UpdateSource.SYSTEM: 0,
    UpdateSource.EXTRACTION: 1,
    UpdateSource.USER: 2,
}


@dataclass
class MergeResult:
    draft: RequestDraftData
    applied_changes: list[AppliedChange] = field(default_factory=list)
    invalid_fields: dict[str, str] = field(default_factory=dict)


class RequestMergeService:
    def __init__(
        self,
        registry: RequestFieldRegistry | None = None,
        validator: IntakeFieldValidator | None = None,
    ) -> None:
        self.registry = registry or RequestFieldRegistry()
        self.validator = validator or IntakeFieldValidator(self.registry)

    def merge(self, draft: RequestDraftData, update: IntakeFieldUpdate) -> MergeResult:
        result = draft.model_copy(deep=True)
        changes: list[AppliedChange] = []
        invalid: dict[str, str] = {}
        for code, raw_value in update.values.items():
            if raw_value is None:
                continue
            definition = self.registry.get(code)
            if definition is None:
                invalid[code] = f"Unknown intake field: {code}"
                continue
            try:
                value = self.validator.normalize(code, raw_value)
            except ValueError as exc:
                invalid[code] = str(exc)
                continue
            if value is None:
                continue
            current = getattr(result, code)
            state = result.field_states.get(code)
            if current == value:
                continue
            if (
                current is not None
                and update.explicit_correction
                and not definition.allows_explicit_correction
            ):
                invalid[code] = "Поле нельзя исправить через intake update"
                continue
            if current is not None and not update.explicit_correction:
                if not self._can_replace(state, update.source):
                    conflict = FieldConflict(
                        id=str(uuid4()),
                        field_code=code,
                        current_value=current,
                        proposed_value=value,
                        message=(
                            f"Подтвердите изменение поля «{definition.label}»: "
                            f"{current} → {value}."
                        ),
                    )
                    result.conflicts = [
                        item for item in result.conflicts if item.field_code != code
                    ] + [conflict]
                    continue
            setattr(result, code, value)
            result.conflicts = [
                item for item in result.conflicts if item.field_code != code
            ]
            result.field_states[code] = FieldValueState(
                field_code=code,
                value=value,
                source=update.source,
                evidence=update.evidence_by_field.get(code),
                confirmed=(
                    update.source in {UpdateSource.USER, UpdateSource.SYSTEM}
                    or update.explicit_correction
                ),
                previous_value=current,
            )
            changes.append(
                AppliedChange(
                    field_code=code,
                    previous_value=current,
                    value=value,
                    source=update.source,
                )
            )
        return MergeResult(result, changes, invalid)

    @staticmethod
    def _can_replace(state: FieldValueState | None, new_source: UpdateSource) -> bool:
        if state is None:
            return False
        if state.confirmed:
            return False
        return SOURCE_PRIORITY[new_source] > SOURCE_PRIORITY[state.source]
