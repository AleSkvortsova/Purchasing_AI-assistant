import re
from dataclasses import dataclass
from typing import Literal

from app.rag.models import SearchResult
from app.rag.value_normalization import normalize_regulation_text

RegulationQueryIntent = Literal[
    "approval",
    "urgency",
    "transport_fields",
    "mixed_categories",
    "status",
    "channel",
    "outside_kb",
    "generic",
]

_STOP_WORDS = {
    "а",
    "будет",
    "в",
    "для",
    "за",
    "и",
    "как",
    "какая",
    "какие",
    "какой",
    "кто",
    "ли",
    "мне",
    "может",
    "можно",
    "на",
    "нужно",
    "о",
    "по",
    "с",
    "считаться",
    "что",
    "это",
}
_CURRENCY_WORDS = {"р", "руб", "рубль", "рубля", "рублей", "рубли"}
_EXAMPLE_TYPES = {"examples", "template"}


@dataclass(frozen=True)
class RegulationQueryPlan:
    original_query: str
    normalized_query: str
    strict_query: str
    text_query: str
    broad_query: str
    variants: tuple[str, ...]
    intent: RegulationQueryIntent
    ambiguous: bool
    asks_for_example: bool


def build_regulation_query_plan(question: str) -> RegulationQueryPlan:
    original = question.strip()
    normalized = normalize_regulation_query(original)
    intent = _detect_intent(normalized)
    text_terms = _meaningful_terms(normalized)
    concise = _concise_query(normalized, intent, text_terms)
    expanded = _expanded_query(normalized, intent)
    variants = tuple(
        dict.fromkeys(item for item in (normalized, concise, expanded) if item)
    )
    return RegulationQueryPlan(
        original_query=original,
        normalized_query=normalized,
        strict_query=normalized,
        text_query=" ".join(text_terms),
        broad_query=" | ".join(dict.fromkeys(text_terms + _expansion_terms(intent))),
        variants=variants[:3],
        intent=intent,
        ambiguous=_is_ambiguous(normalized),
        asks_for_example=bool(re.search(r"\bпример\w*\b", normalized)),
    )


def normalize_regulation_query(value: str) -> str:
    return normalize_regulation_text(value)


def fuse_regulation_results(
    ranked_lists: list[list[SearchResult]],
    *,
    rrf_k: int,
) -> list[SearchResult]:
    scores: dict[object, float] = {}
    results: dict[object, SearchResult] = {}
    variants: dict[object, list[int]] = {}
    for variant_index, ranked in enumerate(ranked_lists):
        for rank, result in enumerate(ranked, start=1):
            scores[result.chunk_id] = scores.get(result.chunk_id, 0.0) + (
                1.0 / (rrf_k + rank)
            )
            results.setdefault(result.chunk_id, result)
            variants.setdefault(result.chunk_id, []).append(variant_index)
    ordered = sorted(
        results.values(),
        key=lambda item: (
            -scores[item.chunk_id],
            item.priority,
            str(item.chunk_id),
        ),
    )
    return [
        item.model_copy(
            update={
                "metadata": {
                    **item.metadata,
                    "regulation_query_rrf_score": scores[item.chunk_id],
                    "regulation_query_variants": variants[item.chunk_id],
                }
            }
        )
        for item in ordered
    ]


def select_relevant_regulation_chunks(
    plan: RegulationQueryPlan,
    chunks: list[SearchResult],
    *,
    limit: int,
) -> list[SearchResult]:
    if plan.asks_for_example:
        return chunks[:limit]
    non_examples = [
        chunk for chunk in chunks if chunk.document_type not in _EXAMPLE_TYPES
    ]
    if not non_examples:
        return []
    preferred = [
        chunk for chunk in non_examples if _supports_intent(chunk, plan.intent)
    ]
    if preferred:
        return preferred[:limit]
    return non_examples[:limit] if plan.intent == "generic" else []


def source_kind(document_type: str) -> Literal[
    "normative", "instruction", "faq", "example", "template"
]:
    if document_type == "examples":
        return "example"
    if document_type == "template":
        return "template"
    if document_type == "faq":
        return "faq"
    if document_type in {"user_guide", "error_guide"}:
        return "instruction"
    return "normative"


def _meaningful_terms(value: str) -> list[str]:
    terms = re.findall(r"[a-zа-я0-9]+", value)
    return [
        term
        for term in terms
        if term not in _STOP_WORDS
        and term not in _CURRENCY_WORDS
        and not term.isdigit()
    ]


def _detect_intent(value: str) -> RegulationQueryIntent:
    if re.search(
        r"сам\w*\s+дешев|кто\s+прода\w*|"
        r"какой\s+поставщик\s+сейчас|текущ\w*\s+цен",
        value,
    ):
        return "outside_kb"
    if re.search(r"\bсоглас\w*\b", value):
        return "approval"
    if re.search(r"\bустн\w*\b|\bофициальн\w*\s+заяв\w*\b", value):
        return "channel"
    if re.search(r"\bсроч\w*\b|\bприоритет\w*\b", value):
        return "urgency"
    if re.search(r"\bперевоз\w*\b|\bтранспорт\w*\b|\bпаллет\w*\b", value) and re.search(
        r"\bсведен\w*\b|\bпол\w*\b|\bуказ\w*\b|\bнуж\w*\b", value
    ):
        return "transport_fields"
    if re.search(r"\bобъедин\w*\b|\bразн\w*\s+категор\w*\b", value):
        return "mixed_categories"
    if "товар" in value and "услуг" in value and re.search(r"\bзаяв\w*\b", value):
        return "mixed_categories"
    if re.search(r"\bстатус\w*\b|требует доработки", value):
        return "status"
    return "generic"


def _concise_query(
    normalized: str,
    intent: RegulationQueryIntent,
    terms: list[str],
) -> str:
    if intent == "approval":
        if "внебюдж" in normalized:
            budget = "внебюджетная"
        elif "бюджет" in normalized:
            budget = "бюджетная"
        else:
            budget = ""
        deadline = " срок согласования" if "срок" in normalized else ""
        return f"{budget} закупка матрица согласования{deadline}".strip()
    if intent == "urgency":
        subject = "мероприятие " if "мероприят" in normalized else ""
        return f"{subject}критерии срочной заявки нормативный срок".strip()
    if intent == "transport_fields":
        return "S03 транспорт обязательные поля маршрут груз вес объем даты погрузка"
    if intent == "mixed_categories":
        return "разные категории товары услуги отдельные заявки"
    if intent == "status":
        phrase = "требует доработки" if "требует доработки" in normalized else ""
        return f"статус {phrase} значение переходы".strip()
    if intent == "channel":
        return "официальная заявка устная просьба регистрация через ассистента"
    if intent == "outside_kb":
        return ""
    return " ".join(terms[:10])


def _expanded_query(value: str, intent: RegulationQueryIntent) -> str:
    base = " ".join(_meaningful_terms(value))
    expansion = " ".join(_expansion_terms(intent))
    return f"{base} {expansion}".strip()


def _expansion_terms(intent: RegulationQueryIntent) -> list[str]:
    return {
        "approval": [
            "согласование",
            "согласующие",
            "маршрут",
            "матрица",
            "срок",
        ],
        "urgency": [
            "срочная",
            "срочность",
            "приоритет",
            "нормативный",
            "обязательные",
            "данные",
        ],
        "transport_fields": [
            "S03",
            "транспорт",
            "маршрут",
            "груз",
            "вес",
            "объем",
            "даты",
            "погрузка",
        ],
        "mixed_categories": ["категории", "однородные", "отдельные", "заявки"],
        "status": ["статус", "значение", "переходы"],
        "channel": ["официальная", "заявка", "устная", "регистрация"],
        "outside_kb": [],
        "generic": [],
    }[intent]


def _is_ambiguous(value: str) -> bool:
    return bool(
        len(value.split()) <= 5
        and re.search(r"\b(это|этот|эту|его|ее|её)\b", value)
        and re.search(r"\bсоглас\w*\b", value)
    )


def _supports_intent(chunk: SearchResult, intent: RegulationQueryIntent) -> bool:
    value = " ".join(
        (chunk.document_title, chunk.section_path, chunk.heading or "", chunk.content)
    ).casefold().replace("ё", "е")
    if intent == "approval":
        return chunk.document_type == "approval_rules" and bool(
            re.search(r"согласующ|срок ответа|матрица согласования", value)
        )
    if intent == "urgency":
        supported_types = {"urgency_rules", "field_matrix"}
        return chunk.document_type in supported_types and bool(
            re.search(r"сроч|приоритет|нормативн|мероприят", value)
        )
    if intent == "transport_fields":
        supported_types = {"field_matrix"}
        return chunk.document_type in supported_types and bool(
            re.search(r"s03|транспорт|перевоз|маршрут|груз|погруз", value)
        )
    if intent == "mixed_categories":
        return chunk.document_type in {"error_guide", "regulation", "faq"} and bool(
            re.search(r"разн.*категор|однородн|отдельн.*заяв|объедин", value)
        )
    if intent == "status":
        return chunk.document_type in {"status_guide", "faq", "regulation"} and bool(
            re.search(r"статус|требует доработки|переход", value)
        )
    if intent == "channel":
        return chunk.document_type in {"faq", "regulation"} and bool(
            re.search(r"устн|официальн|регистрац|ассистент", value)
        )
    if intent == "outside_kb":
        return False
    return chunk.document_type not in _EXAMPLE_TYPES
