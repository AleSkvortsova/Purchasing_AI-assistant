import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.rag.question_understanding import understand_regulation_question

HOLDOUT_CASES = Path("data/evaluation/regulation_qa_holdout_cases.json")


def test_holdout_dataset_is_separate_and_typed() -> None:
    cases = json.loads(HOLDOUT_CASES.read_text(encoding="utf-8"))
    assert len(cases) == 25
    assert all("expected_intents" in case for case in cases)
    assert all("expected_slots" in case for case in cases)
    assert all("required_claims" in case for case in cases)
    assert all("forbidden_claims" in case for case in cases)
    assert all("outside_kb" in case for case in cases)


@pytest.mark.parametrize(
    ("question", "intent", "amount", "budget_status"),
    [
        (
            "У нас заявка на 175 тысяч, расходы предусмотрены планом. "
            "Через кого она должна пройти?",
            "approval_route",
            Decimal("175000"),
            "budgeted",
        ),
        (
            "Стоимость услуги 500 001 рубль, деньги заложены. "
            "Какой маршрут согласования?",
            "approval_route",
            Decimal("500001"),
            "budgeted",
        ),
        (
            "Покупка на 180 тысяч, но я не знаю, предусмотрена ли она "
            "бюджетом. Кто будет согласовывать?",
            "approval_route",
            Decimal("180000"),
            "unknown",
        ),
    ],
)
def test_approval_slots_are_phrase_independent(
    question: str,
    intent: str,
    amount: Decimal,
    budget_status: str,
) -> None:
    result = understand_regulation_question(question)
    assert result.primary_intent == intent
    assert result.amount == amount
    assert result.budget_status == budget_status


@pytest.mark.parametrize(
    ("question", "days", "relative"),
    [
        ("Товар нужен сегодня.", 0, "today"),
        ("Товар нужен завтра.", 1, "tomorrow"),
        ("Товар нужен послезавтра.", 2, "day_after_tomorrow"),
        ("Товар нужен через 8 дней.", 8, "in_days"),
        ("Товар нужен через три недели.", 21, "in_weeks"),
        ("Товар нужен через месяц.", 30, "in_month"),
    ],
)
def test_relative_deadlines_are_typed(
    question: str,
    days: int,
    relative: str,
) -> None:
    result = understand_regulation_question(question)
    assert result.duration_days == days
    assert result.relative_deadline == relative


@pytest.mark.parametrize(
    ("question", "intent", "status_name", "category"),
    [
        (
            "Закупщик вернул заявку. Как понять, что исправить?",
            "status_explanation",
            "requires_rework",
            None,
        ),
        (
            "Можно ли снять заявку после начала работы?",
            "request_cancellation",
            None,
            None,
        ),
        (
            "Мы хотим подключить CRM к телефонии. Какие данные нужны?",
            "category_classification",
            None,
            "S05",
        ),
        (
            "Что указать для перевозки груза?",
            "category_classification",
            None,
            "S03",
        ),
        (
            "Где посмотреть недавно отправленную заявку?",
            "draft_and_history",
            None,
            None,
        ),
    ],
)
def test_content_questions_do_not_end_as_generic(
    question: str,
    intent: str,
    status_name: str | None,
    category: str | None,
) -> None:
    result = understand_regulation_question(question)
    assert result.primary_intent == intent
    assert result.status_name == status_name
    assert result.category_hint == category


@pytest.mark.parametrize(
    "question",
    [
        "Кто лучший поставщик мебели?",
        "Кого посоветуете как перевозчика?",
        "Какого подрядчика выбрать?",
        "Кто сейчас предлагает лучшие условия? Какой поставщик?",
    ],
)
def test_supplier_recommendations_are_outside_kb(question: str) -> None:
    result = understand_regulation_question(question)
    assert result.primary_intent == "supplier_recommendation"
    assert result.outside_kb_intent is True


@pytest.mark.parametrize(
    ("question", "pattern"),
    [
        (
            "Кто это должен согласовать?",
            r"сумму закупки.*бюджет",
        ),
        (
            "Что мне указать?",
            r"товар или услугу.*предмет закупки",
        ),
    ],
)
def test_ambiguous_followups_get_slot_based_clarification(
    question: str,
    pattern: str,
) -> None:
    result = understand_regulation_question(question)
    assert result.primary_intent == "ambiguous_followup"
    assert result.requires_clarification is True
    assert result.clarifying_question is not None
    assert re.search(pattern, result.clarifying_question, re.IGNORECASE)


@pytest.mark.parametrize(
    "question",
    [
        "Какие данные мне заполнить?",
        "Какие сведения обязательны?",
    ],
)
def test_required_fields_without_subject_requests_slot_clarification(
    question: str,
) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "required_fields"
    assert result.requires_clarification is True
    assert result.missing_required_context == ("purchase_subject", "purchase_type")


def test_general_help_paraphrase_returns_options() -> None:
    result = understand_regulation_question("Дай краткий обзор правил закупок")

    assert result.primary_intent == "general_help"


@pytest.mark.parametrize(
    "question",
    [
        "Бюджетный статус неизвестен. Какой маршрут согласования нужен?",
        "Какой маршрут согласования, если статус бюджета пока неизвестен?",
        "Чьё согласование нужно при неизвестном бюджетном статусе?",
    ],
)
def test_approval_action_has_priority_over_budget_status_word(question: str) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "approval_route"
    assert "status_explanation" not in result.intents


@pytest.mark.parametrize(
    "question",
    [
        "Какой сейчас статус заявки?",
        "Что означает статус «Требует доработки»?",
        "Какие бывают статусы заявки?",
        "Почему установлен статус «На согласовании»?",
    ],
)
def test_status_questions_require_status_action(question: str) -> None:
    assert (
        understand_regulation_question(question).primary_intent
        == "status_explanation"
    )


@pytest.mark.parametrize(
    "question",
    [
        "Что значит, что заявка на согласовании?",
        "Заявка ушла на согласование — что сейчас происходит?",
        "У заявки статус «На согласовании». Мне нужно что-то делать?",
        "Кто сейчас рассматривает заявку, если она на согласовании?",
    ],
)
def test_on_approval_status_paraphrases(question: str) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "status_explanation"
    assert result.status_name == "на согласовании"


@pytest.mark.parametrize(
    "question",
    [
        "Можно отменить заявку со статусом «На согласовании»?",
        "Можно снять заявку, если она уже в работе?",
        "Потребность исчезла после передачи заявки в закупки.",
        "Можно ли остановить закупку на согласовании?",
        "Заявка больше не нужна, хотя уже передана.",
    ],
)
def test_cancellation_has_priority_over_status_words(question: str) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "request_cancellation"


@pytest.mark.parametrize(
    "question",
    [
        "Заявки, которые я подавала раньше",
        "Что я уже отправляла?",
        "Мои предыдущие заявки",
        "Старые заявки",
        "Ранее созданные заявки",
        "История моих заявок",
        "Посмотреть прошлые обращения",
        "Что я подавала до этого?",
    ],
)
def test_history_task_paraphrases(question: str) -> None:
    assert (
        understand_regulation_question(question).primary_intent
        == "draft_and_history"
    )


def test_urgency_fields_do_not_add_category_intent_only_from_event_hint() -> None:
    result = understand_regulation_question(
        "Презентация нужна через 12 дней. Будет ли заявка срочной и что "
        "дополнительно написать?"
    )

    assert result.intents == ("urgency_policy", "required_fields")


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        (
            "До выставки осталось пять дней. Что писать в заявке?",
            "urgency_policy",
        ),
        ("Заявка сейчас у согласующих. Что это значит?", "status_explanation"),
        (
            "Потребность отпала, но заявку уже передали закупщику. "
            "Её ещё можно убрать?",
            "request_cancellation",
        ),
        (
            "осоветуйте надёжную транспортную компанию",
            "supplier_recommendation",
        ),
        ("Что мне делать с этой заявкой?", "ambiguous_followup"),
    ],
)
def test_manual_smoke_intents(question: str, intent: str) -> None:
    assert understand_regulation_question(question).primary_intent == intent


def test_plan_wording_is_recognized_as_budgeted_approval() -> None:
    result = understand_regulation_question(
        "У нас на это есть 240 тысяч в плане. Кто должен одобрить заявку?"
    )

    assert result.primary_intent == "approval_route"
    assert result.amount == Decimal("240000")
    assert result.budget_status == "budgeted"
    assert result.requires_clarification is False


def test_event_days_and_required_fields_are_both_preserved() -> None:
    result = understand_regulation_question(
        "До выставки осталось пять дней. Что писать в заявке?"
    )

    assert result.intents == ("urgency_policy", "required_fields")
    assert result.duration_days == 5
    assert result.category_hint == "S07"
    assert result.purchase_subject == "мероприятие"


@pytest.mark.parametrize(
    "subject",
    [
        "мероприятие",
        "конференция",
        "форум",
        "семинар",
        "выставка",
        "презентация",
        "корпоративное событие",
        "организация площадки",
        "деловая встреча с внешней организацией",
    ],
)
def test_procurement_event_ontology_uses_s07(subject: str) -> None:
    result = understand_regulation_question(
        f"{subject.capitalize()} будет через 20 дней. Это срочная заявка?"
    )

    assert result.primary_intent == "urgency_policy"
    assert result.category_hint == "S07"
    assert result.purchase_subject == "мероприятие"
    assert result.purchase_type == "service"


@pytest.mark.parametrize(
    "subject",
    ["созвон", "внутреннее совещание", "обычная встреча"],
)
def test_internal_meetings_are_not_procurement_events(subject: str) -> None:
    result = understand_regulation_question(f"{subject} будет через 20 дней")

    assert result.category_hint is None


@pytest.mark.parametrize(
    "question",
    [
        "Можно сохранить незаполненную заявку?",
        "Хочу оставить незаконченную заявку",
        "Можно вернуться к заполнению позднее?",
        "Продолжу заполнять потом",
        "Пока не знаю все данные",
        "Можно заполнить частично?",
        "Не хочу отправлять заявку сейчас",
    ],
)
def test_draft_intent_is_task_oriented(question: str) -> None:
    assert (
        understand_regulation_question(question).primary_intent
        == "draft_and_history"
    )


@pytest.mark.parametrize(
    "question",
    [
        "Где последние отправленные заявки?",
        "Покажите недавние заявки",
        "Где посмотреть, что я подавала?",
        "Какую заявку я недавно отправила?",
        "Где мои прошлые заявки?",
        "Хочу посмотреть ранее созданные заявки",
    ],
)
def test_history_intent_is_task_oriented(question: str) -> None:
    assert (
        understand_regulation_question(question).primary_intent
        == "draft_and_history"
    )


def test_current_request_is_not_history() -> None:
    result = understand_regulation_question("Где моя текущая активная заявка?")

    assert result.primary_intent != "draft_and_history"


@pytest.mark.parametrize(
    ("question", "secondary"),
    [
        (
            "Какие параметры нужны для перевозки восьми контейнеров?",
            "required_fields",
        ),
        (
            "К какой категории относится интеграция и какие поля заполнить?",
            "required_fields",
        ),
        (
            "Будет ли заявка срочной и что дополнительно указать?",
            "required_fields",
        ),
    ],
)
def test_secondary_intents_follow_explicit_second_action(
    question: str,
    secondary: str,
) -> None:
    result = understand_regulation_question(question)

    assert secondary in result.secondary_intents


def test_approval_synonym_without_slots_requests_clarification() -> None:
    result = understand_regulation_question("Чьё одобрение понадобится?")

    assert result.primary_intent == "approval_route"
    assert result.requires_clarification is True


def test_partial_fill_is_a_draft_task() -> None:
    result = understand_regulation_question(
        "Я заполню часть заявки сейчас, а остальное добавлю позже"
    )

    assert result.primary_intent == "draft_and_history"
