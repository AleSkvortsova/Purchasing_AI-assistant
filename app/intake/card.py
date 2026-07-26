from decimal import Decimal
from typing import Any

from app.intake.field_registry import CATEGORY_NAMES, RequestFieldRegistry
from app.intake.models import (
    CardField,
    CardSection,
    RequestCard,
    RequestDraftData,
)
from app.rules.models import ApprovalRouteResult

SECTION_ORDER = (
    "Потребность",
    "Финансовые данные",
    "Сроки и доставка",
    "Обоснования",
    "Согласование",
)

SECTION_BY_FIELD = {
    "amount": "Финансовые данные",
    "currency": "Финансовые данные",
    "budget_status": "Финансовые данные",
    "desired_delivery_date": "Сроки и доставка",
    "delivery_location": "Сроки и доставка",
    "business_justification": "Обоснования",
    "brand_justification": "Обоснования",
    "single_supplier_justification": "Обоснования",
    "urgency_justification": "Обоснования",
}


class RequestCardBuilder:
    def __init__(self, registry: RequestFieldRegistry | None = None) -> None:
        self.registry = registry or RequestFieldRegistry()

    def build(
        self,
        draft: RequestDraftData,
        approval_route: ApprovalRouteResult | None = None,
    ) -> RequestCard:
        sections: dict[str, list[CardField]] = {name: [] for name in SECTION_ORDER}
        for definition in sorted(
            self.registry.all(), key=lambda item: item.display_order
        ):
            if definition.code in {"request_id", "requester_id", "currency"}:
                continue
            value = getattr(draft, definition.code)
            if value is None:
                continue
            section = SECTION_BY_FIELD.get(definition.code, definition.card_section)
            sections[section].append(
                CardField(
                    code=definition.code,
                    label=definition.label,
                    display_value=_display(definition.code, value),
                )
            )
        if approval_route is not None:
            for approver in dict.fromkeys(approval_route.final_approvers):
                sections["Согласование"].append(
                    CardField(
                        code="approver",
                        label="Согласующий",
                        display_value=approver,
                    )
                )
        optional = [
            item.code
            for item in self.registry.applicable(draft)
            if not self.registry.is_required(item, draft)
            and getattr(draft, item.code) is None
        ]
        return RequestCard(
            title=(
                draft.title
                or draft.item_name
                or CATEGORY_NAMES.get(draft.category_code or "")
                or "Заявка на закупку"
            ),
            sections=[
                CardSection(title=name, fields=fields)
                for name, fields in sections.items()
                if fields
            ],
            approval_route=approval_route,
            warnings=draft.warnings,
            unresolved_optional_fields=optional,
        )


def _display(code: str, value: Any) -> str:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if code == "amount" and isinstance(value, Decimal):
        formatted = f"{value:,.2f}".replace(",", " ")
        formatted = formatted.rstrip("0").rstrip(".")
        return f"{formatted} ₽"
    if code == "category_code":
        return (
            f"{value} — {CATEGORY_NAMES[value]}"
            if value in CATEGORY_NAMES
            else str(value)
        )
    return str(value)
