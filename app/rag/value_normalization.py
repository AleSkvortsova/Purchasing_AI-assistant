import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

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
    "тридцать": 30,
}
_TENS = {"двадцать": 20, "тридцать": 30}
_ONES = {key: value for key, value in _NUMBER_WORDS.items() if value < 10}
_WORD_NUMBER_PATTERN = "|".join(
    sorted(_NUMBER_WORDS, key=len, reverse=True)
)


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
    normalized = _normalize_scaled_amounts(normalized)
    previous = None
    while previous != normalized:
        previous = normalized
        normalized = re.sub(r"(?<=\d)[ .](?=\d{3}(?:\D|$))", "", normalized)
    normalized = _normalize_duration_phrases(normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_money_amount(value: str) -> Decimal | None:
    normalized = normalize_regulation_text(value)
    match = re.search(
        rf"(?<![\w])(-?\d+(?:[.,]\d+)?)\s*{_CURRENCY}"
        r"(?=\s|$|[.,;:])|"
        r"^\s*(-?\d+(?:[.,]\d+)?)\s*$",
        normalized,
    )
    if match is None:
        return None
    raw = next(item for item in match.groups() if item is not None)
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def parse_duration_days(value: str) -> int | None:
    normalized = _basic_normalize(value)
    normalized = _replace_word_durations(normalized)
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
    normalized = _replace_word_durations(value)

    def replace_weeks(match: re.Match[str]) -> str:
        return f"{int(match.group(1)) * 7} дней"

    return re.sub(
        r"(?<!\w)(\d+)\s+недел(?:я|и|ю|ь)\b",
        replace_weeks,
        normalized,
    )


def _replace_word_durations(value: str) -> str:
    pattern = re.compile(
        rf"\b({_WORD_NUMBER_PATTERN})(?:\s+({_WORD_NUMBER_PATTERN}))?\s+"
        r"((?:(?:календарн|рабоч)\w*\s+)?"
        r"(?:д(?:ень|ня|ней)|недел(?:я|и|ю|ь)))\b"
    )

    def replace(match: re.Match[str]) -> str:
        number = _word_number(match.group(1), match.group(2))
        if number is None:
            return match.group(0)
        unit = match.group(3)
        if unit.startswith("недел"):
            return f"{number * 7} дней"
        return f"{number} {unit}"

    return pattern.sub(replace, value)


def _word_number(first: str, second: str | None) -> int | None:
    if second is None:
        return _NUMBER_WORDS.get(first)
    if first in _TENS and second in _ONES:
        return _TENS[first] + _ONES[second]
    return None
