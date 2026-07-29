from dataclasses import dataclass
from typing import Literal

from app.intake.field_registry import CATEGORY_NAMES
from app.intake.models import ProcurementType, RequestDraftData

CategoryMatchKind = Literal["exact", "multiple", "none"]

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "G02": ("крес", "мебел", "шкаф", "офисный стол", "офисного стола"),
    "G03": ("ноутбук", "сервер", "системный блок"),
    "G04": ("монитор", "клавиатур", "компьютерная мыш", "мышь"),
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
    "S01": ("ремонт", "монтаж", "обслуживан"),
    "S02": ("уборк", "клининг"),
    "S03": ("доставк", "перевозк", "транспортные услуг"),
    "S05": (
        "разработк",
        "интеграц",
        "поддержк по",
        "поддержк программ",
    ),
    "S09": ("обучен", "тренинг", "курс"),
    "S14": ("аренд",),
}

_AMBIGUOUS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("компьютерное оборудование", "it-оборудование"), ("G03", "G04")),
    (("полиграф", "печать"), ("G11", "S13")),
)


@dataclass(frozen=True)
class CategoryClassification:
    kind: CategoryMatchKind
    category_code: str | None = None
    candidates: tuple[str, ...] = ()


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
        for phrases, codes in _AMBIGUOUS:
            if any(phrase in normalized for phrase in phrases):
                candidates = self._filter_type(codes, type_value)
                if len(candidates) > 1:
                    return CategoryClassification("multiple", candidates=candidates)

        matches = tuple(
            code
            for code, keywords in _KEYWORDS.items()
            if any(keyword in normalized for keyword in keywords)
            and self._matches_type(code, type_value)
        )
        if len(matches) == 1:
            return CategoryClassification("exact", category_code=matches[0])
        if len(matches) > 1:
            return CategoryClassification("multiple", candidates=matches[:4])
        return CategoryClassification("none")

    def classify_draft(self, draft: RequestDraftData) -> CategoryClassification:
        text = " ".join(
            value for value in (draft.item_name, draft.description) if value
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
