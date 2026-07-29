from decimal import Decimal

import pytest

from app.extraction.normalization import (
    CATEGORY_ALIASES,
    compact_category_reference,
    evidence_supports_field,
    match_category,
    normalize_budget_status,
    normalize_money,
    normalize_urgency,
)
from app.intake.field_registry import CATEGORY_NAMES


def test_compact_category_reference_describes_g09_cleaning_goods() -> None:
    reference = compact_category_reference()

    g09 = next(line for line in reference.splitlines() if "G09" in line)
    assert "моющие" in g09
    assert "хозяйственный инвентарь" in g09


def test_category_codes_and_names_match_compact_knowledge_source() -> None:
    rows = {}
    for line in compact_category_reference().splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows[cells[0]] = cells[1]

    assert rows == CATEGORY_NAMES
    assert set(CATEGORY_ALIASES) == set(CATEGORY_NAMES)


@pytest.mark.parametrize(
    ("source", "evidence"),
    [
        ("Купить 12 паллет для склада", "паллеты для склада"),
        ("Закупить офисную мебель", "офисная мебель"),
        ("Клининг в новом офисе", "новый офис"),
    ],
)
def test_semantic_evidence_supports_safe_russian_word_forms(
    source: str,
    evidence: str,
) -> None:
    assert evidence_supports_field("item_name", source, evidence)


def test_derived_category_requires_a_supported_source_phrase() -> None:
    assert evidence_supports_field(
        "category", "Заказать доставку 12 паллет", "доставка 12 паллет"
    )
    assert not evidence_supports_field(
        "category", "Купить бумагу", "доставка паллет"
    )


def test_direct_numeric_evidence_does_not_use_morphological_matching() -> None:
    assert not evidence_supports_field("unit", "Купить 12 паллет", "шт.")


def test_delivery_location_still_requires_direct_source_span() -> None:
    assert not evidence_supports_field(
        "delivery_location", "Клининг в новом офисе", "новый офис"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("180000", Decimal("180000")),
        ("180 000", Decimal("180000")),
        ("180\u00a0000", Decimal("180000")),
        ("180 тыс.", Decimal("180000")),
        ("180 тысяч", Decimal("180000")),
        ("1,2 млн", Decimal("1200000.0")),
        ("1.2 млн", Decimal("1200000.0")),
        ("600 т.р.", Decimal("600000")),
    ],
)
def test_normalize_exact_money(text: str, expected: Decimal) -> None:
    result = normalize_money(text)

    assert result.amount == expected
    assert result.amount_type == "exact"


def test_normalize_maximum_and_approximate_money() -> None:
    maximum = normalize_money("до 500 тысяч")
    approximate = normalize_money("бюджет около 200 тысяч")

    assert maximum.amount == Decimal("500000")
    assert maximum.amount_type == "maximum"
    assert approximate.amount == Decimal("200000")
    assert approximate.amount_type == "approximate"


@pytest.mark.parametrize(
    ("text", "minimum", "maximum", "evidence"),
    [
        (
            "от 180 до 220 тысяч",
            Decimal("180000"),
            Decimal("220000"),
            "от 180 до 220 тысяч",
        ),
        (
            "180–220 тысяч",
            Decimal("180000"),
            Decimal("220000"),
            "180–220 тысяч",
        ),
        (
            "180 - 220 тыс.",
            Decimal("180000"),
            Decimal("220000"),
            "180 - 220 тыс.",
        ),
        (
            "от 1,2 до 1,5 млн",
            Decimal("1200000"),
            Decimal("1500000"),
            "от 1,2 до 1,5 млн",
        ),
        (
            "диапазон 180000–220000 рублей",
            Decimal("180000"),
            Decimal("220000"),
            "диапазон 180000–220000 рублей",
        ),
    ],
)
def test_range_is_not_silently_reduced_to_one_amount(
    text: str,
    minimum: Decimal,
    maximum: Decimal,
    evidence: str,
) -> None:
    result = normalize_money(text)

    assert result.amount is None
    assert result.min_amount == minimum
    assert result.max_amount == maximum
    assert result.amount_type == "range"
    assert result.currency == "RUB"
    assert evidence in result.evidence


def test_equal_range_boundaries_become_exact() -> None:
    result = normalize_money("от 180 до 180 тысяч")

    assert result.amount == Decimal("180000")
    assert result.min_amount is None
    assert result.max_amount is None
    assert result.amount_type == "exact"


@pytest.mark.parametrize(
    "text",
    [
        "от -180 до 220 тысяч",
        "от 180 до -220 тысяч",
        "-180–220 тысяч",
    ],
)
def test_negative_range_boundaries_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="negative"):
        normalize_money(text)


def test_reversed_range_boundaries_are_rejected() -> None:
    with pytest.raises(ValueError, match="reversed"):
        normalize_money("от 220 до 180 тысяч")


def test_multiple_independent_ranges_are_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="multiple amount ranges",
    ):
        normalize_money(
            "от 100 до 200 тысяч или от 300 до 400 тысяч"
        )


@pytest.mark.parametrize("text", ["-100 рублей", "100 и 200 тысяч"])
def test_ambiguous_or_negative_money_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        normalize_money(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("закупка бюджетная", "budgeted"),
        ("предусмотрено бюджетом", "budgeted"),
        ("вне бюджета", "unbudgeted"),
        ("не предусмотрено бюджетом", "unbudgeted"),
        ("бюджета нет", "unbudgeted"),
        ("бюджет 180 тысяч", None),
    ],
)
def test_budget_status_requires_explicit_semantics(
    text: str,
    expected: str | None,
) -> None:
    status, contradictions = normalize_budget_status(text)

    assert status == expected
    assert contradictions == []


def test_budget_status_contradiction_is_visible() -> None:
    status, contradictions = normalize_budget_status(
        "Закупка бюджетная, но в бюджете не предусмотрена"
    )

    assert status is None
    assert contradictions


def test_urgency_claim_does_not_assign_priority() -> None:
    urgency, claimed, warnings = normalize_urgency("Нужно очень срочно")

    assert urgency is None
    assert claimed is True
    assert warnings


def test_explicit_priority_is_preserved_with_warning() -> None:
    urgency, claimed, warnings = normalize_urgency("Приоритет P3")

    assert urgency == "P3"
    assert claimed is True
    assert warnings


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("юридические услуги", "S11"),
        ("IT-интеграция", "S05"),
        ("перевозка паллет", "S03"),
    ],
)
def test_real_category_codes_are_used(
    text: str,
    expected: str,
) -> None:
    category, warnings = match_category(text)

    assert category == expected
    assert warnings == []
