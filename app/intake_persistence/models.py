from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.intake.models import IntakeStatus, IntakeStepResult, NextQuestion


class ProcurementNeedCandidate(BaseModel):
    item_name: str
    procurement_type: Literal["goods", "service"]
    category_code: str | None = None
    category_label: str | None = None
    category_candidates: list[str] = Field(default_factory=list)
    action: str | None = None
    evidence: str | None = None
    relation: str = "separate_request"
    quantity: str | None = None


ProcurementItemCandidate = ProcurementNeedCandidate


class CategoryCandidateOption(BaseModel):
    code: str
    label: str
    source: Literal[
        "classifier_exact",
        "classifier_multiple",
        "derived",
        "llm_exact",
        "llm_candidates",
        "llm_confirmed",
        "persisted_strong",
        "generic_fallback",
    ] = "generic_fallback"
    selectable: bool = False
    readiness_eligible: bool = False


class IntakeConversationState(BaseModel):
    item_candidates: list[ProcurementNeedCandidate] = Field(default_factory=list)
    category_candidates: list[CategoryCandidateOption] = Field(default_factory=list)
    category_procurement_type: Literal["goods", "service"] | None = None
    category_subject_fingerprint: str | None = None
    category_decomposition_fingerprint: str | None = None
    decomposition_kind: Literal[
        "single_need",
        "multiple_goods",
        "multiple_services",
        "goods_plus_service",
        "ambiguous",
    ] | None = None
    decomposition_fingerprint: str | None = None
    category_step_id: str | None = None
    split_required: bool = False
    category_question_fingerprint: str | None = None
    category_clarification_repeats: int = Field(default=0, ge=0)
    category_clarification_kind: Literal["software_acquisition_scope"] | None = None
    original_description: str | None = None
    reason_code: (
        Literal[
            "category_candidates_missing",
            "repeated_category_clarification",
        "multi_category_split_required",
        "software_scope_clarification_required",
        ]
        | None
    ) = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.item_candidates
            or self.category_candidates
            or self.category_procurement_type
            or self.category_subject_fingerprint
            or self.category_decomposition_fingerprint
            or self.decomposition_kind
            or self.decomposition_fingerprint
            or self.split_required
            or self.category_step_id
            or self.category_question_fingerprint
            or self.category_clarification_repeats
            or self.category_clarification_kind
            or self.original_description
            or self.reason_code
        )


class MessageEnvelope(BaseModel):
    message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersistentDialogState(BaseModel):
    user_id: UUID
    request_id: UUID
    intake_status: IntakeStatus
    awaiting_field_code: str | None = None
    next_question: NextQuestion | None = None
    related_conflict_id: str | None = None
    state_version: int = Field(ge=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)
    intake_conversation: IntakeConversationState = Field(
        default_factory=IntakeConversationState
    )


class PersistenceMessageLog(BaseModel):
    user_id: UUID
    request_id: UUID
    direction: Literal["incoming", "outgoing"]
    message_type: Literal[
        "structured_update",
        "question",
        "conflict",
        "card",
        "system_error",
        "confirm_command",
        "return_to_editing_command",
        "cancel_command",
        "request_registered",
        "request_returned_to_editing",
        "request_cancelled",
        "lifecycle_conflict",
        "lifecycle_error",
    ]
    message_id: str | None = None
    idempotency_key: str | None = None
    idempotency_fingerprint: str | None = None
    field_code: str | None = None
    intake_status: IntakeStatus | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PersistentIntakeStepResult(BaseModel):
    request_id: UUID
    user_id: UUID
    request_version: int = Field(ge=1)
    created_new_request: bool = False
    replayed: bool = False
    intake_result: IntakeStepResult
    dialog_state: PersistentDialogState
    persistence_status: Literal["saved", "replayed", "partial_failure"]
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IdempotencyRecord(BaseModel):
    user_id: UUID
    key: str
    fingerprint: str
    result: PersistentIntakeStepResult


class SaveIntakeStepCommand(BaseModel):
    request_id: UUID
    expected_version: int
    request_type: Literal["product", "service"] | None = None
    category_code: str | None = None
    title: str | None = None
    request_data: dict[str, Any]
    dialog_state: PersistentDialogState
    incoming_log: PersistenceMessageLog
    outgoing_log: PersistenceMessageLog
    idempotency_record: IdempotencyRecord | None = None


class SavedIntakeStep(BaseModel):
    request_version: int
    dialog_state: PersistentDialogState
    replayed: bool = False
