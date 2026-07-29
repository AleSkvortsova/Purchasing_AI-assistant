import re
from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_TIMEZONE = "Europe/Moscow"
_SPACES = re.compile(r"[\s\u00a0]+")
_UNKNOWN = {"не знаю", "нужна оценка", "сумма неизвестна", "неизвестно"}
AmountModifier = Literal["exact", "maximum", "approximate"]
BillingPeriod = Literal["per_month", "per_quarter", "per_year"]
_AMOUNT_MODIFIERS: tuple[tuple[AmountModifier, tuple[str, ...]], ...] = (
    ("maximum", ("не более", "максимум", "до")),
    ("approximate", ("ориентировочно", "примерно", "порядка", "около")),
)
_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_UNIT_ALIASES = {
    "шт": "шт.",
    "шт.": "шт.",
    "штука": "шт.",
    "штуки": "шт.",
    "штук": "шт.",
    "единица": "шт.",
    "единицы": "шт.",
    "единиц": "шт.",
    "кг": "кг",
    "килограмм": "кг",
    "килограмма": "кг",
    "килограммов": "кг",
    "л": "л",
    "л.": "л",
    "литр": "л",
    "литра": "л",
    "литров": "л",
    "м": "м",
    "метр": "м",
    "метра": "м",
    "метров": "м",
    "упак": "упак.",
    "упак.": "упак.",
    "упаковка": "упак.",
    "упаковки": "упак.",
    "упаковок": "упак.",
    "коробка": "коробка",
    "коробки": "коробка",
    "коробок": "коробка",
    "комплект": "комплект",
    "комплекта": "комплект",
    "комплектов": "комплект",
    "м2": "м²",
    "м²": "м²",
    "кв. м": "м²",
    "час": "час",
    "часа": "час",
    "часов": "час",
    "день": "день",
    "дня": "день",
    "дней": "день",
    "услуга": "услуга",
    "услуги": "услуга",
    "услуг": "услуга",
}

_CARDINAL_ONES = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одни": 1,
    "одного": 1,
    "одной": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "три": 3,
    "трех": 3,
    "четыре": 4,
    "четырех": 4,
    "пять": 5,
    "пяти": 5,
    "шесть": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
}
_CARDINAL_TEENS = {
    "десять": 10,
    "десяти": 10,
    "одиннадцать": 11,
    "одиннадцати": 11,
    "двенадцать": 12,
    "двенадцати": 12,
    "тринадцать": 13,
    "тринадцати": 13,
    "четырнадцать": 14,
    "четырнадцати": 14,
    "пятнадцать": 15,
    "пятнадцати": 15,
    "шестнадцать": 16,
    "шестнадцати": 16,
    "семнадцать": 17,
    "семнадцати": 17,
    "восемнадцать": 18,
    "восемнадцати": 18,
    "девятнадцать": 19,
    "девятнадцати": 19,
}
_CARDINAL_TENS = {
    "двадцать": 20,
    "двадцати": 20,
    "тридцать": 30,
    "тридцати": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "пятидесяти": 50,
    "шестьдесят": 60,
    "шестидесяти": 60,
    "семьдесят": 70,
    "семидесяти": 70,
    "восемьдесят": 80,
    "восьмидесяти": 80,
    "девяносто": 90,
}
_CARDINAL_HUNDRED = {"сто": 100, "ста": 100}
_CARDINAL_WORDS = {
    *_CARDINAL_ONES,
    *_CARDINAL_TEENS,
    *_CARDINAL_TENS,
    *_CARDINAL_HUNDRED,
}
CARDINAL_WORD_PATTERN = "(?:" + "|".join(
    sorted(_CARDINAL_WORDS, key=len, reverse=True)
) + ")"
_RELATIVE_DATE = re.compile(
    rf"\bчерез\s+"
    rf"(?:(?P<count>\d+|{CARDINAL_WORD_PATTERN}(?:\s+{CARDINAL_WORD_PATTERN})?)\s+)?"
    r"(?P<unit>день|дня|дней|неделю|недели|недель|"
    r"месяц|месяца|месяцев|квартал)\b",
    re.IGNORECASE,
)


class UnknownIntakeValueError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedAmount:
    amount: Decimal
    modifier: AmountModifier
    billing_period: BillingPeriod | None = None


@dataclass(frozen=True)
class ParsedAmountSpan:
    parsed: ParsedAmount
    span: tuple[int, int]


_AMOUNT_SEARCH = re.compile(
    r"(?:(?P<label>бюджет|ориентировочная\s+сумма|сумма)\s*(?:[:—–-]\s*)?)?"
    r"(?:(?P<modifier>не\s+более|максимум|до|ориентировочно|примерно|порядка|около)\s+)?"
    r"(?P<number>\d[\d\s\u00a0]*(?:[.,]\d+)?)\s*"
    r"(?P<scale>миллион(?:а|ов)?|млн\.?|тысяч(?:а|и)?|тыс\.?)?\s*"
    r"(?:(?P<currency>рубл(?:ь|я|ей)|руб\.?|р\.?|₽)(?![а-яёa-z]))?"
    r"(?:\s+(?P<period>в\s+месяц|за\s+месяц|ежемесячно|"
    r"в\s+квартал|в\s+год|за\s+год|ежегодно))?",
    re.IGNORECASE,
)


class NaturalDateParser:
    def __init__(
        self,
        timezone_name: str = PROJECT_TIMEZONE,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        try:
            project_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            if timezone_name != PROJECT_TIMEZONE:
                raise
            # Moscow has used UTC+3 year-round since October 2014. This keeps
            # the default usable on Windows installations without tzdata.
            project_timezone = timezone(timedelta(hours=3), name=PROJECT_TIMEZONE)
        self._today = today_provider or (
            lambda: datetime.now(project_timezone).date()
        )

    def parse(self, value: str) -> date:
        normalized = " ".join(value.casefold().replace("ё", "е").split())
        today = self._today()
        if normalized == "завтра":
            return today + timedelta(days=1)
        if normalized == "послезавтра":
            return today + timedelta(days=2)
        relative = _RELATIVE_DATE.fullmatch(normalized)
        if relative is not None:
            count = parse_cardinal(relative.group("count") or "один")
            if count <= 0:
                raise ValueError("Relative date must be in the future")
            unit = relative.group("unit")
            if unit.startswith("недел"):
                return today + timedelta(days=count * 7)
            if unit.startswith("месяц"):
                return _add_calendar_months(today, count)
            if unit == "квартал":
                return _add_calendar_months(today, count * 3)
            return today + timedelta(days=count)
        normalized = re.sub(
            r"^(?:(?:завершить|выполнить)\s+)?(?:"
            r"не\s+позднее(?:\s+чем)?|не\s+позже|"
            r"крайний\s+срок\s*[—–:-]?|максимум\s+до)\s+",
            "",
            normalized,
        )
        normalized = re.sub(
            r"^(?:(?:как\s+я\s+уже\s+написал[аи]?|я\s+писал[аи]?,?\s+что\s+нужно|"
            r"нужно|давайте|дата\s*[—–:-]?|срок\s*[—–:-]?)\s*,?\s*)?"
            r"(?:начиная\s+с|начать\s+с|с|на|к|до)?\s*",
            "",
            normalized,
        )
        for pattern, parser in (
            (r"\d{4}-\d{2}-\d{2}", date.fromisoformat),
            (r"\d{2}\.\d{2}\.\d{4}", self._parse_dotted),
        ):
            if re.fullmatch(pattern, normalized):
                return parser(normalized)
        match = re.fullmatch(
            r"(?P<day>\d{1,2})\s+(?P<month>[а-я]+)(?:\s+(?P<year>\d{4}))?",
            normalized,
        )
        if match is None or match.group("month") not in _MONTHS:
            raise ValueError("Unsupported date format")
        year = int(match.group("year")) if match.group("year") else today.year
        result = date(year, _MONTHS[match.group("month")], int(match.group("day")))
        if match.group("year") is None and result < today:
            result = result.replace(year=year + 1)
        return result

    def search(self, text: str) -> tuple[date, tuple[int, int]] | None:
        month_date = (
            r"\d{1,2}\s+(?:" + "|".join(_MONTHS) + r")(?:\s+\d{4})?"
        )
        prefix = (
            r"(?:не\s+позднее(?:\s+чем)?|не\s+позже|"
            r"крайний\s+срок\s*[—–:-]?|максимум\s+до|"
            r"начиная\s+с|начать\s+с|нужн[ао]\s+с|с|на|к|до|"
            r"дата\s*[—–:-]?|срок\s*[—–:-]?)\s*"
        )
        patterns = (
            r"\bпослезавтра\b",
            r"\bзавтра\b",
            r"\b(?:начиная\s+с\s+)?\d{4}-\d{2}-\d{2}\b",
            r"\b\d{2}\.\d{2}\.\d{4}\b",
            r"\b" + prefix + month_date + r"\b",
        )
        relative = _RELATIVE_DATE.search(text.casefold().replace("ё", "е"))
        if relative is not None:
            return self.parse(relative.group()), relative.span()
        for pattern in patterns:
            match = re.search(pattern, text.casefold())
            if match is not None:
                return self.parse(match.group()), match.span()
        return None

    @staticmethod
    def _parse_dotted(value: str) -> date:
        day, month, year = (int(part) for part in value.split("."))
        return date(year, month, day)


def parse_cardinal(value: str) -> int:
    normalized = " ".join(value.casefold().replace("ё", "е").split())
    if normalized.isdigit():
        return int(normalized)
    tokens = normalized.split()
    if len(tokens) == 1:
        token = tokens[0]
        for numbers in (
            _CARDINAL_ONES,
            _CARDINAL_TEENS,
            _CARDINAL_TENS,
            _CARDINAL_HUNDRED,
        ):
            if token in numbers:
                return numbers[token]
    if (
        len(tokens) == 2
        and tokens[0] in _CARDINAL_TENS
        and tokens[1] in _CARDINAL_ONES
    ):
        return _CARDINAL_TENS[tokens[0]] + _CARDINAL_ONES[tokens[1]]
    raise ValueError("Unsupported cardinal number")


def _add_calendar_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def parse_amount(value: str) -> Decimal:
    return parse_amount_expression(value).amount


def parse_amount_expression(value: str) -> ParsedAmount:
    normalized = " ".join(value.casefold().replace("ё", "е").split())
    if normalized in _UNKNOWN:
        raise UnknownIntakeValueError(
            "Для завершения заявки нужна хотя бы ориентировочная сумма."
        )
    period: BillingPeriod | None = None
    period_match = re.search(
        r"\s+(в\s+месяц|за\s+месяц|ежемесячно|в\s+квартал|"
        r"в\s+год|за\s+год|ежегодно)$",
        normalized,
    )
    if period_match is not None:
        period = _billing_period(period_match.group(1))
        normalized = normalized[: period_match.start()].strip()
    normalized = re.sub(
        r"^(?:бюджет|ориентировочная\s+сумма|сумма)\s*(?:[:—–-]\s*)?",
        "",
        normalized,
    )
    modifier: AmountModifier = "exact"
    for candidate, prefixes in _AMOUNT_MODIFIERS:
        prefix = next(
            (item for item in prefixes if normalized.startswith(f"{item} ")),
            None,
        )
        if prefix is not None:
            modifier = candidate
            normalized = normalized[len(prefix) :].strip()
            break
    match = re.fullmatch(
        r"(?P<number>\d[\d\s\u00a0]*(?:[.,]\d+)?)\s*"
        r"(?P<scale>миллион(?:а|ов)?|млн\.?|тысяч(?:а|и)?|тыс\.?)?\s*"
        r"(?:рубл(?:ь|я|ей)|руб\.?|р\.?|₽)?",
        normalized,
    )
    if match is None:
        raise ValueError("Unsupported amount format")
    compact = _SPACES.sub("", match.group("number")).replace(",", ".")
    try:
        result = Decimal(compact)
    except InvalidOperation as exc:
        raise ValueError("Unsupported amount format") from exc
    scale = match.group("scale")
    if scale and scale.startswith(("млн", "миллион")):
        result *= 1_000_000
    elif scale:
        result *= 1000
    return ParsedAmount(
        amount=result,
        modifier=modifier,
        billing_period=period,
    )


def find_amount_expression(value: str) -> ParsedAmountSpan | None:
    """Find one conservative amount phrase and its complete source span."""
    for match in _AMOUNT_SEARCH.finditer(value):
        has_amount_marker = (
            match.group("label") or match.group("scale") or match.group("currency")
        )
        if not has_amount_marker:
            continue
        raw = match.group().strip()
        if not raw:
            continue
        leading = len(match.group()) - len(match.group().lstrip())
        trailing = len(match.group()) - len(match.group().rstrip())
        end = match.end() - trailing if trailing else match.end()
        return ParsedAmountSpan(
            parsed=parse_amount_expression(raw),
            span=(match.start() + leading, end),
        )
    return None


def amount_evidence(parsed: ParsedAmount) -> str:
    parts = [f"amount_modifier={parsed.modifier}"]
    if parsed.billing_period is not None:
        parts.append(f"billing_period={parsed.billing_period}")
    return "; ".join(parts)


def parse_amount_evidence(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    result: dict[str, str] = {}
    for part in value.split(";"):
        key, separator, raw_value = part.strip().partition("=")
        if separator and key in {"amount_modifier", "billing_period"}:
            result[key] = raw_value.strip()
    return result


def _billing_period(value: str) -> BillingPeriod:
    normalized = " ".join(value.casefold().split())
    if normalized in {"в месяц", "за месяц", "ежемесячно"}:
        return "per_month"
    if normalized == "в квартал":
        return "per_quarter"
    return "per_year"


def normalize_unit(value: str) -> str:
    normalized = " ".join(value.casefold().replace("ё", "е").split())
    if normalized not in _UNIT_ALIASES:
        raise ValueError("Unsupported unit")
    return _UNIT_ALIASES[normalized]


def normalize_procurement_type(value: str) -> str:
    normalized = value.casefold().strip()
    if normalized in {"товар", "товары", "goods"}:
        return "goods"
    if normalized in {"услуга", "услуги", "service"}:
        return "service"
    raise ValueError("Unsupported procurement type")


def normalize_budget_status(value: str) -> str:
    normalized = " ".join(value.casefold().replace("ё", "е").split())
    if re.search(
        r"\b(?:не\s+знаю|не\s+уверен\w*|неизвест\w*|"
        r"(?:надо|нужно|требуется)\s+уточнить)\b",
        normalized,
    ):
        return "unknown"
    if normalized in {
        "не знаю",
        "неизвестно",
        "не уверен",
        "не уверена",
        "надо уточнить",
        "нужно уточнить",
        "не могу сказать",
    } or normalized.startswith("не знаю,"):
        return "unknown"
    if normalized in {
        "нет",
        "нет, не предусмотрена",
        "не предусмотрена",
        "не предусмотрена бюджетом",
        "вне бюджета",
        "бюджета нет",
        "unbudgeted",
    } or re.search(
        r"\b(?:не\s+забюджетирован\w*|"
        r"не\s+заложен\w*\s+в\s+бюджете|"
        r"не\s+предусмотрен\w*\s+(?:бюджетом|в\s+бюджете)|"
        r"вне\s+бюджета|бюджета\s+нет)\b",
        normalized,
    ):
        return "unbudgeted"
    if normalized in {
        "да",
        "да, предусмотрена",
        "предусмотрена",
        "предусмотрена бюджетом",
        "в бюджете",
        "бюджет есть",
        "budgeted",
    } or re.search(
        r"\b(?:забюджетирован\w*|"
        r"заложен\w*\s+в\s+бюджете|"
        r"предусмотрен\w*\s+(?:бюджетом|в\s+бюджете)|"
        r"учтен\w*\s+в\s+бюджете|"
        r"бюджет\s+есть|в\s+бюджете\s+есть|"
        r"из\s+утвержденного\s+бюджета|"
        r"бюджетн\w*\s+закупк\w*)\b",
        normalized,
    ):
        return "budgeted"
    raise ValueError("Unsupported budget status")
