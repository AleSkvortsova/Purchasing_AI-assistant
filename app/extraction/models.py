from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rules.models import ApprovalContext, ApprovalRouteResult

ExtractionStatus = Literal[
    "extracted",
    "needs_clarification",
    "conflict",
    "failed",
]
AmountType = Literal["exact", "approximate", "maximum", "range"]


class RawApprovalExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_raw: str | None = None
    budget_status_raw: str | None = None
    urgency_raw: str | None = None
    single_supplier_raw: bool | None = None
    category_raw: str | None = None
    has_data_access_raw: bool | None = None
    work_on_site_raw: bool | None = None
    urgency_claimed: bool = False
    confidence_by_field: dict[str, float] = Field(default_factory=dict)
    evidence_by_field: dict[str, str] = Field(default_factory=dict)
    unknown_fields: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)

    @field_validator("confidence_by_field")
    @classmethod
    def validate_confidence(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        if any(score < 0 or score > 1 for score in value.values()):
            raise ValueError("confidence values must be between 0 and 1")
        return value


class MoneyExtraction(BaseModel):
    amount: Decimal | None = Field(default=None, ge=0)
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    amount_type: AmountType
    currency: str | None = None
    evidence: str


class NormalizedApprovalExtraction(BaseModel):
    amount: Decimal | None = Field(default=None, ge=0)
    budget_status: Literal["budgeted", "unbudgeted"] | None = None
    urgency: Literal["P1", "P2", "P3", "P4"] | None = None
    urgency_claimed: bool = False
    single_supplier: bool = False
    category_code: str | None = None
    has_data_access: bool = False
    work_on_site: bool = False
    confidence_by_field: dict[str, float] = Field(default_factory=dict)
    evidence_by_field: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_text: str
    money: MoneyExtraction | None = None


class ApprovalExtractionResult(BaseModel):
    status: ExtractionStatus
    extraction: NormalizedApprovalExtraction
    approval_context: ApprovalContext | None = None
    clarification_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provider_metadata: dict = Field(default_factory=dict)
    duration_ms: int = Field(ge=0)


class ApprovalEvaluationResult(BaseModel):
    extraction_result: ApprovalExtractionResult
    approval_route_result: ApprovalRouteResult | None = None


class ApprovalExtractionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized
