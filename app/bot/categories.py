import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.intake.field_registry import CATEGORY_NAMES
from app.intake.models import ProcurementType, RequestDraftData

CategoryMatchKind = Literal["exact", "multiple", "none"]
SoftwareProcurementScope = Literal[
    "product", "service", "mixed", "ambiguous", "none"
]

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "G01": ("бумаг", "ручк", "канцеляр"),
    "G02": ("крес", "мебел", "шкаф", "стол", "тумб"),
    "G03": (
        "ноутбук",
        "сервер",
        "системный блок",
        "=компьютер",
        "компьютерная техник",
    ),
    "G04": ("монитор", "клавиатур", "компьютерная мыш", "мышь"),
    "G06": ("стеллаж", "складская тележк"),
    "G07": ("погрузчик", "погрузочная техник"),
    "G08": (
        "спецодеж",
        "средства индивидуальной защиты",
        "средств индивидуальной защиты",
        "=сиз",
        "защитная обув",
    ),
    "G09": (
        "моющ",
        "чистящ",
        "средства для уборк",
        "средств для уборк",
        "средство для уборк",
        "губк",
        "салфет",
        "инвентар",
    ),
    "G14": ("светиль", "ламп", "электротех", "электрическ материал"),
    "G10": ("упаковочн материал", "коробк", "пленк", "плёнк"),
    "G11": ("буклет", "каталог"),
    "G12": ("pos-материал", "рекламн стенд", "баннер"),
    "G13": ("сувенир", "мерч", "подар"),
    "G15": ("инженерн запчаст", "фильтр", "детал"),
    "S01": ("ремонт", "монтаж", "обслуживан", "установ"),
    "S02": ("уборк", "клининг"),
    "S03": ("доставк", "перевозк", "транспортные услуг"),
    "S04": ("хранен", "складские услуг"),
    "S05": (
        "разработк",
        "доработк",
        "интеграц",
        "поддержк по",
        "поддержк программ",
    ),
    "S09": ("обучен", "тренинг", "курс"),
    "S06": ("маркетингов", "исследован", "рекламные услуг"),
    "S07": ("дизайн", "фотосъем", "фотосъём", "видеосъем", "видеосъём", "контент"),
    "S08": ("мероприят", "выставк"),
    "S10": ("подбор персонал", "рекрут"),
    "S11": ("юридическ", "консалт", "аудит", "консультац"),
    "S12": ("переводческ", "перевод"),
    "S13": ("полиграфические услуг", "печать"),
    "S14": ("аренд",),
}

_AMBIGUOUS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("компьютерное оборудование", "it-оборудование", "оргтехник"), ("G03", "G04")),
    (("полиграф", "печать"), ("G11", "S13")),
)

_NATURAL_CATEGORY_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("компьютерная техник", "компьютерной техник", "компьютеры"), ("G03",)),
    (("офисная мебель", "мебель", "столы"), ("G02",)),
    (("транспорт",), ("S03",)),
    (("полиграф",), ("G11", "S13")),
    (("услуг разработк", "разработка"), ("S05",)),
)

_CATEGORY_SELECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "G05": ("лиценз", "saas", "подписк", "программ"),
    "S05": (
        "установ",
        "настрой",
        "разработ",
        "доработ",
        "интеграц",
        "программ",
    ),
}

_SOFTWARE_TERMS = ("программ", "software", "saas", "лиценз", "подписк")
_SOFTWARE_SERVICE_ACTIONS = (
    "установ",
    "настрой",
    "разработ",
    "доработ",
    "интеграц",
    "поддержк",
)
_SOFTWARE_PRODUCT_TERMS = ("лиценз", "saas", "подписк")

_ITEM_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bкомпьютер(?:а|ы|ов|ом)?\b", re.I), "компьютер", "G03"),
    (re.compile(r"\bноутбук(?:а|и|ов|ом)?\b", re.I), "ноутбук", "G03"),
    (re.compile(r"\bстол(?:а|ы|ов|ом)?\b", re.I), "стол", "G02"),
    (re.compile(r"\bтумб(?:а|ы|у|очек|очки)?\b", re.I), "тумба", "G02"),
    (re.compile(r"\bшкаф(?:а|ы|ов|ом)?\b", re.I), "шкаф", "G02"),
    (re.compile(r"\bкрес(?:ло|ла|ел)\b", re.I), "кресло", "G02"),
)

_QUANTITY_WORDS = {
    "один": Decimal("1"),
    "одна": Decimal("1"),
    "два": Decimal("2"),
    "две": Decimal("2"),
    "три": Decimal("3"),
    "четыре": Decimal("4"),
    "пять": Decimal("5"),
}


@dataclass(frozen=True)
class CategoryClassification:
    kind: CategoryMatchKind
    category_code: str | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcurementItemMatch:
    item_name: str
    procurement_type: Literal["goods", "service"]
    category_code: str
    quantity: Decimal | None = None


class DeterministicCategoryClassifier:
    def classify(
        self,
        text: str,
        procurement_type: ProcurementType | str | None = None,
    ) -> CategoryClassification:
        normalized = _normalize(text)
        type_value = (
            procurement_type.value
            if isinstance(procurement_type, ProcurementType)
            else procurement_type
        )
        software = self._classify_software(text, normalized, type_value)
        if software is not None:
            return software
        for phrases, codes in _NATURAL_CATEGORY_ALIASES:
            if any(phrase in normalized for phrase in phrases):
                candidates = self._filter_type(codes, type_value)
                if len(candidates) == 1:
                    return CategoryClassification("exact", category_code=candidates[0])
                if candidates:
                    return CategoryClassification("multiple", candidates=candidates)
        for phrases, codes in _AMBIGUOUS:
            if any(phrase in normalized for phrase in phrases):
                candidates = self._filter_type(codes, type_value)
                if len(candidates) > 1:
                    return CategoryClassification("multiple", candidates=candidates)

        matches = tuple(
            code
            for code, keywords in _KEYWORDS.items()
            if any(_matches_keyword(normalized, keyword) for keyword in keywords)
            and self._matches_type(code, type_value)
        )
        if len(matches) == 1:
            return CategoryClassification("exact", category_code=matches[0])
        if len(matches) > 1:
            return CategoryClassification("multiple", candidates=matches[:4])
        return CategoryClassification("none")

    def classify_draft(self, draft: RequestDraftData) -> CategoryClassification:
        text = " ".join(
            value
            for value in (
                draft.item_name,
                draft.description,
                draft.specifications,
                draft.desired_result,
            )
            if value
        )
        return self.classify(text, draft.procurement_type)

    @staticmethod
    def category_by_name(value: str) -> str | None:
        normalized = _normalize(value)
        return next(
            (
                code
                for code, name in CATEGORY_NAMES.items()
                if _normalize(name) == normalized
            ),
            None,
        )

    def match_candidate_name(
        self,
        value: str,
        candidates: tuple[str, ...],
    ) -> str | None:
        normalized = _normalize(value)
        has_po_acronym = re.search(r"\bПО\b", value) is not None
        matches = tuple(
            code
            for code in candidates
            if normalized in _normalize(CATEGORY_NAMES[code])
            or _normalize(CATEGORY_NAMES[code]) in normalized
        )
        alias_matches = tuple(
            code
            for code in candidates
            if (
                (has_po_acronym and code in {"G05", "S05"})
                or any(
                    _matches_keyword(normalized, alias)
                    for alias in _CATEGORY_SELECTION_ALIASES.get(code, ())
                )
            )
        )
        if len(alias_matches) == 1:
            return alias_matches[0]
        if len(matches) == 1:
            return matches[0]
        classification = self.classify(normalized)
        if (
            classification.kind == "exact"
            and classification.category_code in candidates
        ):
            return classification.category_code
        return None

    @classmethod
    def _classify_software(
        cls,
        source_text: str,
        normalized: str,
        procurement_type: str | None,
    ) -> CategoryClassification | None:
        scope = classify_software_procurement_scope(source_text)
        if scope == "none":
            return None
        if scope == "service":
            codes = cls._filter_type(("S05",), procurement_type)
        elif scope == "product":
            codes = cls._filter_type(("G05",), procurement_type)
        elif scope in {"mixed", "ambiguous"}:
            codes = ("G05", "S05")
        else:
            codes = cls._filter_type(("G05", "S05"), procurement_type)
        if len(codes) == 1:
            return CategoryClassification("exact", category_code=codes[0])
        if codes:
            return CategoryClassification("multiple", candidates=codes)
        return CategoryClassification("none")

    @staticmethod
    def labels(codes: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(f"{code} — {CATEGORY_NAMES[code]}" for code in codes)

    @classmethod
    def _filter_type(
        cls,
        codes: tuple[str, ...],
        procurement_type: str | None,
    ) -> tuple[str, ...]:
        return tuple(
            code for code in codes if cls._matches_type(code, procurement_type)
        )[:4]

    @staticmethod
    def _matches_type(code: str, procurement_type: str | None) -> bool:
        if procurement_type == ProcurementType.GOODS.value:
            return code.startswith("G")
        if procurement_type == ProcurementType.SERVICE.value:
            return code.startswith("S")
        return True


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _matches_keyword(text: str, keyword: str) -> bool:
    if keyword.startswith("="):
        return re.search(rf"\b{re.escape(keyword[1:])}\b", text) is not None
    return keyword in text


def classify_software_procurement_scope(text: str) -> SoftwareProcurementScope:
    normalized = _normalize(text)
    has_software = re.search(r"\bПО\b", text) is not None or any(
        _matches_keyword(normalized, term) for term in _SOFTWARE_TERMS
    )
    if not has_software:
        return "none"
    service_action = any(
        term in normalized for term in _SOFTWARE_SERVICE_ACTIONS
    )
    product_action = bool(
        re.search(r"\b(?:купить|закупить|приобрести)\w*\b", normalized)
        or "нужна подписка" in normalized
        or "нужны лицензии" in normalized
        or "лицензий еще нет" in normalized
    )
    owned = bool(
        re.search(
            r"лиценз\w*\s+(?:уже\s+)?(?:есть|куплен\w*|приобретен\w*)",
            normalized,
        )
        or re.search(r"(?:уже\s+)?(?:куплен\w*|приобретен\w*)\s+лиценз", normalized)
        or "нужна только установка" in normalized
        or "только установить" in normalized
        or "только настроить" in normalized
    )
    combined = bool(
        (product_action and service_action)
        or re.search(r"лиценз\w*.*вместе\s+с\s+(?:установ|настрой)", normalized)
        or re.search(r"(?:и\s+)?приобрести.*(?:и\s+)?настроить", normalized)
    )
    if combined:
        return "mixed"
    if owned:
        return "service"
    if product_action:
        return "product"
    if service_action and any(
        term in normalized for term in ("лиценз", "подписк", "новое по")
    ):
        return "ambiguous"
    if service_action:
        return "service"
    if any(term in normalized for term in _SOFTWARE_PRODUCT_TERMS):
        return "product"
    return "ambiguous"


def normalize_software_scope_reply(text: str) -> SoftwareProcurementScope:
    normalized = _normalize(text)
    if (
        re.search(
            r"(?:купить|закупить|приобрести)\w*.*(?:установ|настро)", normalized
        )
        or re.search(
            r"(?:установ|настро)\w*.*(?:купить|закупить|приобрести)", normalized
        )
        or "лицензии вместе с установкой" in normalized
        or "нужны лицензии вместе с установкой" in normalized
        or "и приобрести, и настроить" in normalized
    ):
        return "mixed"
    if (
        re.search(r"лиценз\w*\s+(?:уже\s+)?(?:есть|куплен\w*)", normalized)
        or normalized in {"куплены", "уже куплены"}
        or "нужна только установка" in normalized
        or "только установить" in normalized
        or "только настроить" in normalized
    ):
        return "service"
    if (
        re.search(r"(?:купить|закупить|приобрести)\w*.*лиценз", normalized)
        or "лицензий еще нет" in normalized
        or "нужна подписка" in normalized
        or re.search(r"закупить\w*\s+(?:новое\s+)?по\b", normalized)
    ):
        return "product"
    return classify_software_procurement_scope(text)


def extract_procurement_items(text: str) -> tuple[ProcurementItemMatch, ...]:
    matches: list[tuple[int, ProcurementItemMatch]] = []
    for pattern, item_name, category_code in _ITEM_PATTERNS:
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 18) : match.start()].casefold()
            quantity = _quantity_before(prefix)
            matches.append(
                (
                    match.start(),
                    ProcurementItemMatch(
                        item_name=item_name,
                        procurement_type="goods",
                        category_code=category_code,
                        quantity=quantity,
                    ),
                )
            )
    matches.sort(key=lambda item: item[0])
    unique: list[ProcurementItemMatch] = []
    for _, item in matches:
        if item.item_name not in {existing.item_name for existing in unique}:
            unique.append(item)
    return tuple(unique)


def _quantity_before(prefix: str) -> Decimal | None:
    match = re.search(r"(?:^|\s)(\d+(?:[.,]\d+)?|[а-я]+)\s*$", prefix)
    if match is None:
        return None
    token = match.group(1).replace(",", ".")
    if token in _QUANTITY_WORDS:
        return _QUANTITY_WORDS[token]
    try:
        return Decimal(token)
    except Exception:
        return None
