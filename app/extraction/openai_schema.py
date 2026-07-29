from typing import Any, Literal

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.extraction.models import RawApprovalExtraction

ExtractionFieldName = Literal[
    "amount",
    "budget_status",
    "urgency",
    "single_supplier",
    "category",
    "has_data_access",
    "work_on_site",
    "procurement_type",
    "item_name",
    "quantity",
    "unit",
    "specifications",
    "desired_result",
    "amount_modifier",
    "billing_period",
    "desired_delivery_date",
    "delivery_location",
    "business_justification",
    "department",
    "contact_person",
]


class FieldConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: ExtractionFieldName
    confidence: float = Field(ge=0, le=1)


class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: ExtractionFieldName
    evidence: str


class OpenAIApprovalExtractionPayload(BaseModel):
    """Strict transport DTO used only by OpenAI Structured Outputs."""

    model_config = ConfigDict(extra="forbid")

    amount_raw: str | None
    budget_status_raw: str | None
    urgency_raw: str | None
    single_supplier_raw: bool | None
    category_raw: str | None
    has_data_access_raw: bool | None
    work_on_site_raw: bool | None
    procurement_type_raw: Literal["goods", "service"] | None
    item_name_raw: str | None
    quantity_raw: str | None
    unit_raw: str | None
    specifications_raw: str | None
    desired_result_raw: str | None
    amount_modifier_raw: Literal["exact", "maximum", "approximate"] | None
    billing_period_raw: Literal[
        "one_time", "per_month", "per_quarter", "per_year"
    ] | None
    desired_delivery_date_raw: str | None
    delivery_location_raw: str | None
    business_justification_raw: str | None
    department_raw: str | None
    contact_person_raw: str | None
    urgency_claimed: bool
    confidence_items: list[FieldConfidence]
    evidence_items: list[FieldEvidence]
    unknown_fields: list[str]
    contradictions: list[str]

    @model_validator(mode="after")
    def reject_duplicate_field_items(self):
        _ensure_unique_names(self.confidence_items, "confidence_items")
        _ensure_unique_names(self.evidence_items, "evidence_items")
        return self

    def to_raw_extraction(self) -> RawApprovalExtraction:
        return RawApprovalExtraction(
            amount_raw=self.amount_raw,
            budget_status_raw=self.budget_status_raw,
            urgency_raw=self.urgency_raw,
            single_supplier_raw=self.single_supplier_raw,
            category_raw=self.category_raw,
            has_data_access_raw=self.has_data_access_raw,
            work_on_site_raw=self.work_on_site_raw,
            procurement_type_raw=self.procurement_type_raw,
            item_name_raw=self.item_name_raw,
            quantity_raw=self.quantity_raw,
            unit_raw=self.unit_raw,
            specifications_raw=self.specifications_raw,
            desired_result_raw=self.desired_result_raw,
            amount_modifier_raw=self.amount_modifier_raw,
            billing_period_raw=self.billing_period_raw,
            desired_delivery_date_raw=self.desired_delivery_date_raw,
            delivery_location_raw=self.delivery_location_raw,
            business_justification_raw=self.business_justification_raw,
            department_raw=self.department_raw,
            contact_person_raw=self.contact_person_raw,
            urgency_claimed=self.urgency_claimed,
            confidence_by_field={
                item.field_name: item.confidence
                for item in self.confidence_items
            },
            evidence_by_field={
                item.field_name: item.evidence for item in self.evidence_items
            },
            unknown_fields=self.unknown_fields,
            contradictions=self.contradictions,
        )


def approval_extraction_strict_json_schema() -> dict[str, Any]:
    return to_strict_json_schema(OpenAIApprovalExtractionPayload)


def validate_approval_extraction_schema(
    schema: dict[str, Any] | None = None,
) -> list[str]:
    candidate = (
        approval_extraction_strict_json_schema()
        if schema is None
        else schema
    )
    errors: list[str] = []
    if candidate.get("type") != "object":
        errors.append("root type must be object")
    if "anyOf" in candidate:
        errors.append("root must not use anyOf")
    _validate_schema_node(candidate, "$", errors)
    return errors


def _ensure_unique_names(items: list, collection_name: str) -> None:
    names = [item.field_name for item in items]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"duplicate field_name in {collection_name}: "
            + ", ".join(duplicates)
        )


def _validate_schema_node(node: object, path: str, errors: list[str]) -> None:
    if isinstance(node, list):
        for index, item in enumerate(node):
            _validate_schema_node(item, f"{path}[{index}]", errors)
        return
    if not isinstance(node, dict):
        return
    if "default" in node:
        errors.append(f"{path}: default is not allowed")
    if node.get("type") == "object":
        additional = node.get("additionalProperties")
        if additional is not False:
            errors.append(f"{path}: object must set additionalProperties=false")
        if isinstance(additional, dict):
            errors.append(f"{path}: typed additionalProperties is not allowed")
        properties = node.get("properties", {})
        required = node.get("required", [])
        if isinstance(properties, dict) and set(properties) != set(required):
            errors.append(f"{path}: every property must be required")
    for key, value in node.items():
        _validate_schema_node(value, f"{path}.{key}", errors)
