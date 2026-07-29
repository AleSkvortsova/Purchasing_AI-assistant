import re
from datetime import date
from decimal import Decimal

from aiogram.types import InlineKeyboardMarkup

from app.bot.keyboards import conflict_actions, ready_actions, unresolved_actions
from app.bot.normalization import parse_amount_evidence
from app.intake.field_registry import CATEGORY_NAMES
from app.intake.models import (
    IntakeStatus,
    NextQuestion,
    ProcurementType,
    RequestDraftData,
)
from app.intake_persistence.models import PersistentIntakeStepResult

TELEGRAM_MESSAGE_LIMIT = 4096
_SAFE_MESSAGE_LIMIT = TELEGRAM_MESSAGE_LIMIT - 96
WELCOME_TEXT = (
    "Здравствуйте! Я помогу оформить заявку в отдел закупок.\n\n"
    "Расскажите, что нужно приобрести или заказать, укажите количество или "
    "объём, основные требования, срок и ориентировочную сумму. Чем подробнее "
    "описание, тем быстрее мы оформим заявку."
)
ACTIVE_DRAFT_NOTICE = (
    "У вас уже есть незавершённая заявка. Можно продолжить её через пункт "
    "«Текущая заявка» или начать заново через «Новая заявка»."
)
NEW_REQUEST_PROMPT = (
    "Опишите новую потребность в свободной форме. Укажите всё, что уже знаете: "
    "что нужно, количество или объём, основные требования, срок и "
    "ориентировочную сумму."
)
EXAMPLES_TEXT = (
    "Чем подробнее вы опишете потребность сразу, тем меньше уточнений "
    "потребуется.\n\n"
    "Товар\n\n"
    "Нужно купить 10 эргономичных офисных кресел с регулируемой высотой и "
    "поддержкой спины. Товар нужен до 20 августа, ориентировочная сумма — не "
    "более 120 000 ₽. Поставка в офис в Санкт-Петербурге. Закупка нужна для "
    "оснащения новых рабочих мест.\n\n"
    "Услуга\n\n"
    "Нужно организовать еженедельную уборку офиса площадью 500 м² с 1 "
    "сентября. Уборка нужна по будням после 19:00, ожидаемый результат — "
    "чистые рабочие зоны, кухня и санузлы. Ориентировочная сумма — до "
    "80 000 ₽ в месяц. Место оказания услуги — офис в Санкт-Петербурге."
)
HELP_TEXT = (
    "Как работает Бот Закупкин:\n\n"
    "1. Опишите, что нужно приобрести или заказать.\n"
    "2. Я уточню недостающие сведения.\n"
    "3. Вы проверите готовую карточку.\n"
    "4. После подтверждения заявка будет зарегистрирована.\n\n"
    "Полезно сразу указать количество или объём, основные требования, срок, "
    "место и ориентировочную сумму. Заявку можно изменить до регистрации, а "
    "незавершённую — отменить. Зарегистрированная заявка больше не "
    "редактируется в Telegram MVP."
)
READY_TEXT = (
    "Данные собраны. Откройте «Текущая заявка», чтобы проверить карточку "
    "и доступные действия."
)

_OPTION_LABELS = {
    "goods": "Товар",
    "service": "Услуга",
    "budgeted": "Предусмотрена бюджетом",
    "unbudgeted": "Не предусмотрена бюджетом",
    "unknown": "Не знаю",
    "true": "Да",
    "false": "Нет",
}


def format_question(
    question: NextQuestion,
    procurement_type: ProcurementType | str | None = None,
    category_candidates: tuple[str, ...] = (),
) -> str:
    type_value = _type_value(procurement_type)
    text = _contextual_question(question, type_value)
    options = _presentation_options(question, category_candidates)
    parts = [text]
    if options:
        parts.append("Варианты:\n" + "\n".join(f"• {item}" for item in options))
    return _limit("\n\n".join(parts))


def format_intake_result(
    result: PersistentIntakeStepResult,
    category_candidates: tuple[str, ...] = (),
) -> str:
    intake = result.intake_result
    if result.persistence_status == "partial_failure":
        return "Черновик создан, но ответ не сохранился. Повторите сообщение."
    if intake.status == IntakeStatus.READY_FOR_CONFIRMATION:
        return format_request_card(result)
    if intake.status == IntakeStatus.CONFLICT:
        if intake.next_question is not None:
            rendered = format_question(
                intake.next_question,
                intake.draft.procurement_type,
                category_candidates,
            )
            return _limit("Нужно уточнить данные.\n\n" + rendered)
        return "В данных есть противоречие. Уточните последнее сообщение."
    if intake.next_question is not None:
        return format_question(
            intake.next_question,
            intake.draft.procurement_type,
            category_candidates,
        )
    return "Данные сохранены."


def card_actions(result: PersistentIntakeStepResult) -> InlineKeyboardMarkup | None:
    intake = result.intake_result
    if intake.status == IntakeStatus.CONFLICT and intake.draft.conflicts:
        return conflict_actions(result.request_id, result.request_version)
    if (
        intake.status != IntakeStatus.READY_FOR_CONFIRMATION
        or result.dialog_state.intake_status == IntakeStatus.EDITING
    ):
        return None
    route = intake.approval_route
    if route is not None and route.status == "resolved":
        return ready_actions(result.request_id, result.request_version)
    if intake.draft.budget_status == "unknown":
        return unresolved_actions(result.request_id, result.request_version)
    return None


def format_request_card(result: PersistentIntakeStepResult) -> str:
    intake = result.intake_result
    card = intake.request_card
    if card is None:
        return READY_TEXT
    draft = intake.draft
    fields = {
        field.code: field
        for section in card.sections
        for field in section.fields
        if field.code != "approver"
    }
    values = {code: field.display_value for code, field in fields.items()}
    is_goods = draft.procurement_type == ProcurementType.GOODS
    lines = ["Проверьте заявку перед отправкой:", ""]
    lines.append(f"Тип закупки: {'Товар' if is_goods else 'Услуга'}")
    category = _category_display(draft.category_code)
    if category:
        lines.append(f"Категория: {category}")
    _append(
        lines,
        "Что требуется" if is_goods else "Какая услуга требуется",
        _sentence(values.get("item_name")),
    )
    quantity = _quantity_display(draft)
    _append(lines, "Количество" if is_goods else "Количество или объём", quantity)
    if is_goods:
        _append(lines, "Характеристики товара", values.get("specifications"))
    else:
        requirements = _distinct_service_requirements(
            draft,
            None,
            values.get("desired_result"),
            specifications=values.get("specifications"),
        )
        _append(lines, "Объём и требования", requirements)
        _append(lines, "Ожидаемый результат", values.get("desired_result"))
    _append(lines, "Ориентировочная сумма", _amount_display(fields.get("amount")))
    _append(lines, "Бюджет", _budget_display(draft.budget_status))
    _append(
        lines,
        "Желаемая дата поставки" if is_goods else "Срок оказания услуги",
        _date_display(draft.desired_delivery_date),
    )
    _append(
        lines,
        "Место поставки" if is_goods else "Место оказания услуги",
        values.get("delivery_location"),
    )
    _append(lines, "Цель закупки", values.get("business_justification"))
    _append(lines, "Подразделение", values.get("department"))
    _append(lines, "Контактное лицо", values.get("contact_person"))
    if draft.budget_status == "unknown":
        lines.extend(
            [
                "",
                "Бюджетный статус нужно уточнить. Пока он не определён, "
                "заявку нельзя зарегистрировать, потому что от него зависит "
                "маршрут согласования.",
            ]
        )
    return _limit("\n".join(lines))


def format_current_summary(result: PersistentIntakeStepResult) -> str:
    if result.dialog_state.intake_status == IntakeStatus.EDITING:
        return (
            format_request_card(result)
            + "\n\nЗаявка находится в режиме редактирования. Напишите изменение "
            "свободным текстом."
        )
    if result.intake_result.status == IntakeStatus.READY_FOR_CONFIRMATION:
        return format_request_card(result)
    draft = result.intake_result.draft
    lines = ["Сейчас в заявке указано:"]
    _append(lines, "Тип", _type_display(draft.procurement_type))
    _append(lines, "Потребность", draft.item_name)
    _append(lines, "Категория", _category_display(draft.category_code))
    _append(lines, "Количество", _quantity_display(draft))
    _append(lines, "Сумма", _draft_amount_display(draft))
    question = result.intake_result.next_question
    if question is not None:
        lines.extend(
            [
                "",
                "Следующий шаг:",
                format_question(question, draft.procurement_type),
            ]
        )
    return _limit("\n".join(lines))


def presentation_label(value: str) -> str:
    return _OPTION_LABELS.get(value.casefold(), value)


def _contextual_question(question: NextQuestion, procurement_type: str | None) -> str:
    if question.field_code == "procurement_type":
        return "Это товар или услуга?"
    if question.field_code == "budget_status":
        return "Эта закупка предусмотрена в утверждённом бюджете?"
    if question.field_code == "amount":
        return "Укажите ориентировочную сумму закупки. Например: 120 000 ₽."
    if question.field_code == "department":
        return "Какое подразделение оформляет заявку?"
    if question.field_code == "business_justification":
        return "Зачем нужна эта закупка?"
    if question.field_code == "category_code":
        if procurement_type == "goods":
            return "К какой категории относится товар?"
        if procurement_type == "service":
            return "К какой категории относится услуга?"
        return "Уточните категорию закупки."
    if procurement_type == "goods":
        return {
            "specifications": (
                "Какие характеристики товара важны? Например, размер, "
                "материал, цвет или другие требования."
            ),
            "delivery_location": "Куда нужно поставить товар?",
            "desired_delivery_date": (
                "К какой дате нужен товар? Можно написать, например: "
                "20 августа или через 10 дней."
            ),
        }.get(question.field_code, question.text)
    if procurement_type == "service":
        return {
            "description": (
                "Опишите, какой результат нужен и какие требования важны "
                "для услуги."
            ),
            "specifications": (
                "Опишите, какой результат нужен и какие требования важны "
                "для услуги."
            ),
            "delivery_location": "Где должна быть оказана услуга?",
            "desired_delivery_date": (
                "К какой дате нужна услуга или результат? Можно написать, "
                "например: 20 августа или через 2 недели."
            ),
        }.get(question.field_code, question.text)
    return question.text


def _presentation_options(
    question: NextQuestion,
    category_candidates: tuple[str, ...],
) -> tuple[str, ...]:
    if question.field_code == "procurement_type":
        return ("Товар", "Услуга")
    if question.field_code == "budget_status":
        return ("Да, предусмотрена", "Нет, не предусмотрена", "Не знаю")
    if question.field_code == "category_code":
        if not category_candidates:
            return ()
        return tuple(
            f"{index}. {CATEGORY_NAMES[code]}"
            for index, code in enumerate(category_candidates[:4], start=1)
        )
    if question.question_type == "boolean":
        return ("Да", "Нет")
    labels: list[str] = []
    for option in question.options:
        label = presentation_label(option)
        if label not in labels:
            labels.append(label)
    return tuple(labels)


def _type_value(value: ProcurementType | str | None) -> str | None:
    if isinstance(value, ProcurementType):
        value = value.value
    return value


def _type_display(value: ProcurementType | str | None) -> str | None:
    normalized = _type_value(value)
    if normalized == "goods":
        return "Товар"
    if normalized == "service":
        return "Услуга"
    return None


def _category_display(code: str | None) -> str | None:
    if not code:
        return None
    name = CATEGORY_NAMES.get(code)
    return f"{name} ({code})" if name else code


def _quantity_display(draft: RequestDraftData) -> str | None:
    if draft.quantity is None:
        return None
    value = _decimal_display(draft.quantity)
    return f"{value} {draft.unit}" if draft.unit else value


def _amount_display(field) -> str | None:
    if field is None:
        return None
    value = field.display_value
    modifier = field.metadata.get("amount_modifier", "exact")
    prefix = {"maximum": "не более ", "approximate": "около "}.get(
        modifier, ""
    )
    suffix = {
        "per_month": " в месяц",
        "per_quarter": " в квартал",
        "per_year": " в год",
    }.get(field.metadata.get("billing_period"), "")
    return f"{prefix}{value}{suffix}".strip()


def _draft_amount_display(draft: RequestDraftData) -> str | None:
    if draft.amount is None:
        return None
    state = draft.field_states.get("amount")
    metadata = parse_amount_evidence(state.evidence if state else None)
    modifier = metadata.get("amount_modifier", "exact")
    prefix = {"maximum": "не более ", "approximate": "около "}.get(
        modifier, ""
    )
    suffix = {
        "per_month": " в месяц",
        "per_quarter": " в квартал",
        "per_year": " в год",
    }.get(metadata.get("billing_period"), "")
    currency = "₽" if draft.currency == "RUB" else draft.currency
    return f"{prefix}{_decimal_display(draft.amount)} {currency}{suffix}"


def _budget_display(value: str | None) -> str | None:
    return {
        "budgeted": "Предусмотрена",
        "unbudgeted": "Не предусмотрена",
        "unknown": "Требуется уточнение",
    }.get(value) if value else None


def _date_display(value: date | None) -> str | None:
    if value is None:
        return None
    months = (
        "",
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{value.day} {months[value.month]} {value.year}"


def _decimal_display(value: Decimal) -> str:
    formatted = f"{value:,.2f}".replace(",", " ")
    return formatted.rstrip("0").rstrip(".")


def _append(lines: list[str], label: str, value: object | None) -> None:
    if value is not None and str(value).strip():
        lines.append(f"{label}: {value}")


def _distinct_service_requirements(
    draft: RequestDraftData,
    item_name: str | None,
    desired_result: str | None,
    **values: str | None,
) -> str | None:
    references = [value for value in (item_name, desired_result) if value]
    candidates = [
        (code, value)
        for code, value in values.items()
        if value
    ]
    candidates.sort(
        key=lambda item: (
            not _is_user_confirmed(draft, item[0]),
            tuple(values).index(item[0]),
        )
    )
    result: list[str] = []
    for _, value in candidates:
        for segment in _requirement_segments(value):
            segment = _remove_literal_reference(segment, item_name)
            if not segment:
                continue
            if any(_is_reference_only(segment, reference) for reference in references):
                continue
            if any(_semantically_same(segment, existing) for existing in result):
                continue
            result.append(segment.strip(" ,.;:—–-"))
    if not result:
        return None
    return "; ".join(_sentence(value) or value for value in result) + "."


def _remove_literal_reference(value: str, reference: str | None) -> str:
    if not reference:
        return value
    return re.sub(
        re.escape(reference),
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    ).strip(" ,.;:—–-")


def _is_user_confirmed(draft: RequestDraftData, field_code: str) -> bool:
    state = draft.field_states.get(field_code)
    return bool(state and state.source == "user" and state.confirmed)


def _requirement_segments(value: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(r"[.;]+|,\s+", value)
        if segment.strip()
    ]


def _semantically_same(left: str, right: str) -> bool:
    left_normalized = _comparison_text(left)
    right_normalized = _comparison_text(right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return True
    left_tokens = _semantic_tokens(left_normalized)
    right_tokens = _semantic_tokens(right_normalized)
    if not left_tokens or not right_tokens:
        return False
    smaller, larger = sorted((left_tokens, right_tokens), key=len)
    return len(smaller) >= 2 and smaller <= larger


def _is_reference_only(value: str, reference: str) -> bool:
    value_normalized = _comparison_text(value)
    reference_normalized = _comparison_text(reference)
    if value_normalized == reference_normalized:
        return True
    value_tokens = _semantic_tokens(value_normalized)
    reference_tokens = _semantic_tokens(reference_normalized)
    return bool(value_tokens) and value_tokens <= reference_tokens


def _semantic_tokens(value: str) -> set[str]:
    value = re.sub(r"\b(?:в\s+)?утренн\w*(?:\s+час\w*)?\b|\bутром\b", "утро", value)
    value = re.sub(r"\b(?:в\s+)?вечерн\w*(?:\s+час\w*)?\b|\bвечером\b", "вечер", value)
    stop_words = {"в", "во", "на", "и", "с", "со", "по", "для", "нужно"}
    return {token for token in value.split() if token not in stop_words}


def _comparison_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(
        "".join(character if character.isalnum() else " " for character in value)
        .casefold()
        .split()
    )


def _sentence(value: str | None) -> str | None:
    if not value:
        return None
    return value[:1].upper() + value[1:]


def _limit(text: str) -> str:
    if len(text) <= _SAFE_MESSAGE_LIMIT:
        return text
    return text[: _SAFE_MESSAGE_LIMIT - 2].rstrip() + "…"
