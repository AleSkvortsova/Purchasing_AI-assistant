import logging
import re
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from app.bot.categories import (
    DeterministicCategoryClassifier,
    classify_software_procurement_scope,
    extract_procurement_items,
    normalize_software_scope_reply,
)
from app.bot.category_resolution import (
    CategoryResolution,
    CategoryResolutionService,
    build_category_resolution_context,
    category_confirmation_evidence,
    category_draft_context_fingerprint,
    category_subject_fingerprint,
)
from app.bot.decomposition import decompose_procurement_needs
from app.bot.dialog_modes import (
    DialogMode,
    DialogModePersistenceError,
    DialogModeRepository,
    DialogReplayConflictError,
    InMemoryDialogModeRepository,
)
from app.bot.formatters import (
    ACTIVE_DRAFT_NOTICE,
    INSTRUCTION_TEXT,
    NEW_REQUEST_PROMPT,
    REGULATION_INTRO_TEXT,
    WELCOME_TEXT,
    card_actions,
    format_current_summary,
    format_history_card,
    format_history_list,
    format_intake_result,
    format_question,
    format_regulation_answer,
)
from app.bot.keyboards import (
    LEGACY_MENU_EXAMPLES,
    LEGACY_MENU_HELP,
    MENU_CURRENT,
    MENU_INSTRUCTION,
    MENU_MY_REQUESTS,
    MENU_NEW,
    MENU_REGULATIONS,
    active_draft_actions,
    budget_choices,
    cancel_confirmation,
    empty_history_actions,
    history_actions,
    history_card_actions,
    instruction_actions,
    new_request_action,
    parse_callback,
    parse_navigation_callback,
    regulation_actions,
)
from app.bot.parser import (
    DeterministicIntakeParser,
    TelegramParseError,
    TelegramSemanticMismatchError,
)
from app.bot.request_history import RequestHistoryError, RequestHistoryService
from app.bot.users import ResolvedTelegramUser
from app.extraction.intake import (
    TelegramExtractionMode,
    TelegramIntakeExtractionService,
)
from app.intake.field_registry import CATEGORY_NAMES, CATEGORY_TAXONOMY_VERSION
from app.intake.models import IntakeFieldUpdate, IntakeStatus, UpdateSource
from app.intake_persistence.exceptions import (
    ActiveDraftNotFoundError,
    ConcurrentIntakeUpdateError,
)
from app.intake_persistence.models import (
    CategoryCandidateOption,
    IntakeConversationState,
    MessageEnvelope,
    PersistentIntakeStepResult,
    ProcurementItemCandidate,
)
from app.rag.answering import RegulationAnswer, RegulationQuestionAnsweringService
from app.rag.conversation import answer_regulation_turn
from app.rag.question_understanding import understand_regulation_question
from app.request_lifecycle.exceptions import (
    LifecycleConcurrentUpdateError,
    LifecycleOwnershipError,
    LifecyclePersistenceError,
    LifecycleRequestNotFoundError,
    LifecycleTransitionError,
    RequestAlreadyCancelledError,
    RequestAlreadyRegisteredError,
    RequestNotReadyError,
)
from app.request_lifecycle.models import LifecycleCommandResult
from app.schemas.common import RequestStatus

logger = logging.getLogger(__name__)

_OUTSIDE_DOMAIN_REFUSAL = (
    "С этим запросом я не помогу — я отвечаю только на вопросы, связанные "
    "с внутренними закупками. Могу помочь оформить заявку на товар или услугу "
    "либо подсказать правила оформления и согласования закупки."
)

_DEBUG_SCALAR_FIELDS = {
    "quantity",
    "unit",
    "amount",
    "desired_delivery_date",
}

_ACCEPT_CONFLICT_REPLIES = {
    "подтвердить",
    "да",
    "изменить",
    "применить",
    "новое значение",
    "подтвердить изменение",
}
_KEEP_CONFLICT_REPLIES = {
    "оставить",
    "оставить прежнее",
    "не менять",
    "нет",
    "отменить изменение",
    "прежнее значение",
}

_SOFTWARE_SCOPE_QUESTION = (
    "Уточните, пожалуйста: лицензии уже приобретены и требуется только "
    "установка, или лицензии также нужно закупить?"
)


class IntakeOrchestrator(Protocol):
    def get_active_session(self, user_id: UUID | str) -> PersistentIntakeStepResult: ...

    def process_structured_step(
        self,
        user_id: UUID | str,
        update: IntakeFieldUpdate,
        request_id: UUID | None = None,
        incoming_message: MessageEnvelope | None = None,
        idempotency_key: str | None = None,
        *,
        intake_conversation: IntakeConversationState | None = None,
    ) -> PersistentIntakeStepResult: ...


class LifecycleService(Protocol):
    def get_confirmation_view(self, request_id, user_id): ...

    def confirm_request(
        self, request_id, user_id, expected_version, idempotency_key
    ) -> LifecycleCommandResult: ...

    def return_to_editing(
        self, request_id, user_id, expected_version, idempotency_key
    ) -> LifecycleCommandResult: ...

    def cancel_draft(
        self,
        request_id,
        user_id,
        expected_version,
        idempotency_key,
        reason=None,
    ) -> LifecycleCommandResult: ...


@dataclass(frozen=True)
class TelegramIntakeOutcome:
    text: str
    idempotency_key: str
    update: IntakeFieldUpdate
    result: PersistentIntakeStepResult | None = None
    reply_markup: object | None = None
    replayed: bool = False
    reason_code: str | None = None


class TelegramIntakeAdapter:
    def __init__(
        self,
        orchestrator: IntakeOrchestrator,
        parser: DeterministicIntakeParser | None = None,
        category_classifier: DeterministicCategoryClassifier | None = None,
        lifecycle_service: LifecycleService | None = None,
        structured_extractor: TelegramIntakeExtractionService | None = None,
        extraction_mode: TelegramExtractionMode = "rule",
        extraction_debug: bool = False,
        dialog_modes: DialogModeRepository | None = None,
        request_history: RequestHistoryService | None = None,
        regulation_qa: RegulationQuestionAnsweringService | None = None,
        category_resolver: CategoryResolutionService | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._categories = category_classifier or DeterministicCategoryClassifier()
        self._parser = parser or DeterministicIntakeParser(
            category_classifier=self._categories
        )
        self._lifecycle = lifecycle_service
        self._structured_extractor = structured_extractor
        self._extraction_mode = extraction_mode
        self._extraction_debug = extraction_debug
        self._dialog_modes = dialog_modes or InMemoryDialogModeRepository()
        self._request_history = request_history
        self._regulation_qa = regulation_qa
        self._category_resolver = category_resolver
        self._extraction_cache: dict[str, IntakeFieldUpdate] = {}
        self._message_outcome_cache: dict[str, TelegramIntakeOutcome] = {}
        self._awaiting_new_request_description: set[UUID] = set()

    def start_message(self, user_id: UUID) -> str:
        self._dialog_modes.set_mode(user_id, "idle")
        active = self._active_or_none(user_id)
        if active is None:
            return WELCOME_TEXT
        return WELCOME_TEXT + "\n\n" + ACTIVE_DRAFT_NOTICE

    def handle_menu(
        self,
        user: ResolvedTelegramUser | UUID,
        action: str,
    ) -> TelegramIntakeOutcome:
        context = _user_context(user)
        active = self._active_or_none(context.user_id)
        if action in {MENU_INSTRUCTION, LEGACY_MENU_EXAMPLES, LEGACY_MENU_HELP}:
            self._dialog_modes.set_mode(context.user_id, "idle")
            return TelegramIntakeOutcome(
                INSTRUCTION_TEXT,
                "menu:instruction",
                IntakeFieldUpdate(),
                reply_markup=instruction_actions(),
            )
        if action == MENU_REGULATIONS:
            self._dialog_modes.clear_pending_regulation(context.user_id)
            self._dialog_modes.set_mode(context.user_id, "regulation_qa")
            return TelegramIntakeOutcome(
                REGULATION_INTRO_TEXT,
                "menu:regulations",
                IntakeFieldUpdate(),
                reply_markup=regulation_actions(),
            )
        if action == MENU_MY_REQUESTS:
            self._dialog_modes.set_mode(context.user_id, "idle")
            return self._history_outcome(context.user_id, "menu:history")
        if action == MENU_CURRENT:
            if active is None:
                self._dialog_modes.set_mode(context.user_id, "idle")
                return TelegramIntakeOutcome(
                    "Сейчас у вас нет незавершённой заявки.",
                    "menu:current",
                    IntakeFieldUpdate(),
                )
            self._dialog_modes.set_mode(context.user_id, "intake")
            return self._current_outcome(active, "menu:current")
        if action == MENU_NEW:
            self._dialog_modes.set_mode(context.user_id, "intake")
            if active is None:
                if context.user_id in self._awaiting_new_request_description:
                    return TelegramIntakeOutcome(
                        "Можно отправлять описание новой заявки.",
                        "menu:new",
                        IntakeFieldUpdate(),
                    )
                self._awaiting_new_request_description.add(context.user_id)
                return TelegramIntakeOutcome(
                    NEW_REQUEST_PROMPT,
                    "menu:new",
                    IntakeFieldUpdate(),
                )
            return TelegramIntakeOutcome(
                "У вас уже есть незавершённая заявка. Чтобы начать новую, "
                "текущую нужно сначала отменить.",
                "menu:new",
                IntakeFieldUpdate(),
                active,
                active_draft_actions(active.request_id, active.request_version),
            )
        raise ValueError("Unsupported Telegram menu action")

    def handle_text(
        self,
        user: ResolvedTelegramUser | UUID,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> TelegramIntakeOutcome:
        context = _user_context(user)
        user_id = context.user_id
        key = self.idempotency_key(chat_id, message_id)
        replayed = self._message_outcome_cache.get(key)
        if replayed is not None:
            self._debug_event(
                key,
                "duplicate_message_suppressed",
                outgoing_response_count=0,
            )
            return replace(replayed, replayed=True)
        current_mode = self._dialog_modes.get_mode(user_id)
        if current_mode == "regulation_qa":
            return self._handle_regulation_question(
                user_id,
                key,
                text,
                mode_before=current_mode,
            )
        active = self._active_or_none(user_id)
        idle_initial_update: IntakeFieldUpdate | None = None
        if current_mode == "idle" and active is None:
            try:
                idle_initial_update = self._parser.parse(text)
            except TelegramParseError:
                pass
            understanding = understand_regulation_question(text)
            if (
                understanding.domain_decision == "outside_domain"
                and not _has_confident_idle_intake_signal(
                    text,
                    idle_initial_update,
                )
            ):
                logger.info(
                    "Telegram idle routing message_ref=%s mode_before=idle "
                    "domain_decision=outside_domain route=scope_refusal "
                    "mode_after=idle",
                    sha256(key.encode("utf-8")).hexdigest()[:12],
                )
                outcome = TelegramIntakeOutcome(
                    _OUTSIDE_DOMAIN_REFUSAL,
                    key,
                    IntakeFieldUpdate(),
                )
                self._remember_message_outcome(key, outcome)
                return outcome
        if (
            active is not None
            and active.intake_result.status == IntakeStatus.READY_FOR_CONFIRMATION
            and active.dialog_state.intake_status != IntakeStatus.EDITING
        ):
            return self._current_outcome(active, key)
        if active is not None and active.intake_result.draft.conflicts:
            return self._handle_pending_conflict(
                context,
                active,
                key,
                message_id,
                text,
            )

        if (
            active is not None
            and active.dialog_state.intake_conversation.category_clarification_kind
            == "software_acquisition_scope"
        ):
            return self._handle_software_scope_reply(
                context,
                active,
                key,
                message_id,
                text,
            )

        if (
            active is not None
            and active.dialog_state.intake_conversation.split_required
        ):
            return self._handle_split_selection(context, active, key, message_id, text)

        original_missing = active is None
        original_awaiting = (
            active.dialog_state.awaiting_field_code if active is not None else None
        )
        profile_update = self._profile_update(context, active, original_awaiting)
        if profile_update.values:
            active = self._orchestrator.process_structured_step(
                user_id,
                profile_update,
                request_id=active.request_id if active is not None else None,
                incoming_message=self._envelope(message_id, "telegram_profile"),
                idempotency_key=f"{key}:profile",
            )

        question = None
        awaiting_field_code = None
        if not original_missing and active is not None:
            question = (
                active.dialog_state.next_question or active.intake_result.next_question
            )
            awaiting_field_code = active.dialog_state.awaiting_field_code
        if original_missing:
            software_outcome = self._start_software_scope_resolution(
                context,
                active,
                key,
                message_id,
                text,
            )
            if software_outcome is not None:
                return software_outcome
            split_outcome = self._start_multi_category_split(
                context,
                active,
                key,
                message_id,
                text,
            )
            if split_outcome is not None:
                return split_outcome
        category_candidates = self._category_candidates(active, text)
        if question is not None and question.field_code == "category_code":
            confirmation_outcome = self._handle_llm_category_confirmation(
                context,
                active,
                key,
                message_id,
                text,
            )
            if confirmation_outcome is not None:
                return confirmation_outcome
            category_outcome = self._handle_category_clarification_command(
                context,
                active,
                key,
                message_id,
                text,
                category_candidates,
            )
            if category_outcome is not None:
                return category_outcome
        use_structured = self._should_use_structured_extraction(
            original_missing=original_missing,
            active=active,
            question=question,
            text=text,
        )
        editing = (
            active is not None
            and active.dialog_state.intake_status == IntakeStatus.EDITING
        )
        try:
            update = idle_initial_update or self._parser.parse(
                text,
                None if editing else question,
                None if editing else awaiting_field_code,
                category_candidates,
            )
        except TelegramSemanticMismatchError:
            raise
        except TelegramParseError:
            if question is not None and question.field_code == "category_code":
                if not use_structured:
                    return self._category_parse_failure(
                        context,
                        active,
                        key,
                        message_id,
                        category_candidates,
                    )
                update = self._parser.parse(text)
            else:
                if not use_structured:
                    raise
                update = IntakeFieldUpdate()
        proposed_category = update.values.get("category_code")
        if isinstance(proposed_category, str) and active is not None:
            selected_option = next(
                (
                    option
                    for option in (
                        active.dialog_state.intake_conversation.category_candidates
                    )
                    if option.code == proposed_category
                    and option.selectable
                ),
                None,
            )
            if selected_option is not None:
                category_evidence = dict(update.evidence_by_field)
                support = (
                    "llm_confirmed"
                    if selected_option.source in {"llm_exact", "llm_candidates"}
                    else selected_option.source
                )
                if support == "llm_confirmed":
                    draft = active.intake_result.draft
                    support = category_confirmation_evidence(
                        draft.procurement_type.value,
                        draft.item_name or "",
                        proposed_category,
                        category_draft_context_fingerprint(draft),
                    )
                else:
                    support = f"category_support={support}"
                category_evidence["category_code"] = support
                update = update.model_copy(
                    update={"evidence_by_field": category_evidence}
                )
        active_type = (
            active.intake_result.draft.procurement_type
            if active is not None
            else None
        )
        if (
            proposed_category is not None
            and active_type is not None
            and not (
                proposed_category in {"G05", "S05"}
                and {"G05", "S05"}.issubset(category_candidates)
            )
            and not proposed_category.startswith(
                "G" if active_type == "goods" else "S"
            )
        ):
            return self._category_parse_failure(
                context,
                active,
                key,
                message_id,
                category_candidates,
            )
        parsed_update = update
        self._debug_event(
            key,
            "deterministic_extraction",
            current_question=question.field_code if question else None,
            candidate_fields=sorted(update.values),
            scalar_values=_safe_scalar_values(update),
        )
        if use_structured:
            cached = self._extraction_cache.get(key)
            if cached is None:
                assert self._structured_extractor is not None
                resolution = self._structured_extractor.resolve_message(
                    text,
                    active.intake_result.draft if active is not None else None,
                    question,
                    update,
                    source_kind=(
                        "initial_description"
                        if original_missing
                        else "clarification_answer"
                    ),
                    merge_deterministic=True,
                    fallback_on_error=True,
                )
                assert resolution.update is not None
                cached = resolution.update
                if not original_missing and question is not None:
                    values = dict(cached.values)
                    evidence = dict(cached.evidence_by_field)
                    if (
                        question.field_code not in values
                        and question.field_code in parsed_update.values
                    ):
                        values[question.field_code] = parsed_update.values[
                            question.field_code
                        ]
                        if question.field_code in parsed_update.evidence_by_field:
                            evidence[question.field_code] = (
                                parsed_update.evidence_by_field[question.field_code]
                            )
                    cached = cached.model_copy(
                        update={
                            "values": values,
                            "evidence_by_field": evidence,
                            "source": UpdateSource.USER,
                            "answered_field_code": (
                                None
                                if cached.explicit_correction
                                else question.field_code
                            ),
                        }
                    )
                self._remember_extraction(key, cached)
                if resolution.structured is not None:
                    structured = resolution.structured
                    logger.info(
                        "Telegram extraction succeeded mode=%s proposed=%s "
                        "accepted=%s rejected=%s conflicts=%s duration_ms=%s "
                        "prompt_version=%s schema_version=%s",
                        self._extraction_mode,
                        structured.proposed_fields,
                        structured.accepted_fields,
                        len(structured.rejected_fields),
                        structured.metadata.get("conflict_count"),
                        structured.metadata.get("duration_ms"),
                        structured.metadata.get("prompt_version"),
                        structured.metadata.get("schema_version"),
                    )
                    self._debug_event(
                        key,
                        "structured_extraction",
                        candidate_fields=sorted(structured.update.values),
                        accepted_fields=sorted(cached.values),
                        rejected_fields=list(structured.rejected_fields),
                        rejection_codes={
                            field_name: "normalization_or_evidence_rejected"
                            for field_name in structured.rejected_fields
                        },
                    )
                else:
                    assert resolution.failure is not None
                    logger.warning(
                        "Telegram structured extraction fallback mode=%s "
                        "error_type=%s diagnostic_code=%s",
                        self._extraction_mode,
                        resolution.failure.error_type or resolution.failure.error,
                        resolution.failure.diagnostic_code,
                    )
                    self._debug_event(
                        key,
                        "structured_fallback",
                        rejection_codes=(resolution.failure.validation_error_codes),
                    )
            update = cached
        category_resolution = self._resolve_category_for_update(
            update,
            active,
            text,
            should_resolve=(
                original_missing
                or editing
                or bool(
                    {
                        "item_name",
                        "procurement_type",
                        "description",
                        "specifications",
                        "desired_result",
                        "business_justification",
                    }
                    & set(update.values)
                )
            ),
        )
        if category_resolution is not None:
            self._debug_event(
                key,
                "category_resolution",
                procurement_type=(
                    update.values.get("procurement_type")
                    or (
                        active.intake_result.draft.procurement_type.value
                        if active is not None
                        and active.intake_result.draft.procurement_type is not None
                        else None
                    )
                ),
                context_fingerprint=category_resolution.context_fingerprint,
                taxonomy_version=CATEGORY_TAXONOMY_VERSION,
                provider_called=category_resolution.provider_called,
                validated_decision=category_resolution.decision,
                reason_code=category_resolution.reason_code,
                candidate_codes=(
                    category_resolution.candidates
                    or (
                        (category_resolution.category_code,)
                        if category_resolution.category_code
                        else ()
                    )
                ),
            )
        if (
            category_resolution is not None
            and category_resolution.decision == "deterministic_exact"
            and category_resolution.category_code is not None
        ):
            values = dict(update.values)
            evidence = dict(update.evidence_by_field)
            values["category_code"] = category_resolution.category_code
            evidence["category_code"] = "category_support=classifier_exact"
            update = update.model_copy(
                update={"values": values, "evidence_by_field": evidence}
            )
        if category_resolution is not None and category_resolution.decision in {
            "llm_exact",
            "llm_candidates",
            "unresolved",
        }:
            values = dict(update.values)
            evidence = dict(update.evidence_by_field)
            values.pop("category_code", None)
            evidence.pop("category_code", None)
            update = update.model_copy(
                update={"values": values, "evidence_by_field": evidence}
            )
        if (
            not editing
            and question is not None
            and update.source == UpdateSource.USER
            and not update.explicit_correction
        ):
            update = update.model_copy(
                update={"answered_field_code": question.field_code}
            )
        if editing:
            update = update.model_copy(
                update={
                    "source": UpdateSource.USER,
                    "explicit_correction": True,
                }
            )
        if (
            question is not None
            and question.field_code == "category_code"
            and update.values.get("category_code") in {"G05", "S05"}
            and {"G05", "S05"}.issubset(category_candidates)
        ):
            category_values = dict(update.values)
            category_values["procurement_type"] = (
                "goods"
                if category_values["category_code"] == "G05"
                else "service"
            )
            update = update.model_copy(update={"values": category_values})
            current_type = active.intake_result.draft.procurement_type
            if current_type is not None and current_type != category_values[
                "procurement_type"
            ]:
                update = update.model_copy(update={"explicit_correction": True})
        intake_conversation = None
        category_response = None
        if category_resolution is not None and category_resolution.decision in {
            "llm_exact",
            "llm_candidates",
        }:
            assert category_resolution.candidate_source is not None
            decomposition_fingerprint = (
                active.dialog_state.intake_conversation.decomposition_fingerprint
                if active is not None
                else None
            )
            selected_type = update.values.get("procurement_type")
            if selected_type not in {"goods", "service"} and active is not None:
                current_type = active.intake_result.draft.procurement_type
                selected_type = current_type.value if current_type is not None else None
            intake_conversation = IntakeConversationState(
                category_candidates=[
                    CategoryCandidateOption(
                        code=code,
                        label=CATEGORY_NAMES[code],
                        source=category_resolution.candidate_source,
                        selectable=True,
                        readiness_eligible=False,
                    )
                    for code in category_resolution.candidates
                ],
                category_procurement_type=selected_type,
                category_subject_fingerprint=(
                    category_resolution.subject_fingerprint
                ),
                category_context_fingerprint=(
                    category_resolution.context_fingerprint
                ),
                category_decomposition_fingerprint=decomposition_fingerprint,
                decomposition_fingerprint=decomposition_fingerprint,
                category_step_id=f"category-llm:{key}",
                original_description=text,
            )
            category_response = _format_llm_category_resolution(
                category_resolution
            )
        elif (
            category_resolution is not None
            and category_resolution.decision == "deterministic_candidates"
        ):
            decomposition_fingerprint = (
                active.dialog_state.intake_conversation.decomposition_fingerprint
                if active is not None
                else None
            )
            selected_type = update.values.get("procurement_type")
            if selected_type not in {"goods", "service"} and active is not None:
                current_type = active.intake_result.draft.procurement_type
                selected_type = current_type.value if current_type is not None else None
            intake_conversation = IntakeConversationState(
                category_candidates=[
                    CategoryCandidateOption(
                        code=code,
                        label=CATEGORY_NAMES[code],
                        source="classifier_multiple",
                        selectable=True,
                        readiness_eligible=True,
                    )
                    for code in category_resolution.candidates
                ],
                category_procurement_type=selected_type,
                category_subject_fingerprint=(
                    category_resolution.subject_fingerprint
                ),
                category_context_fingerprint=(
                    category_resolution.context_fingerprint
                ),
                category_decomposition_fingerprint=decomposition_fingerprint,
                decomposition_fingerprint=decomposition_fingerprint,
                category_step_id=f"category-deterministic:{key}",
            )
            category_response = _format_category_candidates(
                category_resolution.candidates
            )
        elif (
            category_resolution is not None
            and category_resolution.decision == "deterministic_exact"
            and active is not None
            and not active.dialog_state.intake_conversation.is_empty
        ):
            intake_conversation = IntakeConversationState()
        elif (
            category_resolution is not None
            and category_resolution.decision == "unresolved"
        ):
            intake_conversation = IntakeConversationState(
                category_procurement_type=(
                    update.values.get("procurement_type")
                    if update.values.get("procurement_type") in {"goods", "service"}
                    else (
                        active.intake_result.draft.procurement_type.value
                        if active is not None
                        and active.intake_result.draft.procurement_type is not None
                        else None
                    )
                ),
                category_subject_fingerprint=(
                    category_resolution.subject_fingerprint
                ),
                category_context_fingerprint=(
                    category_resolution.context_fingerprint
                ),
                category_step_id=f"category-unresolved:{key}",
                original_description=(
                    active.dialog_state.intake_conversation.original_description
                    if active is not None
                    and active.dialog_state.intake_conversation.original_description
                    else text
                ),
                reason_code="category_candidates_missing",
            )
            category_response = _format_category_candidates(())
        if category_resolution is None and "category_code" in update.values:
            if (
                active is not None
                and not active.dialog_state.intake_conversation.is_empty
            ):
                intake_conversation = IntakeConversationState()
        elif (
            category_resolution is None
            and question is not None
            and question.field_code == "category_code"
        ):
            intake_conversation = self._category_state(
                active,
                category_candidates,
            )
        elif (
            category_resolution is None
            and update.values.get("procurement_type") in {"goods", "service"}
        ):
            selected_type = update.values["procurement_type"]
            source = " ".join(
                value
                for value in (
                    active.intake_result.draft.item_name if active else None,
                    active.intake_result.draft.description if active else None,
                    text,
                )
                if value
            )
            classification = self._categories.classify(source, selected_type)
            candidate_codes = classification.candidates[:4]
            if classification.kind == "exact" and classification.category_code:
                candidate_codes = (classification.category_code,)
            if candidate_codes:
                intake_conversation = IntakeConversationState(
                    category_candidates=[
                        CategoryCandidateOption(
                            code=code,
                            label=CATEGORY_NAMES[code],
                            source=(
                                "classifier_exact"
                                if classification.kind == "exact"
                                else "classifier_multiple"
                            ),
                            selectable=True,
                            readiness_eligible=True,
                        )
                        for code in candidate_codes
                    ],
                    category_procurement_type=selected_type,
                    category_step_id=f"category:{key}",
                )
            else:
                intake_conversation = IntakeConversationState()
        elif (
            category_resolution is None
            and active is not None
            and active.intake_result.draft.category_code is None
            and active.dialog_state.intake_conversation.is_empty
        ):
            candidate_codes = self._derived_category_candidates(active, text)
            if candidate_codes:
                intake_conversation = IntakeConversationState(
                    category_candidates=[
                        CategoryCandidateOption(
                            code=code,
                            label=CATEGORY_NAMES[code],
                            source="derived",
                            selectable=True,
                            readiness_eligible=True,
                        )
                        for code in candidate_codes
                    ],
                    category_procurement_type=(
                        active.intake_result.draft.procurement_type.value
                        if active.intake_result.draft.procurement_type is not None
                        else None
                    ),
                    category_step_id=f"category:{key}",
                )
        step_kwargs = {
            "request_id": active.request_id if active is not None else None,
            "incoming_message": self._envelope(message_id, "telegram"),
            "idempotency_key": key,
        }
        if intake_conversation is not None:
            step_kwargs["intake_conversation"] = intake_conversation
        try:
            result = self._orchestrator.process_structured_step(
                user_id,
                update,
                **step_kwargs,
            )
        except ConcurrentIntakeUpdateError:
            refreshed = self._active_or_none(user_id)
            text_result = (
                "Состояние заявки обновилось. Повторите ответ на актуальный вопрос."
            )
            if refreshed is not None and refreshed.intake_result.next_question:
                text_result += "\n\n" + format_question(
                    refreshed.intake_result.next_question,
                    refreshed.intake_result.draft.procurement_type,
                    self._category_candidates(refreshed),
                )
            return TelegramIntakeOutcome(text_result, key, update)
        response = category_response or format_intake_result(
            result, self._category_candidates(result)
        )
        if current_mode == "idle" and original_missing:
            self._dialog_modes.set_mode(user_id, "intake")
        self._awaiting_new_request_description.discard(user_id)
        if update.values.get("budget_status") == "unknown":
            response = "Хорошо, отмечу, что бюджет нужно уточнить.\n\n" + response
        outcome = TelegramIntakeOutcome(
            response,
            key,
            update,
            result,
            card_actions(result),
        )
        self._debug_event(
            key,
            "persisted_result",
            merged_fields=sorted(update.values),
            completed_fields=sorted(result.intake_result.completeness.completed_fields),
            missing_fields=list(result.intake_result.completeness.missing_fields),
            next_question=(
                result.intake_result.next_question.field_code
                if result.intake_result.next_question
                else None
            ),
            outgoing_response_count=1,
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _debug_event(self, message_key: str, event: str, **details: object) -> None:
        if not self._extraction_debug:
            return
        message_ref = sha256(message_key.encode("utf-8")).hexdigest()[:12]
        logger.info(
            "Telegram extraction debug message_ref=%s mode=%s event=%s details=%s",
            message_ref,
            self._extraction_mode,
            event,
            details,
        )

    def _handle_pending_conflict(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult,
        key: str,
        message_id: int,
        text: str,
    ) -> TelegramIntakeOutcome:
        conflict = active.intake_result.draft.conflicts[0]
        resolution = _conflict_resolution(text)
        if resolution is None:
            outcome = TelegramIntakeOutcome(
                "Ответьте «подтвердить», чтобы применить новое значение, "
                "или «оставить», чтобы сохранить прежнее.",
                key,
                IntakeFieldUpdate(),
                active,
                card_actions(active),
            )
            self._remember_message_outcome(key, outcome)
            return outcome
        update = IntakeFieldUpdate(
            source=UpdateSource.USER,
            resolve_conflict_id=conflict.id,
            conflict_resolution=resolution,
        )
        result = self._orchestrator.process_structured_step(
            user.user_id,
            update,
            request_id=active.request_id,
            incoming_message=self._envelope(message_id, "telegram_conflict"),
            idempotency_key=key,
        )
        prefix = (
            "Новое значение применено."
            if resolution == "accept"
            else "Прежнее значение сохранено."
        )
        outcome = TelegramIntakeOutcome(
            prefix
            + "\n\n"
            + format_intake_result(result, self._category_candidates(result)),
            key,
            update,
            result,
            card_actions(result),
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def handle_navigation_callback(
        self,
        user: ResolvedTelegramUser | UUID,
        callback_query_id: str,
        data: str | None,
    ) -> TelegramIntakeOutcome:
        context = _user_context(user)
        callback = parse_navigation_callback(data)
        key = f"telegram-navigation:{callback_query_id}:{callback.action}"
        if callback.request_id is not None:
            key += f":{callback.request_id.hex}"
        replayed = self._message_outcome_cache.get(key)
        if replayed is not None:
            return replace(replayed, replayed=True)
        if callback.action in {"instruction", "help", "examples"}:
            outcome = self.handle_menu(context, MENU_INSTRUCTION)
        elif callback.action == "regulations":
            outcome = self.handle_menu(context, MENU_REGULATIONS)
        elif callback.action in {"regulations_end", "menu"}:
            self._dialog_modes.set_mode(context.user_id, "idle")
            outcome = TelegramIntakeOutcome(
                "Выберите действие в главном меню.",
                key,
                IntakeFieldUpdate(),
            )
        elif callback.action == "new":
            outcome = self.handle_menu(context, MENU_NEW)
        elif callback.action == "current":
            outcome = self.handle_menu(context, MENU_CURRENT)
        elif callback.action == "history":
            self._dialog_modes.set_mode(context.user_id, "idle")
            outcome = self._history_outcome(context.user_id, key)
        elif callback.action == "request" and callback.request_id is not None:
            self._dialog_modes.set_mode(context.user_id, "idle")
            outcome = self._history_card_outcome(
                context.user_id,
                callback.request_id,
                key,
            )
        else:
            outcome = TelegramIntakeOutcome(
                "Меню обновлено. Выберите действие в главном меню.",
                key,
                IntakeFieldUpdate(),
            )
        outcome = replace(outcome, idempotency_key=key)
        self._remember_message_outcome(key, outcome)
        return outcome

    def handle_callback(
        self,
        user: ResolvedTelegramUser | UUID,
        callback_query_id: str,
        data: str | None,
    ) -> TelegramIntakeOutcome:
        context = _user_context(user)
        callback = parse_callback(data)
        key = (
            f"telegram-callback:{callback_query_id}:{callback.action}:"
            f"{callback.request_id.hex}"
        )
        if callback.action in {"conflict_accept", "conflict_keep"}:
            return self._handle_conflict_callback(context, callback, key)
        if self._lifecycle is None:
            return self._technical_error(key)
        try:
            if callback.action == "menu":
                self._lifecycle.get_confirmation_view(
                    callback.request_id, context.user_id
                )
                self._dialog_modes.set_mode(context.user_id, "idle")
                return TelegramIntakeOutcome(
                    "Выберите действие в главном меню.", key, IntakeFieldUpdate()
                )
            if callback.action == "current":
                self._lifecycle.get_confirmation_view(
                    callback.request_id, context.user_id
                )
                active = self._active_or_none(context.user_id)
                if active is None or active.request_id != callback.request_id:
                    return TelegramIntakeOutcome(
                        "Сейчас у вас нет незавершённой заявки.",
                        key,
                        IntakeFieldUpdate(),
                    )
                self._dialog_modes.set_mode(context.user_id, "intake")
                return self._current_outcome(active, key)
            if callback.action in {"cancel_ask", "cancel_new_ask"}:
                self._lifecycle.get_confirmation_view(
                    callback.request_id, context.user_id
                )
                return TelegramIntakeOutcome(
                    "Отменить эту заявку? Введённые данные останутся в "
                    "истории, но продолжить оформление будет нельзя.",
                    key,
                    IntakeFieldUpdate(),
                    reply_markup=cancel_confirmation(
                        callback.request_id,
                        callback.version,
                        start_new=callback.action == "cancel_new_ask",
                    ),
                )
            if callback.action == "confirm":
                confirmed = self._lifecycle.confirm_request(
                    callback.request_id,
                    context.user_id,
                    callback.version,
                    key,
                )
                if confirmed.replayed:
                    text = "Эта заявка уже зарегистрирована."
                else:
                    self._dialog_modes.set_mode(context.user_id, "intake")
                    self._awaiting_new_request_description.add(context.user_id)
                    text = (
                        "Заявка зарегистрирована.\n\n"
                        f"Номер заявки: {confirmed.request_number}\n\n"
                        + NEW_REQUEST_PROMPT
                    )
                return TelegramIntakeOutcome(
                    text,
                    key,
                    IntakeFieldUpdate(),
                )
            if callback.action == "edit":
                self._dialog_modes.set_mode(context.user_id, "intake")
                self._lifecycle.return_to_editing(
                    callback.request_id,
                    context.user_id,
                    callback.version,
                    key,
                )
                return TelegramIntakeOutcome(
                    "Хорошо, заявку можно изменить. Напишите, что именно "
                    "нужно исправить.",
                    key,
                    IntakeFieldUpdate(),
                )
            if callback.action == "budget":
                edited = self._lifecycle.return_to_editing(
                    callback.request_id,
                    context.user_id,
                    callback.version,
                    key,
                )
                return TelegramIntakeOutcome(
                    "Эта закупка предусмотрена в утверждённом бюджете?",
                    key,
                    IntakeFieldUpdate(),
                    reply_markup=budget_choices(edited.request_id, edited.version),
                )
            if callback.action in {"budget_yes", "budget_no", "budget_unknown"}:
                return self._handle_budget_callback(context, callback, key)
            if callback.action in {"cancel_yes", "cancel_new_yes"}:
                cancelled = self._lifecycle.cancel_draft(
                    callback.request_id,
                    context.user_id,
                    callback.version,
                    key,
                    "Отменено пользователем в Telegram",
                )
                text = "Заявка отменена. Можно начать новую через меню."
                if callback.action == "cancel_new_yes":
                    self._dialog_modes.set_mode(context.user_id, "intake")
                    self._awaiting_new_request_description.add(context.user_id)
                    text += "\n\n" + NEW_REQUEST_PROMPT
                else:
                    self._dialog_modes.set_mode(context.user_id, "idle")
                return TelegramIntakeOutcome(
                    text,
                    key,
                    IntakeFieldUpdate(),
                    reply_markup=(
                        None
                        if callback.action == "cancel_new_yes"
                        else new_request_action(cancelled.request_id, cancelled.version)
                    ),
                )
            if callback.action == "new":
                self._dialog_modes.set_mode(context.user_id, "intake")
                self._lifecycle.get_confirmation_view(
                    callback.request_id, context.user_id
                )
                active = self._active_or_none(context.user_id)
                if active is not None:
                    return TelegramIntakeOutcome(
                        "У вас уже есть незавершённая заявка.",
                        key,
                        IntakeFieldUpdate(),
                        active,
                        active_draft_actions(active.request_id, active.request_version),
                    )
                if context.user_id in self._awaiting_new_request_description:
                    return TelegramIntakeOutcome(
                        "Можно отправлять описание новой заявки.",
                        key,
                        IntakeFieldUpdate(),
                    )
                self._awaiting_new_request_description.add(context.user_id)
                return TelegramIntakeOutcome(
                    NEW_REQUEST_PROMPT, key, IntakeFieldUpdate()
                )
            raise ValueError("Unsupported callback action")
        except RequestAlreadyRegisteredError:
            return TelegramIntakeOutcome(
                "Эта заявка уже зарегистрирована.", key, IntakeFieldUpdate()
            )
        except RequestAlreadyCancelledError:
            return TelegramIntakeOutcome(
                "Эта заявка уже отменена.", key, IntakeFieldUpdate()
            )
        except LifecycleConcurrentUpdateError:
            return self._stale_outcome(context.user_id, callback.request_id, key)
        except (LifecycleOwnershipError, LifecycleRequestNotFoundError):
            return TelegramIntakeOutcome(
                "Не удалось выполнить действие для этой заявки.",
                key,
                IntakeFieldUpdate(),
            )
        except (RequestNotReadyError, LifecycleTransitionError):
            return self._stale_outcome(context.user_id, callback.request_id, key)
        except LifecyclePersistenceError:
            return self._technical_error(key)

    def _handle_regulation_question(
        self,
        user_id: UUID,
        key: str,
        text: str,
        *,
        mode_before: DialogMode,
    ) -> TelegramIntakeOutcome:
        if self._regulation_qa is None:
            return TelegramIntakeOutcome(
                "Сейчас не удалось обратиться к базе регламентов. "
                "Попробуйте повторить вопрос позже.",
                key,
                IntakeFieldUpdate(),
                reply_markup=regulation_actions(),
            )
        fingerprint = sha256(text.strip().encode("utf-8")).hexdigest()
        try:
            replay = self._dialog_modes.find_regulation_replay(
                user_id,
                key,
                fingerprint,
            )
            mode_after = self._dialog_modes.get_mode(user_id)
        except DialogReplayConflictError:
            return TelegramIntakeOutcome(
                "Это сообщение уже было обработано. Отправьте вопрос ещё раз.",
                key,
                IntakeFieldUpdate(),
                reply_markup=regulation_actions(),
            )
        if replay is not None:
            self._log_regulation_turn(
                key,
                replay,
                mode_before=mode_before,
                mode_after=mode_after,
                replayed=True,
            )
            outcome = TelegramIntakeOutcome(
                format_regulation_answer(replay),
                key,
                IntakeFieldUpdate(),
                reply_markup=regulation_actions(),
                replayed=True,
            )
            self._remember_message_outcome(key, outcome)
            return outcome
        try:
            pending = self._dialog_modes.get_pending_regulation(user_id)
            turn = answer_regulation_turn(self._regulation_qa, text, pending)
            result = turn.result
            if turn.pending is None:
                self._dialog_modes.clear_pending_regulation(user_id)
            else:
                self._dialog_modes.save_pending_regulation(user_id, turn.pending)
            self._dialog_modes.save_regulation_replay(
                user_id,
                key,
                fingerprint,
                result,
            )
            mode_after = self._dialog_modes.get_mode(user_id)
        except DialogModePersistenceError:
            return self._technical_error(key)
        self._log_regulation_turn(
            key,
            result,
            mode_before=mode_before,
            mode_after=mode_after,
        )
        outcome = TelegramIntakeOutcome(
            format_regulation_answer(result),
            key,
            IntakeFieldUpdate(),
            reply_markup=regulation_actions(),
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    @staticmethod
    def _log_regulation_turn(
        key: str,
        result: RegulationAnswer,
        *,
        mode_before: DialogMode,
        mode_after: DialogMode,
        replayed: bool = False,
    ) -> None:
        diagnostics = result.diagnostics
        logger.info(
            "Telegram regulation answer message_ref=%s mode_before=%s "
            "status=%s refusal_reason=%s mode_after=%s replayed=%s "
            "retrieval_status=%s chunks=%s sources=%s duration_ms=%s "
            "reason_code=%s error_code=%s",
            sha256(key.encode("utf-8")).hexdigest()[:12],
            mode_before,
            result.status,
            result.refusal_reason,
            mode_after,
            replayed,
            diagnostics.get("retrieval_status"),
            diagnostics.get("chunk_count", 0),
            diagnostics.get("source_count", 0),
            diagnostics.get("duration_ms", 0),
            result.refusal_reason,
            diagnostics.get("error_code"),
        )

    def _history_outcome(self, user_id: UUID, key: str) -> TelegramIntakeOutcome:
        if self._request_history is None:
            return self._technical_error(key)
        try:
            items = self._request_history.list_recent(user_id, limit=5)
        except RequestHistoryError:
            return self._technical_error(key)
        buttons = (
            history_actions(
                [
                    (item.request_id, item.request_number or "Без номера")
                    for item in items
                ]
            )
            if items
            else empty_history_actions()
        )
        return TelegramIntakeOutcome(
            format_history_list(items),
            key,
            IntakeFieldUpdate(),
            reply_markup=buttons,
        )

    def _history_card_outcome(
        self,
        user_id: UUID,
        request_id: UUID,
        key: str,
    ) -> TelegramIntakeOutcome:
        if self._request_history is None:
            return self._technical_error(key)
        try:
            view = self._request_history.get(request_id, user_id)
        except RequestHistoryError:
            return self._technical_error(key)
        if view is None:
            return TelegramIntakeOutcome(
                "Заявка не найдена или недоступна.",
                key,
                IntakeFieldUpdate(),
                reply_markup=history_card_actions(),
            )
        return TelegramIntakeOutcome(
            format_history_card(view),
            key,
            IntakeFieldUpdate(),
            reply_markup=history_card_actions(),
        )

    def _handle_conflict_callback(
        self,
        user: ResolvedTelegramUser,
        callback,
        key: str,
    ) -> TelegramIntakeOutcome:
        active = self._active_or_none(user.user_id)
        if (
            active is None
            or active.request_id != callback.request_id
            or active.request_version != callback.version
            or not active.intake_result.draft.conflicts
        ):
            return self._stale_outcome(user.user_id, callback.request_id, key)
        conflict = active.intake_result.draft.conflicts[0]
        resolution = "accept" if callback.action == "conflict_accept" else "keep"
        update = IntakeFieldUpdate(
            source=UpdateSource.USER,
            resolve_conflict_id=conflict.id,
            conflict_resolution=resolution,
        )
        result = self._orchestrator.process_structured_step(
            user.user_id,
            update,
            request_id=callback.request_id,
            incoming_message=self._envelope(0, "telegram_conflict_callback"),
            idempotency_key=f"{key}:intake",
        )
        prefix = (
            "Новое значение применено."
            if resolution == "accept"
            else "Прежнее значение сохранено."
        )
        return TelegramIntakeOutcome(
            prefix
            + "\n\n"
            + format_intake_result(result, self._category_candidates(result)),
            key,
            update,
            result,
            card_actions(result),
        )

    def _handle_budget_callback(
        self,
        user: ResolvedTelegramUser,
        callback,
        key: str,
    ) -> TelegramIntakeOutcome:
        active = self._active_or_none(user.user_id)
        if (
            active is None
            or active.request_id != callback.request_id
            or active.request_version != callback.version
        ):
            return self._stale_outcome(user.user_id, callback.request_id, key)
        value = {
            "budget_yes": "budgeted",
            "budget_no": "unbudgeted",
            "budget_unknown": "unknown",
        }[callback.action]
        update = IntakeFieldUpdate(
            values={"budget_status": value},
            source=UpdateSource.USER,
            explicit_correction=True,
        )
        result = self._orchestrator.process_structured_step(
            user.user_id,
            update,
            request_id=callback.request_id,
            incoming_message=self._envelope(0, "telegram_callback"),
            idempotency_key=f"{key}:intake",
        )
        prefix = (
            "Хорошо, отмечу, что бюджет нужно уточнить.\n\n"
            if value == "unknown"
            else "Бюджетный статус обновлён.\n\n"
        )
        return TelegramIntakeOutcome(
            prefix + format_intake_result(result, self._category_candidates(result)),
            key,
            update,
            result,
            card_actions(result),
        )

    def _current_outcome(
        self,
        active: PersistentIntakeStepResult,
        key: str,
        *,
        prefix: str = "",
    ) -> TelegramIntakeOutcome:
        text = format_current_summary(active)
        if prefix:
            text = prefix + "\n\n" + text
        return TelegramIntakeOutcome(
            text,
            key,
            IntakeFieldUpdate(),
            active,
            card_actions(active),
        )

    def _stale_outcome(
        self,
        user_id: UUID,
        request_id: UUID,
        key: str,
    ) -> TelegramIntakeOutcome:
        active = self._active_or_none(user_id)
        if active is not None and active.request_id == request_id:
            return self._current_outcome(
                active,
                key,
                prefix="Заявка уже изменилась. Показываю актуальную версию.",
            )
        try:
            assert self._lifecycle is not None
            view = self._lifecycle.get_confirmation_view(request_id, user_id)
        except Exception:
            return TelegramIntakeOutcome(
                "Заявка уже изменилась. Откройте текущую заявку через меню.",
                key,
                IntakeFieldUpdate(),
            )
        if view.request_status == RequestStatus.NEW:
            text = "Эта заявка уже зарегистрирована."
        elif view.request_status == RequestStatus.CANCELLED:
            text = "Эта заявка уже отменена."
        else:
            text = "Заявка уже изменилась. Откройте актуальную версию через меню."
        return TelegramIntakeOutcome(text, key, IntakeFieldUpdate())

    @staticmethod
    def _technical_error(key: str) -> TelegramIntakeOutcome:
        return TelegramIntakeOutcome(
            "Не удалось выполнить действие. Попробуйте ещё раз немного позже.",
            key,
            IntakeFieldUpdate(),
        )

    @staticmethod
    def idempotency_key(chat_id: int, message_id: int) -> str:
        return f"telegram:{chat_id}:{message_id}"

    @staticmethod
    def _envelope(message_id: int, source: str) -> MessageEnvelope:
        return MessageEnvelope(
            message_id=str(message_id),
            metadata={"transport": "telegram", "source": source},
        )

    @staticmethod
    def _profile_update(
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult | None,
        awaiting_field_code: str | None,
    ) -> IntakeFieldUpdate:
        draft = active.intake_result.draft if active is not None else None
        values: dict[str, str] = {}
        if (
            user.full_name
            and awaiting_field_code != "contact_person"
            and (draft is None or draft.contact_person is None)
        ):
            values["contact_person"] = user.full_name
        if (
            user.department
            and awaiting_field_code != "department"
            and (draft is None or draft.department is None)
        ):
            values["department"] = user.department
        return IntakeFieldUpdate(values=values, source=UpdateSource.SYSTEM)

    def _category_candidates(
        self,
        active: PersistentIntakeStepResult | None,
        latest_text: str | None = None,
    ) -> tuple[str, ...]:
        if active is None:
            return ()
        procurement_type = active.intake_result.draft.procurement_type
        expected_prefix = (
            "G"
            if procurement_type == "goods"
            else "S"
            if procurement_type == "service"
            else None
        )
        stored = active.dialog_state.intake_conversation.category_candidates
        if stored:
            conversation = active.dialog_state.intake_conversation
            state_type = conversation.category_procurement_type
            if state_type is not None and procurement_type != state_type:
                return ()
            if (
                conversation.category_decomposition_fingerprint is not None
                and conversation.decomposition_fingerprint is not None
                and conversation.category_decomposition_fingerprint
                != conversation.decomposition_fingerprint
            ):
                return ()
            expected_fingerprint = conversation.category_subject_fingerprint
            if (
                expected_fingerprint is not None
                and procurement_type is not None
                and expected_fingerprint
                != category_subject_fingerprint(
                    procurement_type.value,
                    active.intake_result.draft.item_name or "",
                )
            ):
                return ()
            return tuple(
                option.code
                for option in stored
                if option.selectable
                and (expected_prefix is None or option.code.startswith(expected_prefix))
            )
        question = active.intake_result.next_question
        if question is None or question.field_code != "category_code":
            return ()
        classification = self._categories.classify_draft(active.intake_result.draft)
        if classification.kind == "exact" and classification.category_code:
            return (classification.category_code,)
        if classification.candidates:
            return classification.candidates[:4]
        return self._derived_category_candidates(active, latest_text)

    def _resolve_category_for_update(
        self,
        update: IntakeFieldUpdate,
        active: PersistentIntakeStepResult | None,
        source_text: str,
        *,
        should_resolve: bool,
    ) -> CategoryResolution | None:
        if not should_resolve or self._category_resolver is None:
            return None
        previous = (
            active.dialog_state.intake_conversation if active is not None else None
        )
        include_current = _category_relevant_reply(source_text)
        context = build_category_resolution_context(
            active.intake_result.draft if active is not None else None,
            update,
            source_text,
            include_current_text=include_current,
        )
        if context is None:
            return None
        if (
            previous is not None
            and previous.category_context_fingerprint == context.fingerprint
            and previous.reason_code == "category_candidates_missing"
        ):
            return None
        return self._category_resolver.resolve(
            context.procurement_type,
            context.item_name,
            context.source_text,
            context_fingerprint=context.fingerprint,
        )

    def _handle_llm_category_confirmation(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult | None,
        key: str,
        message_id: int,
        text: str,
    ) -> TelegramIntakeOutcome | None:
        if active is None:
            return None
        state = active.dialog_state.intake_conversation
        options = [
            option
            for option in state.category_candidates
            if option.source == "llm_exact" and option.selectable
        ]
        normalized = " ".join(text.casefold().replace("ё", "е").split())
        if len(options) != 1 or normalized not in {"да", "подтвердить", "верно"}:
            if len(options) == 1 and normalized in {
                "выбрать другую",
                "другая",
                "нет",
            }:
                result = self._orchestrator.process_structured_step(
                    user.user_id,
                    IntakeFieldUpdate(),
                    request_id=active.request_id,
                    incoming_message=self._envelope(
                        message_id, "telegram_category_reject"
                    ),
                    idempotency_key=key,
                    intake_conversation=IntakeConversationState(),
                )
                outcome = TelegramIntakeOutcome(
                    "Уточните назначение или опишите предмет закупки подробнее.",
                    key,
                    IntakeFieldUpdate(),
                    result,
                    reason_code="category_candidates_missing",
                )
                self._remember_message_outcome(key, outcome)
                return outcome
            return None
        option = options[0]
        draft = active.intake_result.draft
        assert draft.procurement_type is not None
        update = IntakeFieldUpdate(
            values={"category_code": option.code},
            source=UpdateSource.USER,
            evidence_by_field={
                "category_code": category_confirmation_evidence(
                    draft.procurement_type.value,
                    draft.item_name or "",
                    option.code,
                    category_draft_context_fingerprint(draft),
                )
            },
            answered_field_code="category_code",
        )
        result = self._orchestrator.process_structured_step(
            user.user_id,
            update,
            request_id=active.request_id,
            incoming_message=self._envelope(
                message_id, "telegram_category_confirmed"
            ),
            idempotency_key=key,
            intake_conversation=IntakeConversationState(),
        )
        outcome = TelegramIntakeOutcome(
            format_intake_result(result, self._category_candidates(result)),
            key,
            update,
            result,
            card_actions(result),
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _start_multi_category_split(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult | None,
        key: str,
        message_id: int,
        text: str,
    ) -> TelegramIntakeOutcome | None:
        decomposition = decompose_procurement_needs(text)
        if decomposition.kind == "goods_plus_service":
            candidates = [
                ProcurementItemCandidate(
                    item_name=need.subject,
                    procurement_type=need.procurement_type,
                    category_code=need.category_code,
                    category_label=(
                        CATEGORY_NAMES[need.category_code]
                        if need.category_code is not None
                        else None
                    ),
                    category_candidates=list(need.category_candidates),
                    action=need.action,
                    evidence=need.evidence,
                    relation=need.relation,
                    quantity=(
                        str(need.quantity) if need.quantity is not None else None
                    ),
                )
                for need in decomposition.needs
            ]
            extracted = self._parser.parse(text)
            preserved = {
                field: value
                for field, value in extracted.values.items()
                if field
                not in {
                    "procurement_type",
                    "item_name",
                    "title",
                    "description",
                    "category_code",
                    "quantity",
                    "unit",
                    "specifications",
                    "desired_result",
                }
            }
            if decomposition.common_context is not None:
                preserved.setdefault(
                    "delivery_location", decomposition.common_context
                )
            state = IntakeConversationState(
                item_candidates=candidates,
                split_required=True,
                decomposition_kind=decomposition.kind,
                decomposition_fingerprint=decomposition.fingerprint,
                original_description=text,
                reason_code="multi_category_split_required",
            )
            result = self._orchestrator.process_structured_step(
                user.user_id,
                IntakeFieldUpdate(values=preserved),
                request_id=active.request_id if active is not None else None,
                incoming_message=self._envelope(message_id, "telegram_split"),
                idempotency_key=key,
                intake_conversation=state,
            )
            response = _mixed_need_split_response(candidates)
            outcome = TelegramIntakeOutcome(
                response,
                key,
                IntakeFieldUpdate(values=preserved),
                result,
                reason_code="multi_category_split_required",
            )
            self._remember_message_outcome(key, outcome)
            return outcome

        items = extract_procurement_items(text)
        if len({item.category_code for item in items}) < 2:
            return None
        candidates = [
            ProcurementItemCandidate(
                item_name=item.item_name,
                procurement_type=item.procurement_type,
                category_code=item.category_code,
                category_label=CATEGORY_NAMES[item.category_code],
                quantity=str(item.quantity) if item.quantity is not None else None,
            )
            for item in items
        ]
        extracted = self._parser.parse(text)
        preserved = {
            field: value
            for field, value in extracted.values.items()
            if field
            not in {
                "item_name",
                "title",
                "description",
                "category_code",
                "quantity",
                "unit",
            }
        }
        preserved.setdefault("procurement_type", "goods")
        state = IntakeConversationState(
            item_candidates=candidates,
            split_required=True,
            decomposition_kind="multiple_goods",
            decomposition_fingerprint=decomposition.fingerprint,
            reason_code="multi_category_split_required",
        )
        result = self._orchestrator.process_structured_step(
            user.user_id,
            IntakeFieldUpdate(values=preserved),
            request_id=active.request_id if active is not None else None,
            incoming_message=self._envelope(message_id, "telegram_split"),
            idempotency_key=key,
            intake_conversation=state,
        )
        categories = " и ".join(
            _short_category_label(candidate.category_code) for candidate in candidates
        )
        subjects = " или ".join(candidate.item_name for candidate in candidates)
        response = (
            f"В описании указаны товары из разных категорий: {categories}. "
            "Их нужно оформить отдельными заявками. "
            f"С чего начнём: с {subjects}?"
        )
        outcome = TelegramIntakeOutcome(
            response,
            key,
            IntakeFieldUpdate(values=preserved),
            result,
            reason_code="multi_category_split_required",
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _start_software_scope_resolution(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult | None,
        key: str,
        message_id: int,
        text: str,
    ) -> TelegramIntakeOutcome | None:
        scope = classify_software_procurement_scope(text)
        if scope not in {"ambiguous", "mixed"}:
            return None
        extracted = self._parser.parse(text)
        preserved = {
            field: value
            for field, value in extracted.values.items()
            if field not in {"procurement_type", "category_code"}
        }
        if scope == "mixed":
            preserved.pop("item_name", None)
            preserved.setdefault("description", text)
            state = _software_split_state(text)
            response = _software_split_response()
            reason_code = "multi_category_split_required"
        else:
            state = IntakeConversationState(
                category_candidates=[
                    CategoryCandidateOption(
                        code="G05",
                        label=CATEGORY_NAMES["G05"],
                        source="derived",
                        selectable=True,
                        readiness_eligible=True,
                    ),
                    CategoryCandidateOption(
                        code="S05",
                        label=CATEGORY_NAMES["S05"],
                        source="derived",
                        selectable=True,
                        readiness_eligible=True,
                    ),
                ],
                category_step_id=f"software-scope:{key}",
                category_question_fingerprint=sha256(
                    _SOFTWARE_SCOPE_QUESTION.encode("utf-8")
                ).hexdigest()[:16],
                category_clarification_kind="software_acquisition_scope",
                original_description=text,
                reason_code="software_scope_clarification_required",
            )
            response = _SOFTWARE_SCOPE_QUESTION
            reason_code = "software_scope_clarification_required"
        update = IntakeFieldUpdate(values=preserved)
        result = self._orchestrator.process_structured_step(
            user.user_id,
            update,
            request_id=active.request_id if active is not None else None,
            incoming_message=self._envelope(message_id, "telegram_software_scope"),
            idempotency_key=key,
            intake_conversation=state,
        )
        outcome = TelegramIntakeOutcome(
            response,
            key,
            update,
            result,
            reason_code=reason_code,
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _handle_software_scope_reply(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult,
        key: str,
        message_id: int,
        text: str,
    ) -> TelegramIntakeOutcome:
        scope = normalize_software_scope_reply(text)
        if scope == "mixed":
            state = _software_split_state(
                active.dialog_state.intake_conversation.original_description or ""
            )
            result = self._orchestrator.process_structured_step(
                user.user_id,
                IntakeFieldUpdate(),
                request_id=active.request_id,
                incoming_message=self._envelope(
                    message_id, "telegram_software_scope_mixed"
                ),
                idempotency_key=key,
                intake_conversation=state,
            )
            outcome = TelegramIntakeOutcome(
                _software_split_response(),
                key,
                IntakeFieldUpdate(),
                result,
                reason_code="multi_category_split_required",
            )
            self._remember_message_outcome(key, outcome)
            return outcome
        if scope in {"product", "service"}:
            values = {
                "procurement_type": "goods" if scope == "product" else "service",
                "category_code": "G05" if scope == "product" else "S05",
            }
            current_type = active.intake_result.draft.procurement_type
            update = IntakeFieldUpdate(
                values=values,
                explicit_correction=(
                    current_type is not None
                    and current_type.value != values["procurement_type"]
                ),
            )
            result = self._orchestrator.process_structured_step(
                user.user_id,
                update,
                request_id=active.request_id,
                incoming_message=self._envelope(
                    message_id, "telegram_software_scope_resolved"
                ),
                idempotency_key=key,
                intake_conversation=IntakeConversationState(),
            )
            outcome = TelegramIntakeOutcome(
                format_intake_result(result, self._category_candidates(result)),
                key,
                update,
                result,
                card_actions(result),
            )
            self._remember_message_outcome(key, outcome)
            return outcome
        previous = active.dialog_state.intake_conversation
        state = previous.model_copy(
            update={
                "category_clarification_repeats": (
                    previous.category_clarification_repeats + 1
                )
            }
        )
        result = self._orchestrator.process_structured_step(
            user.user_id,
            IntakeFieldUpdate(),
            request_id=active.request_id,
            incoming_message=self._envelope(
                message_id, "telegram_software_scope_retry"
            ),
            idempotency_key=key,
            intake_conversation=state,
        )
        outcome = TelegramIntakeOutcome(
            _SOFTWARE_SCOPE_QUESTION,
            key,
            IntakeFieldUpdate(),
            result,
            reason_code="software_scope_clarification_required",
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _handle_split_selection(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult,
        key: str,
        message_id: int,
        text: str,
    ) -> TelegramIntakeOutcome:
        candidates = active.dialog_state.intake_conversation.item_candidates
        selected = _selected_item(text, candidates)
        if selected is None:
            subjects = " или ".join(item.item_name for item in candidates)
            outcome = TelegramIntakeOutcome(
                f"Выберите, с чего начать: {subjects}.",
                key,
                IntakeFieldUpdate(),
                active,
                reason_code="multi_category_split_required",
            )
            self._remember_message_outcome(key, outcome)
            return outcome
        values: dict[str, object] = {
            "procurement_type": selected.procurement_type,
            "item_name": selected.item_name,
        }
        if selected.category_code is not None:
            values["category_code"] = selected.category_code
        if selected.quantity is not None:
            values["quantity"] = selected.quantity
        update = IntakeFieldUpdate(values=values)
        result = self._orchestrator.process_structured_step(
            user.user_id,
            update,
            request_id=active.request_id,
            incoming_message=self._envelope(message_id, "telegram_split_selection"),
            idempotency_key=key,
            intake_conversation=IntakeConversationState(),
        )
        outcome = TelegramIntakeOutcome(
            f"Начинаем с позиции «{selected.item_name}».\n\n"
            + format_intake_result(result, self._category_candidates(result)),
            key,
            update,
            result,
            card_actions(result),
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _handle_category_clarification_command(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult | None,
        key: str,
        message_id: int,
        text: str,
        candidates: tuple[str, ...],
    ) -> TelegramIntakeOutcome | None:
        if not _category_help_requested(text):
            return None
        if active is None:
            return None
        resolved = candidates or self._fallback_category_candidates(active, text)
        reason = (
            "category_candidates_missing"
            if not candidates
            else "repeated_category_clarification"
        )
        state = self._category_state(active, resolved, reason_code=reason)
        result = self._orchestrator.process_structured_step(
            user.user_id,
            IntakeFieldUpdate(),
            request_id=active.request_id,
            incoming_message=self._envelope(message_id, "telegram_category_help"),
            idempotency_key=key,
            intake_conversation=state,
        )
        outcome = TelegramIntakeOutcome(
            _format_category_candidates(resolved),
            key,
            IntakeFieldUpdate(),
            result,
            reason_code=reason,
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _category_parse_failure(
        self,
        user: ResolvedTelegramUser,
        active: PersistentIntakeStepResult | None,
        key: str,
        message_id: int,
        candidates: tuple[str, ...],
    ) -> TelegramIntakeOutcome:
        if active is None:
            return TelegramIntakeOutcome(
                "Опишите предмет закупки, чтобы подобрать категории.",
                key,
                IntakeFieldUpdate(),
                reason_code="category_candidates_missing",
            )
        resolved = candidates or self._fallback_category_candidates(active)
        previous = active.dialog_state.intake_conversation
        repeats = previous.category_clarification_repeats + 1
        reason = (
            "repeated_category_clarification"
            if candidates or repeats >= 2
            else "category_candidates_missing"
        )
        state = self._category_state(
            active,
            resolved,
            reason_code=reason,
            repeats=repeats,
        )
        result = self._orchestrator.process_structured_step(
            user.user_id,
            IntakeFieldUpdate(),
            request_id=active.request_id,
            incoming_message=self._envelope(message_id, "telegram_category_retry"),
            idempotency_key=key,
            intake_conversation=state,
        )
        suffix = (
            "\n\nЕсли ни один вариант не подходит, вернитесь к описанию предмета."
            if reason == "repeated_category_clarification"
            else ""
        )
        outcome = TelegramIntakeOutcome(
            _format_category_candidates(resolved) + suffix,
            key,
            IntakeFieldUpdate(),
            result,
            reason_code=reason,
        )
        self._remember_message_outcome(key, outcome)
        return outcome

    def _fallback_category_candidates(
        self,
        active: PersistentIntakeStepResult,
        latest_text: str | None = None,
    ) -> tuple[str, ...]:
        derived = self._derived_category_candidates(active, latest_text)
        if derived:
            return derived
        return ()

    def _derived_category_candidates(
        self,
        active: PersistentIntakeStepResult,
        latest_text: str | None,
    ) -> tuple[str, ...]:
        draft = active.intake_result.draft
        source = " ".join(
            value
            for value in (
                draft.item_name,
                draft.description,
                draft.specifications,
                draft.desired_result,
                latest_text,
            )
            if value
        )
        classification = self._categories.classify(source, draft.procurement_type)
        if classification.kind == "exact" and classification.category_code:
            return (classification.category_code,)
        return classification.candidates[:4]

    @staticmethod
    def _category_state(
        active: PersistentIntakeStepResult,
        candidates: tuple[str, ...],
        *,
        reason_code: str | None = None,
        repeats: int | None = None,
    ) -> IntakeConversationState:
        previous = active.dialog_state.intake_conversation
        question = active.intake_result.next_question
        fingerprint = (
            sha256(question.text.encode("utf-8")).hexdigest()[:16]
            if question is not None
            else None
        )
        strong_persisted_codes = {
            option.code
            for option in previous.category_candidates
            if option.selectable and option.readiness_eligible
        }
        reuse_persisted = bool(candidates and set(candidates) <= strong_persisted_codes)
        options = (
            [
                option.model_copy(deep=True)
                for option in previous.category_candidates
                if option.code in candidates
            ]
            if reuse_persisted
            else [
                CategoryCandidateOption(
                    code=code,
                    label=CATEGORY_NAMES[code],
                    source="derived",
                    selectable=True,
                    readiness_eligible=True,
                )
                for code in candidates
            ]
        )
        return IntakeConversationState(
            category_candidates=options,
            category_step_id=previous.category_step_id
            or f"category:{active.request_version}",
            category_procurement_type=(
                active.intake_result.draft.procurement_type.value
                if active.intake_result.draft.procurement_type is not None
                else None
            ),
            category_subject_fingerprint=previous.category_subject_fingerprint,
            category_context_fingerprint=previous.category_context_fingerprint,
            category_decomposition_fingerprint=(
                previous.decomposition_fingerprint
            ),
            decomposition_fingerprint=previous.decomposition_fingerprint,
            category_question_fingerprint=fingerprint,
            category_clarification_repeats=(
                previous.category_clarification_repeats if repeats is None else repeats
            ),
            reason_code=reason_code,
        )

    def _active_or_none(self, user_id: UUID) -> PersistentIntakeStepResult | None:
        try:
            return self._orchestrator.get_active_session(user_id)
        except ActiveDraftNotFoundError:
            return None

    def _should_use_structured_extraction(
        self,
        *,
        original_missing: bool,
        active: PersistentIntakeStepResult | None,
        question,
        text: str,
    ) -> bool:
        if self._extraction_mode == "rule" or self._structured_extractor is None:
            return False
        if original_missing:
            return True
        if (
            active is not None
            and active.dialog_state.intake_status == IntakeStatus.EDITING
        ):
            return True
        if question is None:
            return False
        if question.field_code == "category_code":
            return _category_relevant_reply(text)
        if question.question_type == "free_text":
            return True
        return len(text.split()) >= 5

    def _remember_extraction(self, key: str, update: IntakeFieldUpdate) -> None:
        if len(self._extraction_cache) >= 1024:
            self._extraction_cache.pop(next(iter(self._extraction_cache)))
        self._extraction_cache[key] = update.model_copy(deep=True)

    def _remember_message_outcome(
        self,
        key: str,
        outcome: TelegramIntakeOutcome,
    ) -> None:
        if len(self._message_outcome_cache) >= 1024:
            self._message_outcome_cache.pop(next(iter(self._message_outcome_cache)))
        self._message_outcome_cache[key] = outcome


def _safe_scalar_values(update: IntakeFieldUpdate) -> dict[str, object]:
    return {
        field_name: value
        for field_name, value in update.values.items()
        if field_name in _DEBUG_SCALAR_FIELDS
    }


def _category_relevant_reply(text: str) -> bool:
    """Exclude scalar/administrative replies from category retries."""
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    if not normalized or _category_help_requested(text):
        return False
    if re.fullmatch(r"[\d\s.,]+(?:р|руб(?:лей)?|тыс\.?)?", normalized):
        return False
    if re.fullmatch(
        r"(?:да|нет|предусмотрена|не предусмотрена|не знаю)(?: бюджетом)?",
        normalized,
    ):
        return False
    if re.fullmatch(r"(?:до|к)?\s*\d{1,2}\s+[а-я]+", normalized):
        return False
    return len(re.findall(r"[а-яa-z]+", normalized)) >= 3


def _category_help_requested(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    return any(
        phrase in normalized
        for phrase in (
            "какие варианты",
            "какие могут быть варианты",
            "какие есть подходящие варианты",
            "покажи варианты",
            "покажи категории",
            "дай список",
            "перечисли категории",
            "я не знаю номеров",
            "назови варианты",
            "что можно выбрать",
            "не знаю категорию",
        )
    )


def _format_category_candidates(candidates: tuple[str, ...]) -> str:
    if not candidates:
        return (
            "Не удалось уверенно определить категорию. Уточните назначение "
            "или опишите предмет закупки подробнее."
        )
    options = "\n".join(
        f"{index}. {CATEGORY_NAMES[code]} ({code})"
        for index, code in enumerate(candidates[:4], start=1)
    )
    clarification = ""
    if {"G05", "S05"}.issubset(candidates):
        clarification = (
            "Уточните, что требуется: приобрести готовую лицензию, "
            "настроить готовое ПО или разработать/доработать систему.\n\n"
        )
    return (
        f"{clarification}Подходящие категории:\n{options}"
        "\n\nНапишите номер или название категории."
    )


def _format_llm_category_resolution(resolution: CategoryResolution) -> str:
    if resolution.decision == "llm_exact":
        code = resolution.candidates[0]
        return (
            f"Похоже, подходит категория «{CATEGORY_NAMES[code]} ({code})». "
            "Подтвердить категорию?\n\n• Да\n• Выбрать другую"
        )
    return _format_category_candidates(resolution.candidates)


def _selected_item(
    text: str,
    candidates: list[ProcurementItemCandidate],
) -> ProcurementItemCandidate | None:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    matches = [
        item
        for item in candidates
        if item.item_name in normalized
        or (
            item.category_code is not None
            and _short_category_label(item.category_code) in normalized
        )
        or (
            item.category_label is not None
            and item.category_label.casefold() in normalized
        )
        or (
            item.procurement_type == "goods"
            and any(word in normalized for word in ("товар", "поставк"))
        )
        or (
            item.procurement_type == "service"
            and any(word in normalized for word in ("услуг", "работ", "монтаж"))
        )
        or (item.category_code == "G05" and "лиценз" in normalized)
        or (
            item.category_code == "S05"
            and any(term in normalized for term in ("установ", "настрой"))
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _short_category_label(code: str) -> str:
    return {
        "G02": "мебель",
        "G03": "компьютерная техника",
        "G04": "IT-периферия",
    }.get(code, CATEGORY_NAMES[code].casefold())


def _software_split_state(original_description: str) -> IntakeConversationState:
    return IntakeConversationState(
        item_candidates=[
            ProcurementItemCandidate(
                item_name="лицензии на ПО",
                procurement_type="goods",
                category_code="G05",
                category_label=CATEGORY_NAMES["G05"],
            ),
            ProcurementItemCandidate(
                item_name="установка и настройка ПО",
                procurement_type="service",
                category_code="S05",
                category_label=CATEGORY_NAMES["S05"],
            ),
        ],
        category_candidates=[
            CategoryCandidateOption(
                code="G05",
                label=CATEGORY_NAMES["G05"],
                source="derived",
                selectable=True,
                readiness_eligible=True,
            ),
            CategoryCandidateOption(
                code="S05",
                label=CATEGORY_NAMES["S05"],
                source="derived",
                selectable=True,
                readiness_eligible=True,
            ),
        ],
        split_required=True,
        decomposition_kind="goods_plus_service",
        decomposition_fingerprint=sha256(
            original_description.casefold().encode("utf-8")
        ).hexdigest()[:16],
        original_description=original_description,
        reason_code="multi_category_split_required",
    )


def _software_split_response() -> str:
    return (
        "Приобретение лицензий относится к категории «ПО и лицензии» (G05), "
        "а установка и настройка — к категории «IT-разработка и поддержка» "
        "(S05). Потребности разных категорий нужно оформить отдельными "
        "заявками. С чего начнём: с приобретения лицензий или с установки?"
    )


def _mixed_need_split_response(
    candidates: list[ProcurementItemCandidate],
) -> str:
    lines = ["В описании я вижу две отдельные потребности:"]
    for index, candidate in enumerate(candidates, start=1):
        type_label = "товар" if candidate.procurement_type == "goods" else "услуга"
        lines.append(f"{index}. {candidate.item_name.capitalize()} — {type_label}.")
    lines.extend(
        (
            "",
            "По правилам их нужно оформить отдельными заявками.",
            "С какой начнём?",
        )
    )
    return "\n".join(lines)


def _has_confident_idle_intake_signal(
    text: str,
    update: IntakeFieldUpdate | None,
) -> bool:
    if update is not None and (
        {
            "procurement_type",
            "category_code",
            "quantity",
            "unit",
            "amount",
            "budget_status",
            "delivery_location",
        }
        & set(update.values)
    ):
        return True
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    return bool(
        re.match(
            r"^(?:(?:мне|нам)\s+)?(?:куп\w*|закуп\w*|приобрет\w*|закаж\w*)\b",
            normalized,
        )
        or re.match(r"^[а-я-]+(?:ть|ти|чь)\s+\S+", normalized)
    )


def _user_context(user: ResolvedTelegramUser | UUID) -> ResolvedTelegramUser:
    if isinstance(user, UUID):
        return ResolvedTelegramUser(user_id=user, full_name="")
    return user


def _conflict_resolution(text: str) -> str | None:
    normalized = " ".join(text.casefold().replace("ё", "е").strip(' .,!?:;«»"').split())
    if normalized in _ACCEPT_CONFLICT_REPLIES:
        return "accept"
    if normalized in _KEEP_CONFLICT_REPLIES:
        return "keep"
    return None
