import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

_SPACE_TRANSLATION = str.maketrans({"\u00a0": " ", "\u202f": " "})
_DASHES = re.compile(r"[‐‑‒–—−]")
_CURRENCY = r"(?:₽|р\.?|руб\.?|рубл(?:ь|я|ей|и))"
_SCALE = r"(?:тыс(?:\.|яча|ячи|яч)?|млн\.?)"
_NUMBER_WORDS = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "одного": 1,
    "одной": 1,
    "двух": 2,
    "трех": 3,
    "четырех": 4,
    "пяти": 5,
    "шести": 6,
    "семи": 7,
    "восьми": 8,
    "девяти": 9,
    "десяти": 10,
    "одиннадцати": 11,
    "двенадцати": 12,
    "тринадцати": 13,
    "четырнадцати": 14,
    "пятнадцати": 15,
    "шестнадцати": 16,
    "семнадцати": 17,
    "восемнадцати": 18,
    "девятнадцати": 19,
    "двадцати": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
}
_TENS = {
    key: value for key, value in _NUMBER_WORDS.items() if value >= 20
}
_ONES = {key: value for key, value in _NUMBER_WORDS.items() if value < 10}
_WORD_NUMBER_PATTERN = "|".join(
    sorted(_NUMBER_WORDS, key=len, reverse=True)
)
_ONES_PATTERN = "|".join(sorted(_ONES, key=len, reverse=True))
_NEGATIVE_BUDGET = re.compile(
    r"\b(?:внебюджетн\w*|не\s+(?:забюджетирован\w*|заложен\w*|"
    r"предусмотрен\w*(?:\s+(?:в\s+)?бюджет\w*)?)|"
    r"(?:деньги|расход\w*)\s+не\s+(?:заложен\w*|учтен\w*)|"
    r"денег\s+в\s+бюджет\w*\s+нет)\b"
)
_UNKNOWN_BUDGET = re.compile(
    r"\b(?:бюджетн\w*\s+статус\s+неизвест\w*|"
    r"(?:пока\s+)?не\s+подтвержден\w*|"
    r"(?:не\s+уверен\w*|сомнева\w*),?.*(?:бюджет|план|финанс)\w*|"
    r"возможн\w*,?.*(?:бюджет|план|финанс|залож|предусмотр|учтен)\w*|"
    r"(?:не\s+зна\w*|непонятн\w*),?.*предусмотрен\w*\s+ли|"
    r"(?:не\s+зна\w*|непонятн\w*),?\s+(?:есть|предусмотрен\w*)\s+ли\s+"
    r"(?:она\s+)?(?:в\s+)?бюджет\w*|"
    r"(?:не\s+зна\w*|непонятн\w*),?\s+предусмотрен\w*\s+ли\s+"
    r"(?:она\s+)?бюджет\w*|"
    r"неизвест\w*,?\s+(?:есть|предусмотрен\w*)\s+ли\s+"
    r"(?:деньги\s+в\s+)?бюджет\w*)\b"
)
_POSITIVE_BUDGET = re.compile(
    r"\b(?:забюджетирован\w*|предусмотрен\w*\s+(?:в\s+)?бюджет\w*|"
    r"деньги\s+в\s+бюджет\w*\s+есть|"
    r"расход\w*\s+(?:предусмотрен\w*\s+план\w*|заложен\w*(?:\s+в\s+бюджет\w*)?)|"
    r"расход\w*\s+учтен\w*|сумм\w*\s+уже\s+в\s+(?:план\w*|бюджет\w*)|"
    r"(?:есть|имеется)(?=\s+\d[\d\s.,]*(?:тыс\w*|млн\w*|руб\w*)\s+"
    r"в\s+(?:план\w*|бюджет\w*))|"
    r"деньги\s+заложен\w*|"
    r"(?:заложен\w*|заложил\w*|предусмотрен\w*|подтвержден\w*)"
    r"(?=\s+\d[\d .]*(?:тыс|млн|руб|₽))|"
    r"бюджет\w*\s+подтвержден\w*|бюджетн\w*\s+закуп\w*)\b"
)

BudgetStatus = Literal["budgeted", "unbudgeted", "unknown"]


@dataclass(frozen=True)
class NumericRange:
    minimum: Decimal | None
    maximum: Decimal | None
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True


def normalize_regulation_text(value: str) -> str:
    normalized = (
        value.casefold()
        .replace("ё", "е")
        .translate(_SPACE_TRANSLATION)
    )
    normalized = _DASHES.sub("-", normalized)
    normalized = _normalize_budget_phrases(normalized)
    normalized = _replace_word_numbers(normalized)
    normalized = _normalize_scaled_amounts(normalized)
    previous = None
    while previous != normalized:
        previous = normalized
        normalized = re.sub(r"(?<=\d)[ .](?=\d{3}(?:\D|$))", "", normalized)
    normalized = _normalize_duration_phrases(normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return re.sub(r"\s+([.,!?;:])", r"\1", normalized)


def detect_budget_status(value: str) -> BudgetStatus | None:
    normalized = _basic_normalize(value)
    if _UNKNOWN_BUDGET.search(normalized):
        return "unknown"
    if _NEGATIVE_BUDGET.search(normalized):
        return "unbudgeted"
    if _POSITIVE_BUDGET.search(normalized):
        return "budgeted"
    return None


def normalize_money_amount(value: str) -> Decimal | None:
    normalized = normalize_regulation_text(value)
    match = re.search(
        rf"(?<![\w])(-?\d+(?:[.,]\d+)?)\s*{_CURRENCY}"
        r"(?=\s|$|[.,;:!?»)])|"
        r"^\s*(-?\d+(?:[.,]\d+)?)\s*$",
        normalized,
    )
    if match is None:
        if not re.search(r"(?:тыс|млн|стоим|сумм|закуп|покуп|заяв)", value.casefold()):
            return None
        scaled = re.search(r"(?<!\d)(\d{4,})(?!\d)", normalized)
        if scaled is None:
            return None
        match_value = scaled.group(1)
        try:
            return Decimal(match_value)
        except InvalidOperation:
            return None
    raw = next(item for item in match.groups() if item is not None)
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def parse_duration_days(value: str) -> int | None:
    normalized = normalize_regulation_text(value)
    match = re.search(
        r"(?<!\w)(\d+)\s+(?:(?:календарн|рабоч)\w*\s+)?"
        r"(д(?:ень|ня|ней)|недел(?:я|и|ю|ь))\b",
        normalized,
    )
    if match is None:
        return None
    amount = int(match.group(1))
    return amount * 7 if match.group(2).startswith("недел") else amount


def value_in_range(
    value: Decimal,
    minimum: Decimal | None,
    maximum: Decimal | None,
    *,
    minimum_inclusive: bool = True,
    maximum_inclusive: bool = True,
) -> bool:
    if minimum is not None:
        if value < minimum or (value == minimum and not minimum_inclusive):
            return False
    if maximum is not None:
        if value > maximum or (value == maximum and not maximum_inclusive):
            return False
    return True


def duration_below_threshold(duration_days: int, threshold_days: int) -> bool:
    return duration_days < threshold_days


def parse_money_ranges(value: str) -> tuple[NumericRange, ...]:
    normalized = normalize_regulation_text(value)
    ranges: list[NumericRange] = []
    for match in re.finditer(r"(\d+)\s*-\s*(\d+)", normalized):
        ranges.append(NumericRange(Decimal(match.group(1)), Decimal(match.group(2))))
    for match in re.finditer(r"(?:свыше|более)\s+(\d+)", normalized):
        ranges.append(
            NumericRange(
                Decimal(match.group(1)),
                None,
                minimum_inclusive=False,
            )
        )
    for match in re.finditer(r"(?:до|не более)\s+(\d+)", normalized):
        ranges.append(NumericRange(None, Decimal(match.group(1))))
    return tuple(ranges)


def _basic_normalize(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        _DASHES.sub(
            "-",
            value.casefold().replace("ё", "е").translate(_SPACE_TRANSLATION),
        ),
    ).strip()


def _normalize_scaled_amounts(value: str) -> str:
    pattern = re.compile(
        rf"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*({_SCALE})\b",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        try:
            number = Decimal(match.group(1).replace(",", "."))
        except InvalidOperation:
            return match.group(0)
        multiplier = (
            Decimal("1000000")
            if match.group(2).startswith("млн")
            else Decimal("1000")
        )
        return format((number * multiplier).quantize(Decimal("1")), "f")

    return pattern.sub(replace, value)


def _normalize_duration_phrases(value: str) -> str:
    def replace_weeks(match: re.Match[str]) -> str:
        return f"{int(match.group(1)) * 7} дней"

    normalized = re.sub(
        r"(?<!\w)(\d+)\s+недел(?:я|и|ю|ь)\b",
        replace_weeks,
        value,
    )
    normalized = re.sub(r"\bпослезавтра\b", "через 2 дня", normalized)
    normalized = re.sub(r"\bзавтра\b", "через 1 день", normalized)
    normalized = re.sub(r"\bсегодня\b", "через 0 дней", normalized)
    return re.sub(r"\bчерез\s+месяц\b", "через 30 дней", normalized)


def _replace_word_numbers(value: str) -> str:
    pattern = re.compile(
        rf"\b({_WORD_NUMBER_PATTERN})(?:\s+({_ONES_PATTERN}))?\b"
    )

    def replace(match: re.Match[str]) -> str:
        number = _word_number(match.group(1), match.group(2))
        return str(number) if number is not None else match.group(0)

    return pattern.sub(replace, value)


def _normalize_budget_phrases(value: str) -> str:
    normalized = _UNKNOWN_BUDGET.sub(" __budget_unknown__ ", value)
    normalized = _NEGATIVE_BUDGET.sub(" __budget_unbudgeted__ ", normalized)
    normalized = _POSITIVE_BUDGET.sub(" __budget_budgeted__ ", normalized)
    return (
        normalized.replace("__budget_unbudgeted__", "не предусмотрена бюджетом")
        .replace("__budget_unknown__", "бюджетный статус неизвестен")
        .replace("__budget_budgeted__", "предусмотрена бюджетом")
    )


def _word_number(first: str, second: str | None) -> int | None:
    if second is None:
        return _NUMBER_WORDS.get(first)
    if first in _TENS and second in _ONES:
        return _TENS[first] + _ONES[second]
    return None
