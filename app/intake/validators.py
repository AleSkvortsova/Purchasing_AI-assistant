from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.intake.field_registry import RequestFieldRegistry
from app.intake.models import ProcurementType, RequestDraftData


class IntakeFieldValidator:
    def __init__(self, registry: RequestFieldRegistry | None = None) -> None:
        self.registry = registry or RequestFieldRegistry()

    def normalize(self, field_code: str, value: Any) -> Any:
        definition = self.registry.get(field_code)
        if definition is None:
            raise ValueError(f"Unknown intake field: {field_code}")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        name = definition.validator_name
        if name in {"amount", "quantity"}:
            if isinstance(value, float):
                raise ValueError("Use a decimal string or integer, not float")
            try:
                result = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("Некорректное десятичное число") from exc
            if name == "quantity" and result <= 0:
                raise ValueError("Количество должно быть больше нуля")
            if name == "amount" and result < 0:
                raise ValueError("Сумма не может быть отрицательной")
            return result
        if name == "boolean":
            if isinstance(value, bool):
                return value
            normalized = str(value).casefold()
            if normalized in {"true", "да", "yes", "1"}:
                return True
            if normalized in {"false", "нет", "no", "0"}:
                return False
            raise ValueError("Ожидается значение Да или Нет")
        if name == "date":
            try:
                result = value if isinstance(value, date) else date.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("Дата должна быть в формате YYYY-MM-DD") from exc
            if result < date.today():
                raise ValueError("Требуемая дата не может быть в прошлом")
            return result
        if name == "procurement_type":
            normalized = str(value).casefold()
            if normalized not in {"goods", "service", "work"}:
                raise ValueError("Неизвестный тип закупки")
            return ProcurementType(normalized)
        if name == "budget_status":
            normalized = str(value).casefold()
            if normalized not in {"budgeted", "unbudgeted"}:
                raise ValueError("Неизвестный бюджетный статус")
            return normalized
        if name == "urgency":
            normalized = str(value).upper()
            if normalized not in {"P1", "P2", "P3", "P4"}:
                raise ValueError("Приоритет должен быть P1, P2, P3 или P4")
            return normalized
        if name == "category":
            normalized = str(value).upper()
            return normalized.split(maxsplit=1)[0]
        return value

    def validate_draft(self, draft: RequestDraftData) -> dict[str, str]:
        errors: dict[str, str] = {}
        for item in self.registry.all():
            value = getattr(draft, item.code)
            if value is None:
                continue
            try:
                self.normalize(item.code, value)
            except ValueError as exc:
                errors[item.code] = str(exc)
        if (
            draft.preferred_brand
            and draft.analogs_allowed is False
            and not draft.brand_justification
        ):
            errors["brand_justification"] = (
                "При запрете аналогов требуется обоснование бренда"
            )
        if draft.single_supplier is True:
            if not draft.supplier_name:
                errors["supplier_name"] = "Укажите единственного поставщика"
            if not draft.single_supplier_justification:
                errors["single_supplier_justification"] = (
                    "Требуется обоснование единственного поставщика"
                )
        if draft.urgency in {"P1", "P2"} and not draft.urgency_justification:
            errors["urgency_justification"] = "Требуется обоснование срочности"
        return errors
