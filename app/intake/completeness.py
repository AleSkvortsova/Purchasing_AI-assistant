from decimal import Decimal

from app.intake.field_registry import RequestFieldRegistry
from app.intake.models import CompletenessResult, RequestDraftData
from app.intake.validators import IntakeFieldValidator


class RequestCompletenessService:
    def __init__(
        self,
        registry: RequestFieldRegistry | None = None,
        validator: IntakeFieldValidator | None = None,
    ) -> None:
        self.registry = registry or RequestFieldRegistry()
        self.validator = validator or IntakeFieldValidator(self.registry)

    def evaluate(
        self,
        draft: RequestDraftData,
        extra_invalid: dict[str, str] | None = None,
    ) -> CompletenessResult:
        invalid = {**self.validator.validate_draft(draft), **(extra_invalid or {})}
        definitions = sorted(
            self.registry.applicable(draft), key=lambda item: item.display_order
        )
        required = [
            item for item in definitions if self.registry.is_required(item, draft)
        ]
        completed: list[str] = []
        missing: list[str] = []
        blocked: list[str] = []
        reasons: dict[str, str] = dict(invalid)
        for item in required:
            unmet = [dep for dep in item.dependencies if getattr(draft, dep) is None]
            if unmet:
                blocked.append(item.code)
                reasons[item.code] = f"Сначала заполните: {', '.join(unmet)}"
            elif _has_value(getattr(draft, item.code)):
                if item.code not in invalid:
                    completed.append(item.code)
            else:
                missing.append(item.code)
                reasons[item.code] = f"Обязательное поле: {item.label}"
        required_codes = [item.code for item in required]
        ratio = (
            Decimal(len(completed)) / Decimal(len(required_codes))
            if required_codes
            else Decimal("1")
        )
        invalid_codes = [item.code for item in definitions if item.code in invalid] + [
            code for code in invalid if self.registry.get(code) is None
        ]
        return CompletenessResult(
            is_complete=not missing and not invalid_codes and not blocked,
            required_fields=required_codes,
            completed_fields=completed,
            missing_fields=missing,
            invalid_fields=invalid_codes,
            blocked_fields=blocked,
            completion_ratio=ratio.quantize(Decimal("0.01")),
            reasons_by_field=reasons,
        )


def _has_value(value: object) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))
