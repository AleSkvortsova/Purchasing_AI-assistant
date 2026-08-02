import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.rag.answering import RegulationAnswer, RegulationQuestionAnsweringService
from app.rag.question_understanding import (
    RegulationQuestionIntent,
    RegulationQuestionUnderstanding,
    understand_regulation_question,
)
from app.rag.value_normalization import (
    BudgetStatus,
    detect_budget_status,
    normalize_money_amount,
    normalize_regulation_text,
)

PENDING_CLARIFICATION_TTL = timedelta(minutes=30)
MAX_CLARIFICATION_STEPS = 3
_UNKNOWN_BUDGET_ANSWER = (
    "Без бюджетного статуса нельзя однозначно определить маршрут согласования. "
    "Уточните, предусмотрена ли закупка бюджетом, у ответственного за бюджет "
    "подразделения или финансового контролёра."
)


class RegulationKnownSlots(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = None
    budget_status: BudgetStatus | None = None
    duration_days: int | None = None
    relative_deadline: str | None = None
    status_name: str | None = None
    purchase_subject: str | None = None
    purchase_type: Literal["goods", "service"] | None = None
    category_hint: str | None = None

    @classmethod
    def from_understanding(
        cls,
        value: RegulationQuestionUnderstanding,
    ) -> "RegulationKnownSlots":
        return cls(
            amount=value.amount,
            budget_status=value.budget_status,
            duration_days=value.duration_days,
            relative_deadline=value.relative_deadline,
            status_name=value.status_name,
            purchase_subject=value.purchase_subject,
            purchase_type=value.purchase_type,
            category_hint=value.category_hint,
        )


class RegulationPendingClarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_question: str
    primary_intent: RegulationQuestionIntent
    secondary_intents: tuple[RegulationQuestionIntent, ...] = ()
    known_slots: RegulationKnownSlots
    missing_slots: tuple[str, ...]
    clarifying_question: str
    clarification_step: int = Field(default=1, ge=1)
    last_clarifying_question_fingerprint: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        created = (
            self.created_at.replace(tzinfo=UTC)
            if self.created_at.tzinfo is None
            else self.created_at
        )
        return current - created > PENDING_CLARIFICATION_TTL


class RegulationConversationTurn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: RegulationAnswer
    pending: RegulationPendingClarification | None = None
    pending_replaced: bool = False
    expired_context: bool = False


def answer_regulation_turn(
    service: RegulationQuestionAnsweringService,
    text: str,
    pending: RegulationPendingClarification | None = None,
    *,
    now: datetime | None = None,
) -> RegulationConversationTurn:
    expired = pending is not None and pending.is_expired(now)
    if expired:
        pending = None
    if pending is None:
        result = service.answer(text)
        if expired:
            result.diagnostics["expired_context"] = True
        return RegulationConversationTurn(
            result=result,
            pending=_pending_from_question(text, result, now=now),
            expired_context=expired,
        )

    reply = understand_regulation_question(text)
    if _is_new_question(reply, text, pending):
        result = service.answer(text)
        return RegulationConversationTurn(
            result=result,
            pending=_pending_from_question(text, result, now=now),
            pending_replaced=True,
        )

    slots = pending.known_slots.model_copy(deep=True)
    _merge_reply_slots(slots, reply, text, pending.missing_slots)
    if (
        pending.primary_intent == "approval_route"
        and slots.budget_status == "unknown"
    ):
        result = RegulationAnswer(
            answer=_UNKNOWN_BUDGET_ANSWER,
            status="clarification_required",
            refusal_reason="unknown_budget_status",
            diagnostics={
                "retrieval_status": "not_called",
                "clarification_step": pending.clarification_step,
                "conversation_slots": _safe_slots(slots),
                "conversation_primary_intent": pending.primary_intent,
                "clarification_resolution": "unknown_budget_status",
            },
        )
        return RegulationConversationTurn(result=result)

    missing = _remaining_missing(
        pending.primary_intent,
        slots,
        pending.missing_slots,
    )
    if missing:
        question = _clarifying_question(missing)
        next_step = pending.clarification_step + 1
        fingerprint = _clarifying_question_fingerprint(question)
        previous_fingerprint = (
            pending.last_clarifying_question_fingerprint
            or _clarifying_question_fingerprint(pending.clarifying_question)
        )
        if fingerprint == previous_fingerprint or next_step > MAX_CLARIFICATION_STEPS:
            reason = (
                "repeated_clarification"
                if fingerprint == previous_fingerprint
                else "clarification_step_limit"
            )
            result = RegulationAnswer(
                answer=_clarification_limit_answer(pending.primary_intent),
                status="clarification_required",
                refusal_reason=reason,
                diagnostics={
                    "retrieval_status": "not_called",
                    "clarification_step": next_step,
                    "conversation_slots": _safe_slots(slots),
                    "conversation_primary_intent": pending.primary_intent,
                    "clarification_resolution": reason,
                },
            )
            return RegulationConversationTurn(result=result)
        result = RegulationAnswer(
            answer=question,
            status="clarification_required",
            clarifying_question=question,
            diagnostics={
                "retrieval_status": "not_called",
                "clarification_step": next_step,
                "conversation_slots": _safe_slots(slots),
                "conversation_primary_intent": pending.primary_intent,
            },
        )
        return RegulationConversationTurn(
            result=result,
            pending=pending.model_copy(
                update={
                    "known_slots": slots,
                    "missing_slots": missing,
                    "clarifying_question": question,
                    "clarification_step": next_step,
                    "last_clarifying_question_fingerprint": fingerprint,
                }
            ),
        )

    resolved_question = _resolved_question(pending, slots)
    result = service.answer(resolved_question)
    result.diagnostics["clarification_resolved"] = True
    result.diagnostics["clarification_step"] = pending.clarification_step
    result.diagnostics["conversation_slots"] = _safe_slots(slots)
    result.diagnostics["conversation_primary_intent"] = pending.primary_intent
    result.diagnostics["conversation_secondary_intents"] = list(
        pending.secondary_intents
    )
    return RegulationConversationTurn(
        result=result,
        pending=_pending_from_question(resolved_question, result, now=now),
    )


def _pending_from_question(
    question: str,
    result: RegulationAnswer,
    *,
    now: datetime | None,
) -> RegulationPendingClarification | None:
    if result.status != "clarification_required":
        return None
    understanding = understand_regulation_question(question)
    if not understanding.missing_required_context:
        return None
    clarifying_question = result.clarifying_question or result.answer
    return RegulationPendingClarification(
        original_question=question,
        primary_intent=understanding.primary_intent,
        secondary_intents=understanding.secondary_intents,
        known_slots=RegulationKnownSlots.from_understanding(understanding),
        missing_slots=understanding.missing_required_context,
        clarifying_question=clarifying_question,
        last_clarifying_question_fingerprint=(
            _clarifying_question_fingerprint(clarifying_question)
        ),
        created_at=now or datetime.now(UTC),
    )


def _merge_reply_slots(
    slots: RegulationKnownSlots,
    reply: RegulationQuestionUnderstanding,
    text: str,
    missing_slots: tuple[str, ...],
) -> None:
    amount = normalize_money_amount(text)
    if amount is not None:
        slots.amount = amount
    budget_status = detect_budget_status(text)
    if "budget_status" in missing_slots:
        budget_status = budget_status or _budget_clarification_value(text)
    if budget_status is not None:
        slots.budget_status = budget_status
    for name in (
        "duration_days",
        "relative_deadline",
        "status_name",
        "purchase_subject",
        "purchase_type",
        "category_hint",
    ):
        value = getattr(reply, name)
        if value is not None:
            setattr(slots, name, value)


def _budget_clarification_value(text: str) -> BudgetStatus | None:
    value = normalize_regulation_text(text).strip(" .!?«»\"")
    if re.search(r"\b(?:не знаю|неизвестно|пока не знаю)\b", value):
        return "unknown"
    if re.search(r"\b(?:нет|не\s+предусмотрен\w*)\b", value):
        return "unbudgeted"
    if re.search(r"\b(?:да|предусмотрен\w*)\b", value):
        return "budgeted"
    if re.fullmatch(
        r"(?:да|предусмотрена|предусмотрено|предусмотрена бюджетом|"
        r"закупка предусмотрена бюджетом)",
        value,
    ):
        return "budgeted"
    if re.fullmatch(
        r"(?:нет|не предусмотрена|не предусмотрено|не предусмотрена бюджетом|"
        r"закупка не предусмотрена бюджетом)",
        value,
    ):
        return "unbudgeted"
    if re.fullmatch(r"(?:не знаю|неизвестно|пока не знаю)", value):
        return "unknown"
    if re.search(r"\bпредусмотрен\w*\b", value) and not re.search(
        r"\bне\s+предусмотрен", value
    ):
        return "budgeted"
    return None


def _remaining_missing(
    primary_intent: RegulationQuestionIntent,
    slots: RegulationKnownSlots,
    requested_missing: tuple[str, ...],
) -> tuple[str, ...]:
    if primary_intent == "approval_route":
        missing = []
        if slots.amount is None:
            missing.append("amount")
        if slots.budget_status is None:
            missing.append("budget_status")
        return tuple(missing)
    if "status_name" in requested_missing:
        return ("status_name",) if slots.status_name is None else ()
    if primary_intent in {"required_fields", "ambiguous_followup"}:
        missing = []
        if slots.purchase_subject is None:
            missing.append("purchase_subject")
        if slots.purchase_type is None:
            missing.append("purchase_type")
        return tuple(missing)
    return ()


def _clarifying_question(missing: tuple[str, ...]) -> str:
    if missing == ("budget_status",):
        return (
            "Уточните, пожалуйста, предусмотрена ли закупка бюджетом. "
            "От этого зависит маршрут согласования."
        )
    if missing == ("amount",):
        return "Уточните сумму закупки."
    if set(missing) == {"amount", "budget_status"}:
        return "Уточните сумму закупки и предусмотрена ли она бюджетом."
    if missing == ("purchase_subject",):
        return "Уточните, что именно вы хотите закупить."
    if missing == ("purchase_type",):
        return "Уточните, это товар или услуга."
    if missing == ("status_name",):
        return "Уточните текущий статус заявки или опишите, что с ней произошло."
    return (
        "Уточните, что вы хотите закупить: товар или услугу, и кратко "
        "опишите предмет закупки."
    )


def _resolved_question(
    pending: RegulationPendingClarification,
    slots: RegulationKnownSlots,
) -> str:
    base_question = pending.original_question
    if pending.primary_intent == "approval_route":
        base_question = "Какой маршрут согласования закупки?"
    additions: list[str] = []
    if slots.amount is not None:
        additions.append(f"Сумма закупки {slots.amount} рублей.")
    if slots.budget_status == "budgeted":
        additions.append("Закупка предусмотрена бюджетом.")
    elif slots.budget_status == "unbudgeted":
        additions.append("Закупка не предусмотрена бюджетом.")
    if slots.purchase_type == "goods":
        additions.append("Тип закупки: товар.")
    elif slots.purchase_type == "service":
        additions.append("Тип закупки: услуга.")
    if slots.purchase_subject:
        additions.append(f"Предмет закупки: {slots.purchase_subject}.")
    if slots.status_name:
        additions.append(f"Статус заявки: {slots.status_name}.")
    return " ".join((base_question, *additions))


def _is_new_question(
    reply: RegulationQuestionUnderstanding,
    text: str,
    pending: RegulationPendingClarification,
) -> bool:
    normalized = normalize_regulation_text(text)
    fills_missing = _reply_fills_missing(reply, text, pending.missing_slots)
    if fills_missing:
        return bool(
            len(normalized.split()) > 4
            and reply.primary_intent
            not in {pending.primary_intent, "required_fields", "ambiguous_followup"}
            and _looks_like_complete_question(normalized)
        )
    if len(normalized.split()) <= 4:
        return False
    return reply.primary_intent not in {"required_fields", "ambiguous_followup"}


def _reply_fills_missing(
    reply: RegulationQuestionUnderstanding,
    text: str,
    missing: tuple[str, ...],
) -> bool:
    return any(
        (
            name == "amount"
            and normalize_money_amount(text) is not None
            or name == "budget_status"
            and (
                detect_budget_status(text) is not None
                or _budget_clarification_value(text) is not None
            )
            or name == "purchase_type"
            and reply.purchase_type is not None
            or name == "purchase_subject"
            and reply.purchase_subject is not None
            or name == "status_name"
            and reply.status_name is not None
        )
        for name in missing
    )


def _looks_like_complete_question(normalized: str) -> bool:
    return "?" in normalized or bool(
        re.search(
            r"\b(?:кто|что|как|когда|можно\s+ли|нужно\s+ли|почему)\b",
            normalized,
        )
    )


def _clarifying_question_fingerprint(question: str) -> str:
    normalized = normalize_regulation_text(question)
    return sha256(normalized.encode("utf-8")).hexdigest()


def _clarification_limit_answer(primary_intent: RegulationQuestionIntent) -> str:
    if primary_intent == "approval_route":
        return (
            "Без суммы и бюджетного статуса нельзя однозначно определить маршрут "
            "согласования. Задайте новый вопрос, указав сумму закупки и предусмотрена "
            "ли она бюджетом."
        )
    if primary_intent in {"required_fields", "ambiguous_followup"}:
        return (
            "Без описания ситуации нельзя дать точный ответ. Задайте новый вопрос, "
            "указав статус заявки или предмет закупки и что именно вы хотите узнать."
        )
    return "Не удалось уточнить контекст. Задайте новый вопрос с нужными деталями."


def _safe_slots(slots: RegulationKnownSlots) -> dict[str, str | int | None]:
    return {
        name: str(value) if isinstance(value, Decimal) else value
        for name, value in slots.model_dump().items()
        if value is not None
    }
