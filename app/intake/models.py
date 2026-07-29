from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rules.models import ApprovalContext, ApprovalRouteResult


class ProcurementType(StrEnum):
    GOODS = "goods"
    SERVICE = "service"


class IntakeStatus(StrEnum):
    COLLECTING = "collecting"
    CONFLICT = "conflict"
    READY_FOR_CONFIRMATION = "ready_for_confirmation"
    EDITING = "editing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class UpdateSource(StrEnum):
    USER = "user"
    EXTRACTION = "extraction"
    SYSTEM = "system"


class FieldValueState(BaseModel):
    field_code: str
    value: Any
    source: UpdateSource
    evidence: str | None = None
    confirmed: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    previous_value: Any = None
    conflict: bool = False


class FieldConflict(BaseModel):
    id: str
    field_code: str
    conflict_type: str = "value_change_requires_confirmation"
    current_value: Any = None
    proposed_value: Any = None
    message: str


class RequestDraftData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID | None = None
    requester_id: UUID | None = None
    department: str | None = None
    title: str | None = None
    description: str | None = None
    procurement_type: ProcurementType | None = None
    category_code: str | None = None
    item_name: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    specifications: str | None = None
    desired_result: str | None = None
    preferred_brand: str | None = None
    analogs_allowed: bool | None = None
    brand_justification: str | None = None
    amount: Decimal | None = None
    currency: str = "RUB"
    budget_status: Literal["budgeted", "unbudgeted", "unknown"] | None = None
    desired_delivery_date: date | None = None
    delivery_location: str | None = None
    business_justification: str | None = None
    single_supplier: bool | None = None
    supplier_name: str | None = None
    single_supplier_justification: str | None = None
    urgency: Literal["P1", "P2", "P3", "P4"] | None = None
    urgency_justification: str | None = None
    has_data_access: bool | None = None
    work_on_site: bool | None = None
    contact_person: str | None = None
    comments: str | None = None
    field_states: dict[str, FieldValueState] = Field(default_factory=dict)
    conflicts: list[FieldConflict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("category_code", "urgency", mode="before")
    @classmethod
    def uppercase_codes(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value


class FieldCorrection(BaseModel):
    operation: Literal["replace"] = "replace"
    target_field: str
    old_value: Any
    new_value: Any


class IntakeFieldUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    source: UpdateSource = UpdateSource.USER
    explicit_correction: bool = False
    evidence_by_field: dict[str, str] = Field(default_factory=dict)
    corrections: list[FieldCorrection] = Field(default_factory=list)
    suppressed_extraction_fields: list[str] = Field(default_factory=list)
    answered_field_code: str | None = None
    resolve_conflict_id: str | None = None
    conflict_resolution: Literal["accept", "keep"] | None = None
    message_id: str | None = None


class AppliedChange(BaseModel):
    field_code: str
    previous_value: Any = None
    value: Any
    source: UpdateSource


class CompletenessResult(BaseModel):
    is_complete: bool
    required_fields: list[str]
    completed_fields: list[str]
    missing_fields: list[str]
    invalid_fields: list[str]
    blocked_fields: list[str]
    completion_ratio: Decimal
    reasons_by_field: dict[str, str] = Field(default_factory=dict)


class NextQuestion(BaseModel):
    field_code: str
    text: str
    question_type: Literal[
        "free_text", "decimal", "date", "boolean", "choice", "confirmation"
    ]
    options: list[str] = Field(default_factory=list)
    reason: str
    priority: int
    related_conflict_id: str | None = None


class CardField(BaseModel):
    code: str
    label: str
    display_value: str
    metadata: dict[str, str] = Field(default_factory=dict)


class CardSection(BaseModel):
    title: str
    fields: list[CardField]


class RequestCard(BaseModel):
    title: str
    sections: list[CardSection]
    approval_route: ApprovalRouteResult | None = None
    warnings: list[str] = Field(default_factory=list)
    unresolved_optional_fields: list[str] = Field(default_factory=list)


class IntakeStepResult(BaseModel):
    status: IntakeStatus
    draft: RequestDraftData
    completeness: CompletenessResult
    applied_changes: list[AppliedChange] = Field(default_factory=list)
    conflicts: list[FieldConflict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_question: NextQuestion | None = None
    request_card: RequestCard | None = None
    approval_context: ApprovalContext | None = None
    approval_route: ApprovalRouteResult | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
