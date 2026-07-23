from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    field_validator,
    model_validator,
)

BudgetStatus = Literal["budgeted", "unbudgeted"]
ConditionType = Literal[
    "urgency",
    "single_supplier",
    "category",
    "data_access",
    "work_on_site",
]
RouteStatus = Literal[
    "resolved",
    "needs_clarification",
    "no_matching_rule",
    "conflict",
]
RuleKind = Literal["base", "additional"]


def _reject_float(value):
    if isinstance(value, float):
        raise ValueError("amount must be passed as a decimal string or integer")
    return value


StrictDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]


class ApprovalContext(BaseModel):
    amount: StrictDecimal = Field(ge=0)
    budget_status: BudgetStatus | None = None
    urgency: Literal["P1", "P2", "P3", "P4"] | None = None
    single_supplier: bool = False
    category_code: str | None = None
    has_data_access: bool = False
    work_on_site: bool = False
    evaluation_date: date | None = None

    @field_validator("urgency", mode="before")
    @classmethod
    def normalize_urgency(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("category_code", mode="before")
    @classmethod
    def normalize_category(cls, value):
        if value is None:
            return None
        normalized = str(value).strip().upper()
        return normalized or None


class ApprovalRule(BaseModel):
    id: UUID | None = None
    rule_kind: RuleKind
    rule_code: str
    budget_status: BudgetStatus | None = None
    condition_type: ConditionType | None = None
    condition_value: str | None = None
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    approvers: list[str] = Field(min_length=1)
    priority: int = Field(default=100, gt=0)
    is_active: bool = True
    effective_from: date
    effective_to: date | None = None
    source_document_id: str
    source_section: str
    description: str | None = None

    @model_validator(mode="after")
    def validate_rule(self) -> "ApprovalRule":
        if self.max_amount is not None and self.min_amount is not None:
            if self.max_amount < self.min_amount:
                raise ValueError("max_amount must be greater than min_amount")
        if self.effective_to is not None:
            if self.effective_to < self.effective_from:
                raise ValueError("effective_to must not precede effective_from")
        if len(self.approvers) != len(dict.fromkeys(self.approvers)):
            raise ValueError("approvers must not contain duplicates")
        if any(not approver.strip() for approver in self.approvers):
            raise ValueError("approvers must contain non-empty strings")
        if self.rule_kind == "base" and self.budget_status is None:
            raise ValueError("base rule requires budget_status")
        if self.rule_kind == "additional":
            if self.condition_type is None or self.condition_value is None:
                raise ValueError("additional rule requires a condition")
        return self


class SourceReference(BaseModel):
    document_id: str
    section: str
    rule_code: str


class ApprovalRouteResult(BaseModel):
    status: RouteStatus
    base_rule_code: str | None = None
    applied_additional_rule_codes: list[str] = Field(default_factory=list)
    base_approvers: list[str] = Field(default_factory=list)
    additional_approvers: list[str] = Field(default_factory=list)
    final_approvers: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApprovalRuleStatistics(BaseModel):
    base_rules_count: int = 0
    additional_rules_count: int = 0
    active_rules_count: int = 0


class ApprovalRulesHealth(BaseModel):
    configured: bool
    database_configured: bool
    base_rules_count: int = 0
    additional_rules_count: int = 0
    active_rules_count: int = 0
