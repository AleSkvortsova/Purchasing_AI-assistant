from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.categories import DeterministicCategoryClassifier
from app.bot.extraction import DeterministicEntityExtractor
from app.bot.formatters import (
    format_question,
    format_request_card,
    presentation_label,
)
from app.bot.normalization import (
    NaturalDateParser,
    normalize_unit,
    parse_amount,
    parse_amount_expression,
    parse_cardinal,
)
from app.bot.parser import DeterministicIntakeParser, TelegramParseError
from app.bot.users import ResolvedTelegramUser
from app.extraction.openai_schema import OpenAIApprovalExtractionPayload
from app.intake.models import (
    CompletenessResult,
    IntakeFieldUpdate,
    IntakeStatus,
    IntakeStepResult,
    NextQuestion,
    ProcurementType,
    RequestDraftData,
    UpdateSource,
)
from app.intake.service import RequestIntakeService
from app.intake_persistence.models import (
    PersistentDialogState,
    PersistentIntakeStepResult,
)
from app.intake_persistence.repositories import InMemoryIntakePersistenceRepository
from app.intake_persistence.service import PersistentIntakeOrchestrator

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")


def _question(field_code: str, question_type: str = "free_text") -> NextQuestion:
    return NextQuestion(
        field_code=field_code,
        text="Техническая формулировка",
        question_type=question_type,
        reason="required",
        priority=1,
        options=["goods", "service"],
    )


def _persistent(draft: RequestDraftData, awaiting: str | None = None):
    next_question = _question(awaiting) if awaiting else None
    intake = IntakeStepResult(
        status=IntakeStatus.COLLECTING,
        draft=draft,
        completeness=CompletenessResult(
            is_complete=False,
            required_fields=[],
            completed_fields=[],
            missing_fields=[],
            invalid_fields=[],
            blocked_fields=[],
            completion_ratio=Decimal("0"),
        ),
        next_question=next_question,
    )
    return PersistentIntakeStepResult(
        request_id=REQUEST_ID,
        user_id=USER_ID,
        request_version=1,
        intake_result=intake,
        dialog_state=PersistentDialogState(
            user_id=USER_ID,
            request_id=REQUEST_ID,
            intake_status=IntakeStatus.COLLECTING,
            awaiting_field_code=awaiting,
            next_question=next_question,
            state_version=1,
        ),
        persistence_status="saved",
    )


def test_first_message_extracts_goods_fields_and_core_does_not_repeat_them() -> None:
    update = DeterministicEntityExtractor().extract(
        "Нужно купить 10 офисных кресел"
    ).to_update()

    assert update.values == {
        "procurement_type": "goods",
        "item_name": "офисные кресла",
        "quantity": Decimal("10"),
        "unit": "шт.",
        "category_code": "G02",
    }

    result = RequestIntakeService().process_step(RequestDraftData(), update)
    assert result.next_question is not None
    assert result.next_question.field_code == "specifications"
    assert result.next_question.field_code not in {
        "procurement_type",
        "item_name",
        "quantity",
        "unit",
        "category_code",
    }


def test_first_service_message_is_classified_without_goods_quantity() -> None:
    values = DeterministicEntityExtractor().extract(
        "Нужно заказать уборку офиса 3 раза в неделю"
    ).values

    assert values["procurement_type"] == "service"
    assert values["category_code"] == "S02"
    assert values["item_name"] == "уборку офиса"
    assert "quantity" not in values
    assert "unit" not in values


def test_first_message_extracts_date_amount_and_location_when_explicit() -> None:
    dates = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    extractor = DeterministicEntityExtractor(date_parser=dates)

    laptop = extractor.extract("Купить 5 ноутбуков до 15 августа").values
    assert laptop["desired_delivery_date"] == date(2026, 8, 15)
    assert laptop["item_name"] == "ноутбуки"
    assert laptop["category_code"] == "G03"

    chairs = extractor.extract(
        "Нужно купить кресла на сумму 120 тысяч руб. с доставкой в офис"
    ).values
    assert chairs["amount"] == Decimal("120000")
    assert chairs["delivery_location"] == "офис"


def test_procurement_type_question_exposes_only_goods_and_services() -> None:
    rendered = format_question(_question("procurement_type", "choice"))
    assert "Товар" in rendered
    assert "Услуга" in rendered
    assert "work" not in rendered
    assert "Работа" not in rendered


def test_budget_question_exposes_unknown_only_as_russian_label() -> None:
    rendered = format_question(_question("budget_status", "choice"))
    assert "Да, предусмотрена" in rendered
    assert "Нет, не предусмотрена" in rendered
    assert "Не знаю" in rendered
    assert "unknown" not in rendered


def test_telegram_parser_does_not_accept_work_as_new_type() -> None:
    with pytest.raises(TelegramParseError):
        DeterministicIntakeParser().parse(
            "work", _question("procurement_type", "choice")
        )


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("goods", "Товар"),
        ("service", "Услуга"),
        ("budgeted", "Предусмотрена бюджетом"),
        ("unbudgeted", "Не предусмотрена бюджетом"),
        ("true", "Да"),
        ("false", "Нет"),
    ],
)
def test_enum_values_have_russian_presentation(value: str, label: str) -> None:
    assert presentation_label(value) == label


@pytest.mark.parametrize(
    ("procurement_type", "expected"),
    [
        (ProcurementType.GOODS, "Куда нужно поставить товар?"),
        (ProcurementType.SERVICE, "Где должна быть оказана услуга?"),
    ],
)
def test_delivery_question_is_contextual(procurement_type, expected: str) -> None:
    assert format_question(
        _question("delivery_location"), procurement_type
    ).startswith(expected)


@pytest.mark.parametrize(
    ("field_code", "procurement_type", "expected"),
    [
        ("specifications", ProcurementType.GOODS, "Какие характеристики"),
        ("specifications", ProcurementType.SERVICE, "какой результат нужен"),
        ("amount", ProcurementType.GOODS, "ориентировочную сумму"),
        ("department", ProcurementType.GOODS, "Какое подразделение"),
        ("business_justification", ProcurementType.GOODS, "Зачем нужна"),
        ("desired_delivery_date", ProcurementType.GOODS, "через 10 дней"),
        ("desired_delivery_date", ProcurementType.SERVICE, "через 2 недели"),
    ],
)
def test_questions_use_friendly_contextual_text(
    field_code: str,
    procurement_type: ProcurementType,
    expected: str,
) -> None:
    assert expected in format_question(_question(field_code), procurement_type)


def test_category_classifier_exact_match() -> None:
    result = DeterministicCategoryClassifier().classify(
        "офисные кресла", ProcurementType.GOODS
    )
    assert result.kind == "exact"
    assert result.category_code == "G02"


@pytest.mark.parametrize(
    "text",
    [
        "Купить моющие средства для офиса",
        "Купить средства для уборки офиса",
        "Купить губки и салфетки",
        "Закупить хозяйственный инвентарь",
        "Купить чистящие средства",
    ],
)
def test_household_cleaning_goods_are_classified_as_g09(text: str) -> None:
    result = DeterministicCategoryClassifier().classify(
        text, ProcurementType.GOODS
    )

    assert result.kind == "exact"
    assert result.category_code == "G09"


def test_cleaning_goods_and_cleaning_service_are_not_mixed() -> None:
    classifier = DeterministicCategoryClassifier()

    goods = classifier.classify(
        "Купить средства для уборки офиса", ProcurementType.GOODS
    )
    service = classifier.classify(
        "Заказать уборку офиса", ProcurementType.SERVICE
    )

    assert goods.category_code == "G09"
    assert service.category_code == "S02"


def test_g09_requires_goods_context_and_meaningful_household_terms() -> None:
    classifier = DeterministicCategoryClassifier()

    assert classifier.classify("средства", ProcurementType.GOODS).kind == "none"
    assert (
        classifier.classify(
            "моющие средства", ProcurementType.SERVICE
        ).kind
        == "none"
    )


@pytest.mark.parametrize("text", ["ремонт офиса", "монтаж оборудования"])
def test_works_are_extracted_as_service_category(text: str) -> None:
    values = DeterministicEntityExtractor().extract(text).values
    assert values["procurement_type"] == "service"
    assert values["category_code"] == "S01"


@pytest.mark.parametrize(
    ("text", "item_name", "specifications"),
    [
        ("ремонт офиса площадью 200 м²", "ремонт офиса", "площадь 200 м²"),
        ("монтаж оборудования в серверной", "монтаж оборудования", "в серверной"),
        (
            "разработка интеграции CRM с корпоративной системой",
            "разработка интеграции CRM с корпоративной системой",
            None,
        ),
    ],
)
def test_service_subject_is_conservatively_separated_from_details(
    text: str,
    item_name: str,
    specifications: str | None,
) -> None:
    values = DeterministicEntityExtractor().extract(text).values
    assert values["item_name"] == item_name
    assert values.get("specifications") == specifications


def test_openai_approval_schema_does_not_offer_work() -> None:
    schema = OpenAIApprovalExtractionPayload.model_json_schema()
    procurement_type = schema["properties"]["procurement_type_raw"]
    offered = {
        value
        for branch in procurement_type["anyOf"]
        for value in branch.get("enum", [])
    }
    assert offered == {"goods", "service"}


def test_quantity_correction_uses_new_value_and_metadata() -> None:
    update = DeterministicEntityExtractor().extract(
        "Нужно не 10, а 12 кресел"
    ).to_update()

    assert update.values["quantity"] == 12
    assert update.explicit_correction is True
    assert update.corrections[0].model_dump() == {
        "operation": "replace",
        "target_field": "quantity",
        "old_value": 10,
        "new_value": 12,
    }


@pytest.mark.parametrize(
    "text",
    [
        "Вместо 10 — 12 кресел",
        "Поменяйте количество 10 на 12 кресел",
        "Количество будет 12 кресел",
    ],
)
def test_general_quantity_correction_phrases_keep_new_value(text: str) -> None:
    update = DeterministicEntityExtractor().extract(text).to_update()

    assert update.values["quantity"] == 12
    assert update.explicit_correction is True
    assert update.corrections[0].operation == "replace"
    assert update.corrections[0].target_field == "quantity"
    assert update.corrections[0].new_value == 12


def test_date_correction_uses_new_date() -> None:
    extractor = DeterministicEntityExtractor(
        date_parser=NaturalDateParser(today_provider=lambda: date(2026, 7, 29))
    )

    update = extractor.extract(
        "Дата не 20 августа, а 25 августа"
    ).to_update()

    assert update.values["desired_delivery_date"] == date(2026, 8, 25)
    assert update.corrections[0].old_value == date(2026, 8, 20)


def test_amount_correction_and_million_scale_use_decimal() -> None:
    corrected = DeterministicEntityExtractor().extract(
        "Сумма не 100 000, а 120 000 рублей"
    ).to_update()
    million = DeterministicEntityExtractor().extract(
        "Разработать интеграцию за 1,2 млн рублей"
    ).to_update()

    assert corrected.values["amount"] == 120000
    assert corrected.corrections[0].old_value == 100000
    assert million.values["amount"] == 1200000


def test_budget_correction_and_unknown_wording_are_normalized() -> None:
    corrected = DeterministicEntityExtractor().extract(
        "Бюджет не предусмотрен, я ошиблась — предусмотрен"
    ).to_update()
    unknown = DeterministicEntityExtractor().extract(
        "Нужно купить принтер, бюджет пока неизвестен"
    ).to_update()

    assert corrected.values["budget_status"] == "budgeted"
    assert corrected.corrections[0].target_field == "budget_status"
    assert unknown.values["budget_status"] == "unknown"


def test_word_quantity_in_countable_goods_infers_piece_unit() -> None:
    update = DeterministicEntityExtractor().extract(
        "Нужны два подшипника для погрузчика"
    ).to_update()

    assert update.values["quantity"] == 2
    assert update.values["unit"] == "шт."
    assert update.evidence_by_field["unit"] == "два подшипника"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("один", 1),
        ("одна", 1),
        ("одно", 1),
        ("одни", 1),
        ("одного", 1),
        ("двух", 2),
        ("трёх", 3),
        ("четырёх", 4),
        ("пятнадцать", 15),
        ("двадцать", 20),
        ("двадцать один", 21),
        ("тридцать пять", 35),
        ("пятьдесят", 50),
        ("сто", 100),
    ],
)
def test_safe_russian_cardinal_normalization(raw: str, expected: int) -> None:
    assert parse_cardinal(raw) == expected


@pytest.mark.parametrize("raw", ["первый", "второй", "третья", "двадцатого"])
def test_ordinal_words_are_not_cardinal_quantity(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_cardinal(raw)


@pytest.mark.parametrize(
    ("text", "quantity", "unit"),
    [
        ("Купить три банки", 3, "шт."),
        ("Закажите семь тарелок", 7, "шт."),
        ("Закажи пять лампочек", 5, "шт."),
        ("Закажите две клавиатуры", 2, "шт."),
        ("Закажите один принтер", 1, "шт."),
        ("Закажите 5 лампочек", 5, "шт."),
        ("Закажите 3 банки", 3, "шт."),
        ("Закажите 2 клавиатуры", 2, "шт."),
        ("Закажите 1 принтер", 1, "шт."),
        ("Купить две лампы", 2, "шт."),
        ("Купить пять кресел", 5, "шт."),
        ("Купить двадцать один монитор", 21, "шт."),
        ("Купить 3 литра воды", 3, "л"),
        ("Купить 5 кг краски", 5, "кг"),
        ("Купить 10 метров кабеля", 10, "м"),
        ("Купить 2 упаковки бумаги", 2, "упак."),
        ("Купить 3 комплекта мебели", 3, "комплект"),
    ],
)
def test_safe_goods_count_and_unit_extraction(
    text: str,
    quantity: int,
    unit: str,
) -> None:
    values = DeterministicEntityExtractor().extract(text).values

    assert values["quantity"] == quantity
    assert values["unit"] == unit


def test_capacity_is_kept_as_specification_not_total_quantity() -> None:
    jars = DeterministicEntityExtractor().extract(
        "Купить 3 банки объёмом 1 литр"
    ).values
    cans = DeterministicEntityExtractor().extract(
        "Купить 5 канистр по 10 литров"
    ).values

    assert jars["quantity"] == 3
    assert jars["unit"] == "шт."
    assert "1 литр" in jars["specifications"]
    assert cans["quantity"] == 5
    assert cans["unit"] == "шт."
    assert "10 литров" in cans["specifications"]


@pytest.mark.parametrize(
    "text",
    [
        "Купить мебель для офиса на 3 этаже",
        "Купить мебель до 15 августа",
        "Купить мебель, бюджет 150 тысяч рублей",
    ],
)
def test_non_quantity_numbers_do_not_create_quantity(text: str) -> None:
    assert "quantity" not in DeterministicEntityExtractor().extract(text).values


def test_multiple_goods_positions_are_not_summed_into_one_quantity() -> None:
    update = DeterministicEntityExtractor().extract(
        "Купить 3 шкафа и 2 стола"
    ).to_update()
    values = update.values

    assert "quantity" not in values
    assert "unit" not in values
    assert "3 шкафа и 2 стола" in values["item_name"]
    result = RequestIntakeService().process_step(RequestDraftData(), update)
    assert result.next_question.field_code == "quantity"
    assert "несколько товарных позиций" in result.next_question.text


def test_service_object_count_never_infers_goods_piece_unit() -> None:
    values = DeterministicEntityExtractor().extract(
        "Услуга по ремонту 5 кресел"
    ).values

    assert values["procurement_type"] == "service"
    assert "quantity" not in values
    assert "unit" not in values


def test_word_quantity_correction_uses_the_same_cardinal_normalizer() -> None:
    update = DeterministicEntityExtractor().extract(
        "Нужно не три, а четыре монитора"
    ).to_update()

    assert update.values["quantity"] == 4
    assert update.corrections[0].old_value == 3
    assert update.corrections[0].new_value == 4


def test_ambiguous_category_uses_short_restart_safe_candidate_list() -> None:
    classifier = DeterministicCategoryClassifier()
    classification = classifier.classify(
        "компьютерное оборудование", ProcurementType.GOODS
    )
    rendered = format_question(
        _question("category_code", "choice"),
        ProcurementType.GOODS,
        classification.candidates,
    )

    assert classification.kind == "multiple"
    assert classification.candidates == ("G03", "G04")
    assert rendered.count("\n") < 8
    assert "1. IT-оборудование" in rendered
    assert "2. IT-периферия" in rendered
    assert "G30" not in rendered

    parser = DeterministicIntakeParser(category_classifier=classifier)
    assert parser.parse(
        "2", _question("category_code", "choice"),
        category_candidates=classification.candidates,
    ).values == {"category_code": "G04"}


def test_unknown_category_does_not_show_full_registry() -> None:
    classifier = DeterministicCategoryClassifier()
    classification = classifier.classify("редкий нестандартный предмет")
    rendered = format_question(_question("category_code", "choice"))
    assert classification.kind == "none"
    assert "Уточните категорию закупки" in rendered
    assert "Офисные принадлежности" not in rendered


@pytest.mark.parametrize("raw", ["шт", "шт.", "штука", "штуки", "штук", "единиц"])
def test_piece_units_are_normalized(raw: str) -> None:
    assert normalize_unit(raw) == "шт."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-20", date(2026, 8, 20)),
        ("20.08.2026", date(2026, 8, 20)),
        ("20 августа", date(2026, 8, 20)),
        ("к 20 августа", date(2026, 8, 20)),
        ("до 20 августа", date(2026, 8, 20)),
    ],
)
def test_natural_dates(raw: str, expected: date) -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    assert parser.parse(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "с 1 сентября",
        "начиная с 1 сентября",
        "как я уже написала, с 1 сентября",
        "я писала, что нужно с 1 сентября",
        "нужно начиная с 1 сентября",
        "давайте с 1 сентября",
        "дата — 1 сентября",
        "срок: 1 сентября",
    ],
)
def test_natural_date_parser_finds_date_inside_safe_conversational_shell(
    raw: str,
) -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    assert parser.parse(raw) == date(2026, 9, 1)


def test_date_without_year_rolls_to_next_year_after_date_passed() -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 8, 21))
    assert parser.parse("20 августа") == date(2027, 8, 20)


@pytest.mark.parametrize(
    ("raw", "days"),
    [
        ("завтра", 1),
        ("послезавтра", 2),
        ("через 10 дней", 10),
        ("через 1 неделю", 7),
        ("через 2 недели", 14),
        ("через 5 недель", 35),
    ],
)
def test_relative_dates_use_injected_local_base_date(raw: str, days: int) -> None:
    base = date(2026, 7, 28)
    parser = NaturalDateParser(
        timezone_name="Europe/Moscow",
        today_provider=lambda: base,
    )
    assert parser.parse(raw) == date.fromordinal(base.toordinal() + days)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("через день", date(2026, 7, 29)),
        ("через два дня", date(2026, 7, 30)),
        ("через неделю", date(2026, 8, 4)),
        ("через две недели", date(2026, 8, 11)),
        ("через три недели", date(2026, 8, 18)),
        ("через месяц", date(2026, 8, 28)),
        ("через два месяца", date(2026, 9, 28)),
        ("через квартал", date(2026, 10, 28)),
    ],
)
def test_word_relative_dates_use_calendar_semantics(
    raw: str,
    expected: date,
) -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    assert parser.parse(raw) == expected


@pytest.mark.parametrize(
    ("base", "raw", "expected"),
    [
        (date(2026, 1, 31), "через месяц", date(2026, 2, 28)),
        (date(2027, 12, 31), "через месяц", date(2028, 1, 31)),
        (date(2024, 2, 28), "через день", date(2024, 2, 29)),
    ],
)
def test_relative_date_month_year_and_leap_boundaries(
    base: date,
    raw: str,
    expected: date,
) -> None:
    assert NaturalDateParser(today_provider=lambda: base).parse(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "чтобы товар был через неделю",
        "нужно доставить через две недели",
        "поставка нужна через 10 дней",
        "хочу получить товар через месяц",
        "работы завершить через три недели",
        "услуга нужна через один месяц",
    ],
)
def test_relative_deadline_is_found_inside_full_message(text: str) -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    found = parser.search(text)

    assert found is not None
    assert found[0] > date(2026, 7, 28)


@pytest.mark.parametrize(
    "text",
    [
        "не позднее 10 августа",
        "не позже 10 августа",
        "не позднее чем 10 августа",
        "крайний срок — 10 августа",
        "завершить не позднее 10 августа",
        "выполнить не позже 10 августа",
        "максимум до 10 августа",
        "Нужна заправка картриджей, не позднее 10 августа, бюджет 500 р.",
    ],
)
def test_deadline_qualifiers_are_found_in_short_and_long_messages(text: str) -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 7, 29))

    found = parser.search(text)

    assert found is not None
    assert found[0] == date(2026, 8, 10)


@pytest.mark.parametrize(
    "text",
    [
        "бюджет 80 000 рублей в месяц",
        "уборка раз в неделю",
        "обучение в течение недели",
        "результат нужен через пару недель",
        "доставить через несколько дней",
    ],
)
def test_non_deadline_periods_remain_without_relative_date(text: str) -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    assert parser.search(text) is None


def test_unsupported_relative_date_is_rejected() -> None:
    parser = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    with pytest.raises(ValueError):
        parser.parse("через пару недель")


def test_date_update_serializes_to_iso() -> None:
    parser = DeterministicIntakeParser(
        date_parser=NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    )
    update = parser.parse(
        "через 10 дней", _question("desired_delivery_date", "date")
    )
    assert update.model_dump(mode="json")["values"]["desired_delivery_date"] == (
        "2026-08-07"
    )


def test_long_answer_to_date_question_extracts_relative_deadline_span() -> None:
    parser = DeterministicIntakeParser(
        date_parser=NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    )
    update = parser.parse(
        "товар должен быть в офисе через неделю",
        _question("desired_delivery_date", "date"),
    )

    assert update.values["desired_delivery_date"] == date(2026, 8, 4)


def test_long_answer_to_quantity_question_accepts_word_cardinal() -> None:
    update = DeterministicIntakeParser().parse(
        "нужно двадцать один монитор",
        _question("quantity", "decimal"),
    )

    assert update.values["quantity"] == 21


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("120000", Decimal("120000")),
        ("120 000", Decimal("120000")),
        ("120 000 руб.", Decimal("120000")),
        ("120 тыс.", Decimal("120000")),
        ("120 тысяч", Decimal("120000")),
        ("500р", Decimal("500")),
        ("500 р", Decimal("500")),
        ("500р.", Decimal("500")),
        ("500 р.", Decimal("500")),
        ("500руб", Decimal("500")),
        ("500 руб.", Decimal("500")),
        ("500₽", Decimal("500")),
        ("500 ₽", Decimal("500")),
        ("1 500р", Decimal("1500")),
        ("1,5 тыс. р.", Decimal("1500")),
    ],
)
def test_natural_amounts(raw: str, expected: Decimal) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["500p", "модель принтера p500", "500раз"])
def test_amount_does_not_confuse_latin_p_or_part_of_word(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_amount(raw)


@pytest.mark.parametrize(
    ("raw", "modifier"),
    [
        ("120000", "exact"),
        ("120 000", "exact"),
        ("120 тыс.", "exact"),
        ("120 тысяч", "exact"),
        ("не более 120 тыс.", "maximum"),
        ("до 120 тысяч", "maximum"),
        ("максимум 120 000", "maximum"),
        ("около 120 000", "approximate"),
        ("примерно 120 тыс.", "approximate"),
        ("ориентировочно 120 000", "approximate"),
        ("порядка 120 тысяч", "approximate"),
    ],
)
def test_amount_expression_separates_value_and_modifier(
    raw: str,
    modifier: str,
) -> None:
    parsed = parse_amount_expression(raw)
    assert parsed.amount == Decimal("120000")
    assert parsed.modifier == modifier


@pytest.mark.parametrize(
    ("raw", "amount", "modifier", "period"),
    [
        ("до 80 000 рублей в месяц", Decimal("80000"), "maximum", "per_month"),
        (
            "около 300 тыс. рублей в год",
            Decimal("300000"),
            "approximate",
            "per_year",
        ),
        ("120 000 руб.", Decimal("120000"), "exact", None),
        ("80 000 рублей", Decimal("80000"), "exact", None),
        ("120 тысяч рублей", Decimal("120000"), "exact", None),
        ("80 тыс. руб.", Decimal("80000"), "exact", None),
    ],
)
def test_amount_expression_preserves_modifier_period_and_full_currency_word(
    raw: str,
    amount: Decimal,
    modifier: str,
    period: str | None,
) -> None:
    parsed = parse_amount_expression(raw)
    assert parsed.amount == amount
    assert parsed.modifier == modifier
    assert parsed.billing_period == period


@pytest.mark.parametrize(
    ("raw", "period"),
    [
        ("до 80 000 рублей в месяц", "per_month"),
        ("около 80 тысяч рублей в год", "per_year"),
        ("примерно 80 тыс. руб. в квартал", "per_quarter"),
    ],
)
def test_currency_span_removal_keeps_period_as_metadata_without_text_debris(
    raw: str,
    period: str,
) -> None:
    text = f"Нужно заказать уборку офиса, бюджет {raw}"
    extracted = DeterministicEntityExtractor().extract(text)
    combined = " ".join(str(value) for value in extracted.values.values()).casefold()

    assert "лей" not in combined
    assert "руб" not in combined
    assert "бюджет до" not in combined
    assert f"billing_period={period}" in extracted.evidence_by_field["amount"]


@pytest.mark.parametrize(
    "amount_phrase",
    [
        "80 000 рублей",
        "80 000 руб.",
        "80 тыс. рублей",
        "80 тысяч рублей",
        "до 80 000 рублей",
        "не более 80 000 рублей",
        "около 80 000 рублей",
        "примерно 80 тыс. руб.",
        "бюджет до 80 000 рублей",
        "ориентировочная сумма 80 000 рублей",
    ],
)
def test_complete_currency_phrase_is_removed_from_service_text(
    amount_phrase: str,
) -> None:
    extracted = DeterministicEntityExtractor().extract(
        f"Нужно заказать уборку офиса, {amount_phrase}"
    )
    text_values = " ".join(
        str(value) for value in extracted.values.values() if isinstance(value, str)
    ).casefold()

    assert extracted.values["amount"] == Decimal("80000")
    for debris in ("лей", "рубл", "руб.", "рублей", "бюджет до", "до лей"):
        assert debris not in text_values


def test_120_thousand_rubles_does_not_leave_currency_tail() -> None:
    extracted = DeterministicEntityExtractor().extract(
        "Нужно заказать уборку офиса за 120 тысяч рублей"
    )
    assert extracted.values["amount"] == Decimal("120000")
    assert "лей" not in str(extracted.values).casefold()


def test_service_smoke_message_is_split_and_rendered_without_debris() -> None:
    dates = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    parser = DeterministicIntakeParser(date_parser=dates)
    core = RequestIntakeService()
    source = (
        "Нужно заказать еженедельную уборку офиса площадью 500 м² "
        "с 1 сентября, бюджет до 80 000 рублей в месяц"
    )

    first_update = parser.parse(source)
    first = core.process_step(RequestDraftData(), first_update)
    draft = first.draft
    assert draft.procurement_type == ProcurementType.SERVICE
    assert draft.category_code == "S02"
    assert draft.item_name == "еженедельная уборка офиса"
    assert draft.specifications == "площадь 500 м²"
    assert draft.desired_delivery_date == date(2026, 9, 1)
    assert draft.amount == Decimal("80000")
    assert draft.field_states["amount"].evidence == (
        "amount_modifier=maximum; billing_period=per_month"
    )
    assert first.next_question is not None
    assert first.next_question.field_code not in {"amount", "desired_delivery_date"}

    requirements = parser.parse(
        "Нужно, чтобы качественно мылись полы, санузлы и кухня. "
        "Уборка после окончания рабочего дня",
        first.next_question,
    )
    second = core.process_step(draft, requirements)
    assert second.draft.item_name == "еженедельная уборка офиса"
    assert "мылись полы" in (second.draft.description or "")
    assert second.draft.amount == Decimal("80000")
    assert second.draft.desired_delivery_date == date(2026, 9, 1)

    completed = core.process_step(
        second.draft,
        IntakeFieldUpdate(
            values={
                "budget_status": "unknown",
                "business_justification": "Поддержание чистоты в офисе",
                "department": "АХО",
                "contact_person": "Александра",
            }
        ),
    )
    assert completed.status == IntakeStatus.READY_FOR_CONFIRMATION
    persistent = PersistentIntakeStepResult(
        request_id=REQUEST_ID,
        user_id=USER_ID,
        request_version=1,
        intake_result=completed,
        dialog_state=PersistentDialogState(
            user_id=USER_ID,
            request_id=REQUEST_ID,
            intake_status=IntakeStatus.READY_FOR_CONFIRMATION,
            state_version=1,
        ),
        persistence_status="saved",
    )
    card = format_request_card(persistent)
    assert "Какая услуга требуется: Еженедельная уборка офиса" in card
    assert "Площадь 500 м²" in card
    assert "мылись полы" in card
    assert "не более 80 000 ₽ в месяц" in card
    assert "Срок оказания услуги: 1 сентября 2026" in card
    assert source.casefold() not in card.casefold()
    for debris in ("лей", "руб", "бюджет до", "per_month", "billing_period="):
        assert debris not in card.casefold()


def test_amount_modifier_is_persistable_field_evidence() -> None:
    update = DeterministicIntakeParser().parse(
        "не более 120 тыс.", _question("amount", "decimal")
    )
    assert update.values == {"amount": Decimal("120000")}
    assert update.evidence_by_field == {"amount": "amount_modifier=maximum"}
    result = RequestIntakeService().process_step(RequestDraftData(), update)
    assert result.draft.field_states["amount"].evidence == (
        "amount_modifier=maximum"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("да", "budgeted"),
        ("предусмотрена", "budgeted"),
        ("забюджетировано", "budgeted"),
        ("закупка забюджетирована", "budgeted"),
        ("расходы забюджетированы", "budgeted"),
        ("заложено в бюджете", "budgeted"),
        ("сумма заложена в бюджете", "budgeted"),
        ("предусмотрено бюджетом", "budgeted"),
        ("предусмотрено в бюджете", "budgeted"),
        ("учтено в бюджете", "budgeted"),
        ("бюджет есть", "budgeted"),
        ("в бюджете есть", "budgeted"),
        ("из утверждённого бюджета", "budgeted"),
        ("бюджетная закупка", "budgeted"),
        ("budgeted", "budgeted"),
        ("нет", "unbudgeted"),
        ("не предусмотрена", "unbudgeted"),
        ("не забюджетировано", "unbudgeted"),
        ("закупка не забюджетирована", "unbudgeted"),
        ("не заложено в бюджете", "unbudgeted"),
        ("не предусмотрено бюджетом", "unbudgeted"),
        ("не предусмотрено в бюджете", "unbudgeted"),
        ("unbudgeted", "unbudgeted"),
        ("не знаю", "unknown"),
        ("неизвестно", "unknown"),
        ("надо уточнить", "unknown"),
        ("не могу сказать", "unknown"),
        ("не знаю, предусмотрена ли", "unknown"),
        ("не знаю, забюджетировано ли", "unknown"),
        ("не уверен, что заложено в бюджете", "unknown"),
        ("не уверен, что это предусмотрено бюджетом", "unknown"),
        ("бюджет пока не знаю", "unknown"),
    ],
)
def test_budget_answers_are_mapped_to_backend_values(raw: str, expected: str) -> None:
    update = DeterministicIntakeParser().parse(
        raw, _question("budget_status", "choice")
    )
    assert update.values == {"budget_status": expected}


def test_unknown_amount_is_not_written_as_false() -> None:
    with pytest.raises(TelegramParseError):
        DeterministicIntakeParser().parse(
            "Не знаю", _question("amount", "decimal")
        )


def test_profile_fields_are_system_updates_and_explicit_values_win() -> None:
    profile = ResolvedTelegramUser(
        user_id=USER_ID,
        full_name="Иван Петров",
        department="Финансы",
    )
    update = TelegramIntakeAdapter._profile_update(profile, None, None)
    assert update.source == UpdateSource.SYSTEM
    assert update.values == {
        "contact_person": "Иван Петров",
        "department": "Финансы",
    }

    active = _persistent(
        RequestDraftData(contact_person="Мария", department="Закупки")
    )
    preserved = TelegramIntakeAdapter._profile_update(profile, active, None)
    assert preserved.values == {}


def test_profile_autofill_is_persisted_and_not_asked_again() -> None:
    orchestrator = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository()
    )
    adapter = TelegramIntakeAdapter(orchestrator)
    profile = ResolvedTelegramUser(
        user_id=USER_ID,
        full_name="Иван Петров",
        department="Финансы",
    )

    outcome = adapter.handle_text(
        profile,
        chat_id=1001,
        message_id=70,
        text="Нужно купить 10 офисных кресел",
    )

    assert outcome.result is not None
    draft = outcome.result.intake_result.draft
    assert draft.contact_person == "Иван Петров"
    assert draft.department == "Финансы"
    assert draft.field_states["contact_person"].source == UpdateSource.SYSTEM
    assert draft.field_states["department"].source == UpdateSource.SYSTEM
    assert outcome.result.intake_result.next_question is not None
    assert outcome.result.intake_result.next_question.field_code not in {
        "contact_person",
        "department",
        "procurement_type",
        "quantity",
        "unit",
    }


def test_profile_does_not_replace_the_field_currently_answered_by_user() -> None:
    profile = ResolvedTelegramUser(
        user_id=USER_ID,
        full_name="Иван Петров",
        department="Финансы",
    )
    active = _persistent(RequestDraftData(), awaiting="department")
    update = TelegramIntakeAdapter._profile_update(profile, active, "department")
    assert update.values == {"contact_person": "Иван Петров"}
