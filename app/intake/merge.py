import re
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
        resolved_change = self._resolve_conflict(result, update)
        if resolved_change is not None:
            changes.append(resolved_change)
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
            value, is_enrichment = _enrich_text_value(
                code,
                current,
                value,
                update,
            )
            if current == value:
                if update.source == UpdateSource.USER and (
                    state is None or not state.confirmed
                ):
                    result.field_states[code] = FieldValueState(
                        field_code=code,
                        value=value,
                        source=UpdateSource.USER,
                        evidence=update.evidence_by_field.get(code),
                        confirmed=True,
                        previous_value=(
                            state.previous_value if state is not None else None
                        ),
                    )
                    result.conflicts = [
                        item for item in result.conflicts if item.field_code != code
                    ]
                continue
            if (
                current is not None
                and update.explicit_correction
                and not definition.allows_explicit_correction
            ):
                invalid[code] = "Поле нельзя исправить через intake update"
                continue
            if (
                current is not None
                and not update.explicit_correction
                and not is_enrichment
            ):
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
                    update.source == UpdateSource.USER or update.explicit_correction
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

    def _resolve_conflict(
        self,
        draft: RequestDraftData,
        update: IntakeFieldUpdate,
    ) -> AppliedChange | None:
        if not update.resolve_conflict_id or not update.conflict_resolution:
            return None
        conflict = next(
            (
                item
                for item in draft.conflicts
                if item.id == update.resolve_conflict_id
            ),
            None,
        )
        if conflict is None:
            return None
        draft.conflicts = [
            item for item in draft.conflicts if item.id != conflict.id
        ]
        state = draft.field_states.get(conflict.field_code)
        if update.conflict_resolution == "keep":
            if state is not None:
                draft.field_states[conflict.field_code] = state.model_copy(
                    update={"confirmed": True}
                )
            else:
                current = getattr(draft, conflict.field_code)
                draft.field_states[conflict.field_code] = FieldValueState(
                    field_code=conflict.field_code,
                    value=current,
                    source=UpdateSource.USER,
                    confirmed=True,
                )
            return None
        if self.registry.get(conflict.field_code) is None:
            return None
        value = self.validator.normalize(
            conflict.field_code, conflict.proposed_value
        )
        previous = getattr(draft, conflict.field_code)
        setattr(draft, conflict.field_code, value)
        draft.field_states[conflict.field_code] = FieldValueState(
            field_code=conflict.field_code,
            value=value,
            source=UpdateSource.USER,
            confirmed=True,
            previous_value=previous,
        )
        return AppliedChange(
            field_code=conflict.field_code,
            previous_value=previous,
            value=value,
            source=UpdateSource.USER,
        )

    @staticmethod
    def _can_replace(state: FieldValueState | None, new_source: UpdateSource) -> bool:
        if state is None:
            return False
        if state.confirmed:
            return False
        if (
            new_source == UpdateSource.EXTRACTION
            and state.source == UpdateSource.SYSTEM
        ):
            return False
        return SOURCE_PRIORITY[new_source] > SOURCE_PRIORITY[state.source]


_ENRICHABLE_TEXT_FIELDS = {
    "specifications",
    "desired_result",
    "delivery_location",
}


def _enrich_text_value(
    code: str,
    current: object,
    value: object,
    update: IntakeFieldUpdate,
) -> tuple[object, bool]:
    if (
        code not in _ENRICHABLE_TEXT_FIELDS
        or code == update.answered_field_code
        or update.source != UpdateSource.USER
        or not isinstance(current, str)
        or not isinstance(value, str)
    ):
        return value, False
    current_normalized = _normalize_text(current)
    value_normalized = _normalize_text(value)
    if not value_normalized or value_normalized == current_normalized:
        return current, True
    if value_normalized in current_normalized:
        return current, True
    if current_normalized in value_normalized:
        return value, True
    if code == "delivery_location":
        return value, False
    return f"{current.rstrip(' .;')}; {value.lstrip(' .;')}", True


def _normalize_text(value: str) -> str:
    return " ".join(
        re.findall(r"[a-zа-я0-9]+", value.casefold().replace("ё", "е"))
    )
