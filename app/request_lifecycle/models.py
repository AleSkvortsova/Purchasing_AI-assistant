from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.intake.models import (
    CompletenessResult,
    IntakeStatus,
    RequestCard,
)
from app.rules.models import ApprovalRouteResult
from app.schemas.common import RequestStatus, RequestType


class LifecycleCommandType(StrEnum):
    CONFIRM = "confirm"
    RETURN_TO_EDITING = "return_to_editing"
    CANCEL = "cancel"


class ConfirmationView(BaseModel):
    request_id: UUID
    request_version: int = Field(ge=1)
    request_status: RequestStatus
    intake_status: IntakeStatus
    request_card: RequestCard | None = None
    approval_route: ApprovalRouteResult | None = None
    warnings: list[str] = Field(default_factory=list)
    editable: bool
    confirmable: bool
    blocking_reasons: list[str] = Field(default_factory=list)
    updated_at: datetime


class LifecycleCommandResult(BaseModel):
    request_id: UUID
    user_id: UUID
    request_number: str | None = None
    status: RequestStatus
    intake_status: IntakeStatus
    version: int = Field(ge=1)
    registered_at: datetime | None = None
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    replayed: bool = False
    request_card: RequestCard | None = None
    approval_route: ApprovalRouteResult | None = None
    editable: bool = False
    editable_field_codes: list[str] = Field(default_factory=list)
    instruction: str | None = None
    warnings: list[str] = Field(default_factory=list)


class LifecycleIdempotencyRecord(BaseModel):
    user_id: UUID
    request_id: UUID
    command_type: LifecycleCommandType
    key: str
    fingerprint: str
    result: LifecycleCommandResult


class LifecycleMutation(BaseModel):
    user_id: UUID
    request_id: UUID
    command_type: LifecycleCommandType
    expected_version: int = Field(ge=1)
    idempotency_key: str
    fingerprint: str
    request_data: dict[str, Any]
    request_type: RequestType | None = None
    category_code: str | None = None
    title: str | None = None
    intake_status: IntakeStatus
    request_card: RequestCard | None = None
    approval_route: ApprovalRouteResult | None = None
    completeness: CompletenessResult | None = None
    cancellation_reason: str | None = None
    editable_field_codes: list[str] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)


class SavedLifecycleMutation(BaseModel):
    result: LifecycleCommandResult
    replayed: bool = False


LifecycleAction = Literal["confirm", "return_to_editing", "cancel"]
