from decimal import Decimal

import pytest

from app.rag.value_normalization import (
    detect_budget_status,
    duration_below_threshold,
    normalize_money_amount,
    normalize_regulation_text,
    parse_duration_days,
    parse_money_ranges,
    value_in_range,
)


@pytest.mark.parametrize(
    ("word", "number"),
    [
        ("один", 1),
        ("два", 2),
        ("три", 3),
        ("четыре", 4),
        ("пять", 5),
        ("шесть", 6),
        ("семь", 7),
        ("восемь", 8),
        ("девять", 9),
        ("десять", 10),
        ("одиннадцать", 11),
        ("двенадцать", 12),
        ("тринадцать", 13),
        ("четырнадцать", 14),
        ("пятнадцать", 15),
        ("шестнадцать", 16),
        ("семнадцать", 17),
        ("восемнадцать", 18),
        ("девятнадцать", 19),
        ("двадцать", 20),
    ],
)
def test_word_numbers_one_to_twenty_are_normalized(word: str, number: int) -> None:
    assert normalize_regulation_text(f"{word} мониторов") == f"{number} мониторов"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("двадцать один монитор", "21 монитор"),
        ("сорок пять паллет", "45 паллет"),
        ("девяносто коробок", "90 коробок"),
        ("девяносто пять тысяч рублей", "95000 рублей"),
    ],
)
def test_tens_and_word_scaled_amounts_are_normalized(
    value: str,
    expected: str,
) -> None:
    assert normalize_regulation_text(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "530000 руб",
        "530 000 рублей",
        "530\u00a0000 ₽",
        "530.000 руб.",
    ],
)
def test_money_formats_have_one_decimal_value(value: str) -> None:
    assert normalize_money_amount(value) == Decimal("530000")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("через 10 дней", 10),
        ("через десять дней", 10),
        ("через две недели", 14),
        ("через 14 дней", 14),
        ("менее чем через 30 дней", 30),
    ],
)
def test_duration_formats_normalize_to_days(value: str, expected: int) -> None:
    assert parse_duration_days(value) == expected


def test_query_text_uses_same_money_and_duration_normalization() -> None:
    assert normalize_regulation_text(
        "530.000 ₽ через две недели"
    ) == "530000 ₽ через 14 дней"


def test_range_and_duration_comparisons_are_deterministic() -> None:
    assert value_in_range(
        Decimal("120000"), Decimal("100001"), Decimal("500000")
    )
    assert value_in_range(
        Decimal("530000"),
        Decimal("500000"),
        None,
        minimum_inclusive=False,
    )
    assert not value_in_range(
        Decimal("500000"),
        Decimal("500000"),
        None,
        minimum_inclusive=False,
    )
    assert duration_below_threshold(10, 30)
    assert duration_below_threshold(14, 30)


def test_money_ranges_are_parsed_from_full_approval_matrix() -> None:
    ranges = parse_money_ranges(
        "До 100 000 руб.; 100 001–500 000 руб.; свыше 500 000 руб."
    )
    assert any(
        value_in_range(
            Decimal("530000"),
            item.minimum,
            item.maximum,
            minimum_inclusive=item.minimum_inclusive,
            maximum_inclusive=item.maximum_inclusive,
        )
        for item in ranges
    )


@pytest.mark.parametrize(
    "value",
    [
        "предусмотрена бюджетом",
        "забюджетировано",
        "деньги в бюджете есть",
        "расходы заложены в бюджет",
        "бюджет подтверждён",
    ],
)
def test_positive_budget_phrases_are_equivalent(value: str) -> None:
    assert detect_budget_status(value) == "budgeted"
    assert "предусмотрена бюджетом" in normalize_regulation_text(value)


def test_negative_and_unknown_budget_phrases_are_not_made_positive() -> None:
    assert detect_budget_status("не забюджетировано") == "unbudgeted"
    assert detect_budget_status("денег в бюджете нет") == "unbudgeted"
    assert detect_budget_status("бюджетный статус неизвестен") == "unknown"


@pytest.mark.parametrize(
    "value",
    [
        "На закупку заложено 320 тысяч рублей",
        "Предусмотрено 200 тысяч на покупку",
        "Подтверждено 150 тысяч для закупки",
        "На это заложили 400 тысяч рублей",
        "Сумма уже в плане",
        "Расходы учтены",
    ],
)
def test_elliptical_positive_budget_phrases(value: str) -> None:
    assert detect_budget_status(value) == "budgeted"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("На закупку не заложено 320 тысяч", "unbudgeted"),
        ("Пока не подтверждено 150 тысяч", "unknown"),
        ("Не знаю, предусмотрено ли 200 тысяч в плане", "unknown"),
        ("Возможно, сумма есть в плане", "unknown"),
    ],
)
def test_elliptical_budget_negation_and_uncertainty(
    value: str,
    expected: str,
) -> None:
    assert detect_budget_status(value) == expected


@pytest.mark.parametrize(
    "prefix",
    ["Заявка: ", "По этой покупке ", "Для согласования — "],
)
def test_budget_status_is_metamorphic_under_neutral_context(prefix: str) -> None:
    assert detect_budget_status(f"{prefix}расходы учтены") == "budgeted"
    assert detect_budget_status(f"{prefix}расходы не учтены") == "unbudgeted"
    assert detect_budget_status(f"{prefix}возможно, расходы учтены") == "unknown"
