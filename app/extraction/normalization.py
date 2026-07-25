import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.extraction.models import MoneyExtraction

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPACE_TRANSLATION = str.maketrans({"\u00a0": " ", "\u202f": " "})
DASH_PATTERN = r"[-–—]"
NUMBER_PATTERN = (
    r"(?<![A-Za-zА-Яа-я])"
    r"(?:\d{1,3}(?:\s\d{3})+|\d+(?:[.,]\d+)?)"
    r"(?![A-Za-zА-Яа-я])"
)
UNIT_PATTERN = r"(?:млн\.?|тыс(?:яч)?\.?|т\.?\s*р\.?)"
CURRENCY_PATTERN = r"(?:руб(?:\.|ля|лей|ль)?)"
BOOLEAN_FACT_FIELDS = {
    "single_supplier",
    "has_data_access",
    "work_on_site",
}
MULTIPLIERS = {
    "тыс": Decimal("1000"),
    "тысяч": Decimal("1000"),
    "т.р.": Decimal("1000"),
    "тр": Decimal("1000"),
    "млн": Decimal("1000000"),
}

CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "G01": ("офисные принадлежности", "бумага", "ручки"),
    "G02": ("мебель", "кресла", "столы"),
    "G03": ("it-оборудование", "ноутбук", "монитор"),
    "G04": ("it-периферия", "мыши", "гарнитуры"),
    "G05": ("лицензии", "saas", "программное обеспечение"),
    "G06": ("складское оборудование", "стеллаж", "тележк"),
    "G07": ("погрузчик", "погрузочная техника"),
    "G08": ("спецодежд", "сиз"),
    "G09": ("хозяйственные товары", "инвентарь"),
    "G10": ("упаковочные материалы", "коробк", "плёнк"),
    "G11": ("готовая полиграфия", "готовые буклеты"),
    "G12": ("pos-материал", "баннер", "стенд"),
    "G13": ("сувенир", "мерч"),
    "G14": ("электротехнические материалы", "кабел", "ламп"),
    "G15": ("инженерные запчасти", "фильтр", "детал"),
    "S01": ("ремонт оборудования", "обслуживание оборудования"),
    "S02": ("клининг", "уборк"),
    "S03": ("перевозк", "доставк", "транспортные услуги"),
    "S04": ("складские услуги", "хранение"),
    "S05": ("it-интеграц", "it-разработк", "разработка по"),
    "S06": ("маркетинговые услуги", "исследование рынка"),
    "S07": ("дизайн", "фото", "видео"),
    "S08": ("мероприят", "выставк"),
    "S09": ("обучение", "тренинг"),
    "S10": ("подбор персонала", "рекрутинг"),
    "S11": ("юридическ", "консалтинг", "аудит"),
    "S12": ("переводческ", "перевод текст"),
    "S13": ("печать по макету", "полиграфические услуги"),
    "S14": ("аренда",),
    "S15": ("прочие профессиональные услуги",),
}


class MultipleMoneyRangesError(ValueError):
    """Raised when one text contains multiple independent amount ranges."""


class MultipleIndependentAmountsError(ValueError):
    """Raised when different amounts have no explicit semantic roles."""


def normalize_search_text(value: str) -> str:
    normalized = value.translate(SPACE_TRANSLATION).casefold().replace("ё", "е")
    return " ".join(normalized.split())


def evidence_is_present(source_text: str, evidence: str) -> bool:
    return normalize_search_text(evidence) in normalize_search_text(source_text)


def fact_requires_evidence(field_name: str, value: object) -> bool:
    if field_name in BOOLEAN_FACT_FIELDS:
        return value is True
    return value is not None


def normalize_money(value: str) -> MoneyExtraction:
    text = normalize_search_text(value)
    for minus_match in re.finditer(r"[−-]\s*\d", text):
        prefix = text[: minus_match.start()].rstrip()
        if not prefix or not prefix[-1].isdigit():
            raise ValueError("negative amount is not allowed")

    ranges = _find_money_ranges(text)
    if len(ranges) > 1:
        raise MultipleMoneyRangesError(
            "multiple amount ranges require clarification"
        )
    if ranges:
        range_match = ranges[0]
        left_unit = range_match.group("left_unit")
        right_unit = range_match.group("right_unit")
        minimum = _decimal(range_match.group("left")) * _multiplier(
            left_unit or right_unit
        )
        maximum = _decimal(range_match.group("right")) * _multiplier(
            right_unit or left_unit
        )
        if maximum < minimum:
            raise ValueError("amount range is reversed")
        evidence = range_match.group("evidence").strip()
        if minimum == maximum:
            return MoneyExtraction(
                amount=minimum,
                amount_type="exact",
                currency="RUB",
                evidence=evidence,
            )
        return MoneyExtraction(
            min_amount=minimum,
            max_amount=maximum,
            amount_type="range",
            currency="RUB",
            evidence=evidence,
        )

    matches = list(
        re.finditer(
            rf"(?P<number>{NUMBER_PATTERN})"
            r"\s*(?P<unit>млн|тыс(?:яч)?|т\.?\s*р\.?)?"
            r"\s*(?:руб(?:\.|ля|лей|ль)?)?",
            text,
        )
    )
    if not matches:
        raise ValueError("amount is not found")
    amounts = {
        _decimal(match.group("number")) * _multiplier(match.group("unit"))
        for match in matches
    }
    if len(amounts) != 1:
        total_match = _explicit_total_amount_match(text, matches)
        if total_match is None:
            raise MultipleIndependentAmountsError(
                "multiple independent amounts without explicit roles"
            )
        amount = _decimal(total_match.group("number")) * _multiplier(
            total_match.group("unit")
        )
        return MoneyExtraction(
            amount=amount,
            amount_type="exact",
            currency="RUB",
            evidence=total_match.group(0).strip(),
        )

    match = matches[0]
    amount = amounts.pop()
    prefix = text[max(0, match.start() - 12) : match.start()].strip()
    if re.search(r"\bдо$", prefix):
        amount_type = "maximum"
    elif re.search(r"\b(?:около|примерно|порядка|бюджет)$", prefix):
        amount_type = "approximate"
    else:
        amount_type = "exact"
    return MoneyExtraction(
        amount=amount,
        amount_type=amount_type,
        currency="RUB",
        evidence=match.group(0).strip(),
    )


def _explicit_total_amount_match(
    text: str,
    matches: list[re.Match[str]],
) -> re.Match[str] | None:
    if not re.search(r"(?:цена|стоимость)\s+за\s+единиц", text):
        return None
    total_matches = [
        match
        for match in matches
        if re.search(
            r"(?:(?:общая|итоговая)\s+сумма|итого)\s*$",
            text[max(0, match.start() - 30) : match.start()].strip(),
        )
    ]
    return total_matches[0] if len(total_matches) == 1 else None


def _find_money_ranges(text: str) -> list[re.Match[str]]:
    word_range = re.compile(
        rf"(?P<evidence>\bот\s+"
        rf"(?P<left>{NUMBER_PATTERN})\s*"
        rf"(?P<left_unit>{UNIT_PATTERN})?\s+до\s+"
        rf"(?P<right>{NUMBER_PATTERN})\s*"
        rf"(?P<right_unit>{UNIT_PATTERN})?"
        rf"\s*(?:{CURRENCY_PATTERN})?)"
    )
    dash_range = re.compile(
        rf"(?P<evidence>(?:\bдиапазон\s+)?"
        rf"(?P<left>{NUMBER_PATTERN})\s*"
        rf"(?P<left_unit>{UNIT_PATTERN})?\s*"
        rf"{DASH_PATTERN}\s*"
        rf"(?P<right>{NUMBER_PATTERN})\s*"
        rf"(?P<right_unit>{UNIT_PATTERN})?"
        rf"\s*(?:{CURRENCY_PATTERN})?)"
    )
    matches = [
        *word_range.finditer(text),
        *dash_range.finditer(text),
    ]
    return sorted(matches, key=lambda match: match.start())


def normalize_budget_status(
    value: str,
) -> tuple[str | None, list[str]]:
    text = normalize_search_text(value)
    unbudgeted = bool(
        re.search(
            r"вне\s*бюджет|внебюджет|"
            r"не\s+предусмотр\w*\s+бюджет|бюджета\s+нет|"
            r"в\s+бюджет\w*\s+не\s+предусмотр",
            text,
        )
    )
    budgeted = bool(
        re.search(
            r"закупк\w*\s+бюджетн|"
            r"(?<!не\s)предусмотр\w*\s+бюджет|"
            r"\bв\s+бюджете\b|по\s+утвержденн\w+\s+статье",
            text,
        )
    )
    if budgeted and unbudgeted:
        return None, ["Противоречивые сведения о бюджетном статусе"]
    if unbudgeted:
        return "unbudgeted", []
    if budgeted:
        return "budgeted", []
    return None, []


def normalize_urgency(
    value: str,
) -> tuple[str | None, bool, list[str]]:
    text = normalize_search_text(value)
    match = re.search(r"\bp([1-4])\b", text)
    claimed = bool(
        match
        or re.search(
            r"\bсрочн|очень срочно|как можно скорее|горит|нужно вчера",
            text,
        )
    )
    if match:
        return (
            f"P{match.group(1)}",
            True,
            [
                "Приоритет указан пользователем и требует подтверждения "
                "закупщиком"
            ],
        )
    if claimed:
        return (
            None,
            True,
            ["Срочность требует проверки и не преобразована в P1/P2"],
        )
    return None, False, []


def match_category(value: str) -> tuple[str | None, list[str]]:
    text = normalize_search_text(value)
    explicit_code = re.search(r"\b([gs]\d{2})\b", text)
    if explicit_code:
        code = explicit_code.group(1).upper()
        if code in CATEGORY_ALIASES:
            return code, []
        return None, ["Неизвестный код категории"]

    matches = category_candidates(text)
    if len(matches) == 1:
        return matches.pop(), []
    if len(matches) > 1:
        return None, ["Описание соответствует нескольким категориям"]
    return None, []


def category_candidates(value: str) -> set[str]:
    text = normalize_search_text(value)
    return {
        code
        for code, aliases in CATEGORY_ALIASES.items()
        if any(alias in text for alias in aliases)
    }


def compact_category_reference() -> str:
    classifier = (
        PROJECT_ROOT
        / "knowledge_base"
        / "04_Классификатор_категорий_закупок.md"
    )
    if classifier.exists():
        rows = [
            line
            for line in classifier.read_text(encoding="utf-8").splitlines()
            if re.match(r"^\|\s*[GS]\d{2}\s*\|", line)
        ]
        if rows:
            return "\n".join(rows)
    return "\n".join(
        f"{code}: {', '.join(aliases[:2])}"
        for code, aliases in CATEGORY_ALIASES.items()
    )


def _decimal(value: str) -> Decimal:
    normalized = value.replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc


def _multiplier(value: str | None) -> Decimal:
    if not value:
        return Decimal(1)
    normalized = value.replace(" ", "")
    if normalized.startswith(("т.", "тр")):
        return Decimal("1000")
    if normalized.startswith("тыс"):
        normalized = "тысяч" if normalized.startswith("тысяч") else "тыс"
    return MULTIPLIERS.get(normalized, Decimal(1))
