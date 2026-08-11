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
        "Можно ли отменить уже начатую, но ещё не отправленную заявку?",
        "Как отменить черновик заявки?",
        "Я передумал, можно отказаться от ещё не отправленной заявки?",
        "Можно удалить начатую заявку?",
    ],
)
def test_draft_cancellation_uses_action_and_target_without_distance_limit(
    question: str,
) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "request_cancellation"
    assert result.requires_clarification is False
    assert result.missing_required_context == ()


@pytest.mark.parametrize(
    ("question", "expected_intent"),
    [
        ("Что означает статус Отменена?", "status_explanation"),
        ("Кто может отменить заявку после отправки?", "request_cancellation"),
    ],
)
def test_cancellation_action_is_distinct_from_cancelled_status(
    question: str,
    expected_intent: str,
) -> None:
    assert understand_regulation_question(question).primary_intent == expected_intent


@pytest.mark.parametrize(
    ("personal", "internal"),
    [
        (
            "Я хочу купить себе домой новый холодильник. Какую марку лучше выбрать?",
            "Нужно купить холодильник в офисную кухню. Можно ли указать марку?",
        ),
        (
            "Хочу купить ноутбук себе. Какой бренд выбрать?",
            "Нужно купить ноутбук сотруднику. Какой бренд можно указать?",
        ),
        (
            "Нужен принтер домой. Какие требования указать?",
            "Нужен принтер для бухгалтерии. Какие требования указать?",
        ),
    ],
)
def test_personal_and_internal_purpose_are_distinguished(
    personal: str,
    internal: str,
) -> None:
    personal_result = understand_regulation_question(personal)
    internal_result = understand_regulation_question(internal)

    assert personal_result.domain_decision == "outside_domain"
    assert internal_result.domain_decision == "known_domain_intent"


def test_conflicting_personal_and_org_purpose_requires_clarification() -> None:
    result = understand_regulation_question(
        "Хочу купить ноутбук себе для рабочего места в офисе. Что указать?"
    )

    assert result.domain_decision == "ambiguous_domain"
    assert result.requires_clarification is True
    assert "личного" in result.clarifying_question.casefold()


@pytest.mark.parametrize(
    "question",
    [
        "Что означает статус «На согласовании»?",
        "Кто согласует закупку на 180 тысяч рублей?",
        "Какая заявка считается срочной?",
    ],
)
def test_canonical_regulation_intents_do_not_require_org_purpose(question: str) -> None:
    assert (
        understand_regulation_question(question).domain_decision
        == "known_domain_intent"
    )


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


@pytest.mark.parametrize(
    "question",
    [
        "Можно ли начать оформлять заявку, сохранить её как черновик и "
        "закончить позднее?",
        "Можно сохранить незаполненную заявку и вернуться к ней позже?",
        "Если я не закончу заявку сейчас, смогу продолжить завтра?",
        "Черновик заявки сохраняется?",
        "Как продолжить ранее начатую заявку?",
        "Можно начать заявку сегодня и закончить потом?",
    ],
)
def test_draft_actions_have_priority_over_draft_status_literal(question: str) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "draft_and_history"
    assert result.domain_decision == "known_domain_intent"


@pytest.mark.parametrize(
    "question",
    [
        "Что означает статус Черновик?",
        "Какие переходы доступны из статуса Черновик?",
    ],
)
def test_explicit_draft_status_questions_remain_status_questions(question: str) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "status_explanation"
    assert result.status_name == "черновик"
    assert result.domain_decision == "known_domain_intent"


@pytest.mark.parametrize(
    "question",
    [
        "Подскажи рецепт борща на четыре порции.",
        "Какая погода завтра в Москве?",
        "Кто сыграл главную роль в фильме «Ирония судьбы»?",
        "Напиши функцию на Python для сортировки списка.",
        "Куда поехать отдыхать в сентябре?",
        "Как лечить простуду?",
    ],
)
def test_out_of_domain_questions_are_not_guessed_as_procurement(question: str) -> None:
    result = understand_regulation_question(question)

    assert result.primary_intent == "outside_domain"
    assert result.domain_decision == "outside_domain"


@pytest.mark.parametrize(
    "question",
    [
        "Какие продукты нужно указать в заявке на организацию мероприятия?",
        "Можно ли заказать продукты для корпоративного мероприятия?",
        "Какие сведения нужны для заявки на перевозку?",
        "Как оформить закупку мебели?",
        "Какая заявка считается срочной?",
        "Можно ли объединить товар и услугу?",
    ],
)
def test_near_domain_questions_pass_positive_domain_gate(question: str) -> None:
    result = understand_regulation_question(question)

    assert result.domain_decision != "outside_domain"
