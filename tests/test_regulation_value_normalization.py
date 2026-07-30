from decimal import Decimal

import pytest

from app.rag.value_normalization import (
    duration_below_threshold,
    normalize_money_amount,
    normalize_regulation_text,
    parse_duration_days,
    parse_money_ranges,
    value_in_range,
)


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
