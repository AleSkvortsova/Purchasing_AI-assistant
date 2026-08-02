import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.bot.categories import DeterministicCategoryClassifier
from app.bot.normalization import (
    CARDINAL_WORD_PATTERN,
    NaturalDateParser,
    amount_evidence,
    find_amount_expression,
    normalize_budget_status,
    normalize_unit,
    parse_amount_expression,
    parse_cardinal,
)
from app.intake.models import FieldCorrection, IntakeFieldUpdate, UpdateSource

_LEADING = re.compile(
    r"^(?:(?:мне|нам)\s+)?(?:нужно|нужны|необходимо|хочу|хотим)\s+",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"^(?:купить|закупить|приобрести|заказать|закаж(?:и|ите))\s+",
    re.IGNORECASE,
)
_NUMBER_PATTERN = (
    rf"(?:\d+(?:[.,]\d+)?|{CARDINAL_WORD_PATTERN}"
    rf"(?:\s+{CARDINAL_WORD_PATTERN})?)"
)
_COUNT_PREFIX = re.compile(
    rf"^(?P<quantity>{_NUMBER_PATTERN})\s+",
    re.IGNORECASE,
)
_EXPLICIT_UNIT = re.compile(
    r"^(?P<unit>шт\.?|штук(?:а|и)?|единиц(?:а|ы)?|"
    r"кг|килограмм(?:а|ов)?|л\.?|литр(?:а|ов)?|"
    r"м|метр(?:а|ов)?|м²|м2|кв\.\s*м|"
    r"упак\.?|упаков(?:ка|ки|ок)|короб(?:ка|ки|ок)|"
    r"комплект(?:а|ов)?|час(?:а|ов)?)\b\.?\s*",
    re.IGNORECASE,
)
_MULTIPLE_ITEM = re.compile(
    rf"\b(?:и|,)\s+(?P<quantity>{_NUMBER_PATTERN})\s+"
    r"(?P<noun>[а-яё][а-яё-]*)",
    re.IGNORECASE,
)
_CAPACITY = re.compile(
    rf"\b(?P<capacity>(?:объ[её]м(?:ом)?(?:\s+кажд\w*)?|по)\s+"
    rf"(?P<number>{_NUMBER_PATTERN})\s*"
    r"(?P<unit>л\.?|литр(?:а|ов)?|кг|килограмм(?:а|ов)?|"
    r"м|метр(?:а|ов)?|м²|м2))\b",
    re.IGNORECASE,
)
_FREQUENCY = re.compile(
    rf"\s+(?:(?:{_NUMBER_PATTERN})\s+)?раз(?:а)?\s+в\s+\S+(?:\s+|$)",
    re.IGNORECASE,
)
_LOCATION = re.compile(
    r"(?:с\s+доставкой|доставить|поставить|по\s+адресу)\s+"
    r"(?:в|на|по\s+адресу)?\s*(?P<location>[^,.;]+)$",
    re.IGNORECASE,
)
_PLACE_LOCATION = re.compile(
    r"\b(?P<location>(?:(?:в|на)\s+)?"
    r"(?:офис(?:е)?|переговорной|склад(?:е)?|помещении|объекте)\s+"
    r"(?:на|в|по)\s+[^,.;]+?)"
    r"(?=\s+(?:в\s+срок|не\s+позднее|не\s+позже|"
    r"до\s+\d|к\s+\d|бюджет)|[,.;]|$)",
    re.IGNORECASE,
)
_SERVICE_VOLUME = re.compile(
    rf"\b(?P<count>{_NUMBER_PATTERN})\s+"
    r"(?P<object>[а-яё][а-яё-]*)"
    r"(?P<tail>[^,.;]*)",
    re.IGNORECASE,
)
_CANONICAL_ITEMS = {
    "офисных кресел": "офисные кресла",
    "ноутбуков": "ноутбуки",
}


@dataclass(frozen=True)
class ExtractedIntakeEntities:
    values: dict[str, object] = field(default_factory=dict)
    evidence_by_field: dict[str, str] = field(default_factory=dict)
    corrections: tuple[FieldCorrection, ...] = ()
    suppressed_extraction_fields: tuple[str, ...] = ()

    def to_update(self) -> IntakeFieldUpdate:
        return IntakeFieldUpdate(
            values=self.values,
            source=UpdateSource.EXTRACTION,
            evidence_by_field=self.evidence_by_field,
            explicit_correction=bool(self.corrections),
            corrections=list(self.corrections),
            suppressed_extraction_fields=list(self.suppressed_extraction_fields),
        )


class DeterministicEntityExtractor:
    def __init__(
        self,
        category_classifier: DeterministicCategoryClassifier | None = None,
        date_parser: NaturalDateParser | None = None,
    ) -> None:
        self._categories = category_classifier or DeterministicCategoryClassifier()
        self._dates = date_parser or NaturalDateParser()

    def extract(self, text: str) -> ExtractedIntakeEntities:
        original = " ".join(text.strip().split())
        values: dict[str, object] = {}
        evidence: dict[str, str] = {}
        suppressed: set[str] = set()
        corrections, correction_spans = _extract_corrections(
            original, self._dates
        )
        category = self._categories.classify(original)
        procurement_type = self._procurement_type(original, category.category_code)
        if procurement_type is not None:
            values["procurement_type"] = procurement_type
            category = self._categories.classify(original, procurement_type)
        if category.kind == "exact" and category.category_code is not None:
            values["category_code"] = category.category_code
            evidence["category_code"] = original

        working = original
        for span in sorted(correction_spans, reverse=True):
            working = _remove_span(working, span)
        working = _ACTION.sub("", _LEADING.sub("", working)).strip()
        amount_match = find_amount_expression(working)
        if amount_match is not None:
            values["amount"] = amount_match.parsed.amount
            evidence["amount"] = amount_evidence(amount_match.parsed)
            working = _remove_span(working, amount_match.span)
        date_match = self._dates.search(working)
        if date_match is not None:
            parsed_date, span = date_match
            values["desired_delivery_date"] = parsed_date
            working = _remove_span(working, span)
        location_match = _find_location(working)
        if location_match is not None:
            values["delivery_location"] = location_match.group("location").strip()
            working = _remove_span(working, location_match.span())

        item_text = _clean_text(working)
        if procurement_type != "service":
            if _starts_with_multiple_goods_positions(item_text):
                suppressed.update({"quantity", "unit"})
            item_text, count_values, count_evidence = _extract_goods_count(
                item_text
            )
            values.update(count_values)
            evidence.update(count_evidence)
            item_text, capacity = _extract_capacity(item_text)
            if capacity:
                values["specifications"] = capacity
        if procurement_type == "service":
            item_text = _FREQUENCY.sub(" ", item_text).strip()
            item_text, details = _split_service_text(item_text)
            if details:
                values["specifications"] = details
            if "category_code" not in values:
                refined = self._categories.classify(item_text, procurement_type)
                if refined.kind == "exact" and refined.category_code is not None:
                    values["category_code"] = refined.category_code
        item_text = _CANONICAL_ITEMS.get(item_text.casefold(), item_text)
        values["item_name"] = item_text or original
        try:
            values["budget_status"] = normalize_budget_status(original)
            evidence["budget_status"] = original
        except ValueError:
            pass
        for correction in corrections:
            values[correction.target_field] = correction.new_value
            evidence[correction.target_field] = str(correction.new_value)
        return ExtractedIntakeEntities(
            values,
            evidence,
            tuple(corrections),
            tuple(sorted(suppressed)),
        )

    @staticmethod
    def _procurement_type(text: str, category_code: str | None) -> str | None:
        normalized = text.casefold().replace("ё", "е")
        if re.search(r"\b(?:купить|закупить|приобрести|товар)\w*", normalized):
            return "goods"
        if re.search(
            r"\b(?:услуг|уборк|клининг|ремонт|монтаж|разработк|сборк|"
            r"установ|настройк|обслуживан|перевозк|доставк|заправк|"
            r"помыть|мыть|мойк)\w*",
            normalized,
        ):
            return "service"
        if category_code:
            return "goods" if category_code.startswith("G") else "service"
        return None


def _remove_span(value: str, span: tuple[int, int]) -> str:
    return _clean_text(value[: span[0]] + " " + value[span[1] :])


def _clean_text(value: str) -> str:
    cleaned = re.sub(r"[\s\u00a0]+", " ", value)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",(?:\s*,)+", ",", cleaned)
    cleaned = re.sub(r"(?:\.\s*){2,}", ". ", cleaned)
    cleaned = re.sub(r"\b(?:бюджет|сумма)\s*(?=,|\.|$)", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:до|на)\s*(?=,|\.|$)", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"[,;]?\s*(?:доставить|поставить|получить)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r"[,.;](?:\s*[,.;])+", ",", cleaned)
    return cleaned.strip(" ,.;")


def _split_service_text(value: str) -> tuple[str, str | None]:
    text = _clean_text(value)
    area = re.search(
        r"\bплощадью\s+(?P<area>\d+(?:[.,]\d+)?)\s*(?P<unit>м²|м2|кв\.\s*м)\b",
        text,
        re.IGNORECASE,
    )
    if area is not None:
        item = _clean_text(text[: area.start()] + " " + text[area.end() :])
        details = f"площадь {area.group('area')} м²"
        return _canonical_service_item(item), details
    volume = _SERVICE_VOLUME.search(text)
    if volume is not None:
        details = _clean_text(volume.group())
        prefix = _clean_text(text[: volume.start()])
        prefix = re.sub(
            r"^(?:организовать|заказать|провести)\s+",
            "",
            prefix,
            flags=re.IGNORECASE,
        )
        object_name = volume.group("object")
        item = prefix
        if object_name.casefold() not in item.casefold():
            item = f"{item} {object_name}".strip()
        return _canonical_service_item(item), details
    location = re.search(r"\s+(?P<detail>в\s+серверной)\s*$", text, re.IGNORECASE)
    if location is not None:
        return (
            _canonical_service_item(text[: location.start()]),
            location.group("detail"),
        )
    return _canonical_service_item(text), None


def _canonical_service_item(value: str) -> str:
    item = _clean_text(value)
    item = re.sub(
        r"^еженедельную\s+уборку\b",
        "еженедельная уборка",
        item,
        flags=re.IGNORECASE,
    )
    return item


def _find_location(value: str):
    action_match = _LOCATION.search(value)
    place_match = _PLACE_LOCATION.search(value)
    if action_match is None:
        return place_match
    if place_match is None:
        return action_match
    return max((action_match, place_match), key=lambda match: len(match.group()))


def _extract_goods_count(
    value: str,
) -> tuple[str, dict[str, object], dict[str, str]]:
    match = _COUNT_PREFIX.match(value)
    if match is None:
        return value, {}, {}
    raw_quantity = match.group("quantity")
    try:
        quantity = Decimal(str(parse_cardinal(raw_quantity)))
    except ValueError:
        try:
            quantity = Decimal(raw_quantity.replace(",", "."))
        except Exception:
            return value, {}, {}
    remainder = value[match.end() :]
    unit_match = _EXPLICIT_UNIT.match(remainder)
    if _has_independent_second_item(remainder):
        return value, {}, {}
    evidence = {"quantity": raw_quantity}
    if unit_match is not None:
        raw_unit = unit_match.group("unit")
        unit = normalize_unit(raw_unit)
        item_remainder = remainder[unit_match.end() :].strip()
        if item_remainder:
            remainder = item_remainder
        evidence["unit"] = raw_unit
    else:
        noun = re.match(r"(?P<noun>[а-яё][а-яё-]*)", remainder, re.IGNORECASE)
        if noun is None:
            return value, {}, {}
        unit = "шт."
        evidence["unit"] = f"{raw_quantity} {noun.group('noun')}"
    return (
        _clean_text(remainder),
        {"quantity": quantity, "unit": unit},
        evidence,
    )


def _has_independent_second_item(value: str) -> bool:
    for match in _MULTIPLE_ITEM.finditer(value):
        noun = match.group("noun")
        try:
            normalize_unit(noun)
        except ValueError:
            return True
    return False


def _starts_with_multiple_goods_positions(value: str) -> bool:
    match = _COUNT_PREFIX.match(value)
    return bool(match and _has_independent_second_item(value[match.end() :]))


def _extract_capacity(value: str) -> tuple[str, str | None]:
    match = _CAPACITY.search(value)
    if match is None:
        return value, None
    return (
        _clean_text(value[: match.start()] + " " + value[match.end() :]),
        match.group("capacity"),
    )


def _extract_corrections(
    text: str,
    dates: NaturalDateParser,
) -> tuple[list[FieldCorrection], list[tuple[int, int]]]:
    corrections: list[FieldCorrection] = []
    spans: list[tuple[int, int]] = []
    money = (
        r"\d[\d\s\u00a0]*(?:[.,]\d+)?\s*"
        r"(?:млн\.?|миллион(?:а|ов)?|тыс\.?|тысяч(?:а|и)?)?\s*"
        r"(?:рубл(?:ь|я|ей)|руб\.?|₽)?"
    )
    amount_match = re.search(
        rf"\b(?:сумма|бюджет)\s+не\s+(?P<old>{money})\s*,?\s*"
        rf"а\s+(?P<new>{money})",
        text,
        re.IGNORECASE,
    )
    if amount_match is not None:
        old_amount = parse_amount_expression(amount_match.group("old")).amount
        new_amount = parse_amount_expression(amount_match.group("new")).amount
        corrections.append(
            FieldCorrection(
                target_field="amount",
                old_value=old_amount,
                new_value=new_amount,
            )
        )
        spans.append(amount_match.span())

    date_expression = r"\d{1,2}\s+[А-Яа-яЁё]+(?:\s+\d{4})?"
    date_match = re.search(
        rf"\b(?:дата\s+)?не\s+(?P<old>{date_expression})\s*,?\s*"
        rf"а\s+(?P<new>{date_expression})",
        text,
        re.IGNORECASE,
    )
    if date_match is not None:
        corrections.append(
            FieldCorrection(
                target_field="desired_delivery_date",
                old_value=dates.parse(date_match.group("old")),
                new_value=dates.parse(date_match.group("new")),
            )
        )
        spans.append(date_match.span())

    quantity_match = re.search(
        rf"\bне\s+(?P<old>{_NUMBER_PATTERN})\s*,?\s*а\s+"
        rf"(?P<new>{_NUMBER_PATTERN})"
        r"(?:\s*(?P<unit>шт\.?|штук(?:а|и)?|единиц(?:а|ы)?))?",
        text,
        re.IGNORECASE,
    )
    if quantity_match is not None and amount_match is None:
        old_quantity = _quantity_decimal(quantity_match.group("old"))
        new_quantity = _quantity_decimal(quantity_match.group("new"))
        corrections.append(
            FieldCorrection(
                target_field="quantity",
                old_value=old_quantity,
                new_value=new_quantity,
            )
        )
        spans.append(quantity_match.span())
    elif amount_match is None:
        quantity_replacement = re.search(
            r"(?:\bвместо\s+(?P<instead_old>\d+(?:[.,]\d+)?)\s*"
            r"[—–-]\s*(?P<instead_new>\d+(?:[.,]\d+)?)|"
            r"\bпоменяйте(?:\s+количество)?\s+(?P<change_old>\d+(?:[.,]\d+)?)"
            r"\s+на\s+(?P<change_new>\d+(?:[.,]\d+)?)|"
            r"\bколичество\s+(?:будет|составит)\s+"
            r"(?P<declared_new>\d+(?:[.,]\d+)?))"
            r"\s*(?:шт\.?|штук(?:а|и)?|единиц(?:а|ы)?|крес\w+|товар\w+)",
            text,
            re.IGNORECASE,
        )
        if quantity_replacement is not None:
            old_raw = (
                quantity_replacement.group("instead_old")
                or quantity_replacement.group("change_old")
            )
            new_raw = (
                quantity_replacement.group("instead_new")
                or quantity_replacement.group("change_new")
                or quantity_replacement.group("declared_new")
            )
            corrections.append(
                FieldCorrection(
                    target_field="quantity",
                    old_value=(
                        Decimal(old_raw.replace(",", "."))
                        if old_raw is not None
                        else None
                    ),
                    new_value=Decimal(new_raw.replace(",", ".")),
                )
            )
            spans.append(quantity_replacement.span())

    budget_match = re.search(
        r"бюджет\s+не\s+предусмотрен\w*\s*[,.;—–-]?\s*"
        r"(?:я\s+)?(?:ошиб\w*\s*[—–-]?\s*)?предусмотрен\w*",
        text,
        re.IGNORECASE,
    )
    if budget_match is not None:
        corrections.append(
            FieldCorrection(
                target_field="budget_status",
                old_value="unbudgeted",
                new_value="budgeted",
            )
        )
        spans.append(budget_match.span())
    return corrections, spans


def _quantity_decimal(value: str) -> Decimal:
    try:
        return Decimal(parse_cardinal(value))
    except ValueError:
        return Decimal(value.replace(",", "."))
