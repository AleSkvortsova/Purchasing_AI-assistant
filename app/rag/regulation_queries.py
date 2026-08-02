import re
from dataclasses import dataclass
from typing import Literal

from app.rag.models import SearchResult
from app.rag.question_understanding import (
    RegulationQuestionUnderstanding,
    is_history_question,
    understand_regulation_question,
)
from app.rag.value_normalization import (
    detect_budget_status,
    normalize_regulation_text,
)

RegulationQueryIntent = Literal[
    "approval",
    "urgency",
    "transport_fields",
    "mixed_categories",
    "status",
    "category_fields",
    "budget_policy",
    "brand_policy",
    "channel",
    "request_cancellation",
    "responsibility",
    "draft_history",
    "general_help",
    "ambiguous_followup",
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
    intents: tuple[RegulationQueryIntent, ...]
    ambiguous: bool
    asks_for_example: bool
    understanding: RegulationQuestionUnderstanding


def build_regulation_query_plan(question: str) -> RegulationQueryPlan:
    original = question.strip()
    understanding = understand_regulation_question(original)
    normalized = understanding.normalized_question
    intents = _detect_intents(understanding)
    intent = intents[0]
    text_terms = _meaningful_terms(normalized)
    queries_by_intent = [
        _intent_queries(understanding, item, text_terms) for item in intents
    ]
    concise_queries = [queries[0] for queries in queries_by_intent if queries]
    concise_queries.extend(
        query for queries in queries_by_intent for query in queries[1:]
    )
    expanded = _expanded_query(normalized, intents)
    variants = tuple(
        dict.fromkeys(
            item for item in (normalized, *concise_queries, expanded) if item
        )
    )
    return RegulationQueryPlan(
        original_query=original,
        normalized_query=normalized,
        strict_query=normalized,
        text_query=" ".join(text_terms),
        broad_query=" | ".join(
            dict.fromkeys(
                text_terms
                + [term for item in intents for term in _expansion_terms(item)]
            )
        ),
        variants=variants[:5],
        intent=intent,
        intents=intents,
        ambiguous=understanding.primary_intent == "ambiguous_followup",
        asks_for_example=bool(re.search(r"\bпример\w*\b", normalized)),
        understanding=understanding,
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
    preferred = [chunk for chunk in non_examples if matching_intents(plan, chunk)]
    if preferred:
        return _select_diverse_chunks(plan, preferred, limit)
    return non_examples[:limit] if plan.intent == "generic" else []


def matching_intents(
    plan: RegulationQueryPlan,
    chunk: SearchResult,
) -> tuple[RegulationQueryIntent, ...]:
    return tuple(intent for intent in plan.intents if _supports_intent(chunk, intent))


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


def _detect_intents(
    understanding: RegulationQuestionUnderstanding,
) -> tuple[RegulationQueryIntent, ...]:
    mapped: list[RegulationQueryIntent] = []
    normalized = understanding.normalized_question
    if re.search(r"\bустн\w*\b|\bофициальн\w*\s+заяв\w*\b", normalized):
        mapped.append("channel")
    if (
        re.search(r"\bбюджет\w*|внебюджет\w*", normalized)
        and not re.search(r"\bсоглас\w*|маршрут", normalized)
    ):
        mapped.append("budget_policy")
    for intent in understanding.intents:
        if intent == "approval_route":
            mapped.append("approval")
        elif intent == "urgency_policy":
            mapped.append("urgency")
        elif intent == "status_explanation":
            mapped.append("status")
        elif intent == "request_cancellation":
            mapped.append("request_cancellation")
        elif intent == "required_fields":
            mapped.append(
                "transport_fields"
                if understanding.category_hint == "S03"
                else "category_fields"
            )
        elif intent == "category_classification":
            if understanding.purchase_type is None and re.search(
                r"вместе|одн\w*\s+заяв|\d+\s+заяв|объедин",
                understanding.normalized_question,
            ):
                mapped.append("mixed_categories")
            else:
                mapped.append("category_fields")
        elif intent == "brand_equivalent_policy":
            mapped.append("brand_policy")
        elif intent == "responsibility_policy":
            mapped.append("responsibility")
        elif intent == "draft_and_history":
            mapped.append("channel" if "устн" in normalized else "draft_history")
        elif intent == "supplier_recommendation":
            mapped.append("outside_kb")
        elif intent == "general_help":
            mapped.append("general_help")
        elif intent == "ambiguous_followup":
            mapped.append("ambiguous_followup")
    return tuple(dict.fromkeys(mapped)) or ("generic",)


def _intent_queries(
    understanding: RegulationQuestionUnderstanding,
    intent: RegulationQueryIntent,
    terms: list[str],
) -> tuple[str, ...]:
    normalized = understanding.normalized_question
    if intent == "approval":
        budget_status = detect_budget_status(normalized)
        if budget_status == "unbudgeted":
            budget = "внебюджетная"
        elif budget_status == "budgeted":
            budget = "бюджетная"
        else:
            budget = ""
        deadline = " срок согласования" if "срок" in normalized else ""
        return (f"{budget} закупка матрица согласования{deadline}".strip(),)
    if intent == "urgency":
        if understanding.category_hint == "S07":
            subject = "мероприятие"
        elif "товар" in normalized:
            subject = "товар"
        elif re.search(r"разработ|интеграц|подключ\w*.*(?:систем|сервис)", normalized):
            subject = "IT-разработка"
        else:
            subject = "категория закупки"
        return (
            "приоритет P2 высокий срок меньше нормативного срыв мероприятия",
            "срочная заявка обязательные данные причина последствия подтверждение",
            f"{subject} нормативные сроки",
        )
    if intent == "transport_fields":
        return ("S03 транспорт обязательные поля маршрут груз вес объем даты погрузка",)
    if intent == "mixed_categories":
        return (
            "в одной заявке объединены товары и услуги разных категорий "
            "разделить на отдельные заявки",
        )
    if intent == "status":
        phrase = _status_phrase(normalized)
        if phrase == "на согласовании":
            return (
                "статус На согласовании требуется решение согласующие",
                "маршрут согласования ожидание решения статус заявки",
                "На согласовании переход после согласования",
            )
        return (
            f"статус {phrase} значение переходы действия заказчика".strip(),
            "отдел закупок проверяет полноту заявки определяет способ закупки",
        )
    if intent == "request_cancellation":
        return (
            "отмена снятие заявки до статуса принята в работу",
            "потребность исчезла остановить закупку после начала согласовать",
        )
    if intent == "responsibility":
        return (
            "внутренний заказчик отвечает за характеристики технические требования",
        )
    if intent == "draft_history":
        if is_history_question(normalized):
            return ("Мои заявки последние зарегистрированные заявки пользователя",)
        return ("сохранить незавершенную заявку как черновик продолжить позже",)
    if intent in {"general_help", "ambiguous_followup"}:
        return ()
    if intent == "category_fields":
        if re.search(
            r"интеграц|it.?разработ|разработк|подключ\w*.*(?:систем|сервис)",
            normalized,
        ):
            return (
                "S05 IT-разработка обязательные поля бизнес-требования "
                "интеграции результат приемка",
            )
        if understanding.category_hint is None:
            return (
                "общие поля для всех заявок обязательны название тип "
                "категория инициатор подразделение бюджет дата",
            )
        return ("категориальные обязательные поля заявки",)
    if intent == "budget_policy":
        status = detect_budget_status(normalized)
        prefix = "внебюджетная" if status == "unbudgeted" else "бюджетная"
        return (f"{prefix} заявка можно подать дополнительное согласование",)
    if intent == "brand_policy":
        return ("конкретный бренд или эквивалент обоснование запрета",)
    if intent == "channel":
        return ("официальная заявка устная просьба регистрация через ассистента",)
    if intent == "outside_kb":
        return ()
    return (" ".join(terms[:10]),)


def _expanded_query(
    value: str,
    intents: tuple[RegulationQueryIntent, ...],
) -> str:
    base = " ".join(_meaningful_terms(value))
    expansion = " ".join(
        dict.fromkeys(term for intent in intents for term in _expansion_terms(intent))
    )
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
        "status": [
            "статус",
            "значение",
            "переходы",
            "согласующие",
            "решение",
        ],
        "category_fields": [
            "категория",
            "обязательные",
            "поля",
            "требования",
        ],
        "budget_policy": ["бюджет", "внебюджетная", "заявка", "согласование"],
        "brand_policy": ["бренд", "эквивалент", "обоснование", "референс"],
        "channel": ["официальная", "заявка", "устная", "регистрация"],
        "request_cancellation": ["отмена", "заявка", "закупщик", "статус"],
        "responsibility": ["ответственность", "характеристики", "заказчик"],
        "draft_history": ["черновик", "мои", "заявки", "история"],
        "general_help": [],
        "ambiguous_followup": [],
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
            re.search(
                r"статус|требует доработки|переход|регистрац|"
                r"проверк|провер\w*\s+полнот",
                value,
            )
        )
    if intent == "request_cancellation":
        return chunk.document_type in {"regulation", "status_guide", "faq"} and bool(
            re.search(
                r"отмен\w*|снять\s+заяв|до\s+статуса.*принята|"
                r"после\s+начала\s+закупки",
                value,
            )
        )
    if intent == "responsibility":
        return chunk.document_type in {"regulation", "faq"} and bool(
            re.search(
                r"отвечает\s+за\s+характерист|внутренн\w*\s+заказчик|"
                r"профильн\w*\s+эксперт",
                value,
            )
        )
    if intent == "draft_history":
        return chunk.document_type in {"user_guide", "faq"} and bool(
            re.search(
                r"мои\s+заяв|последн\w*\s+заяв|черновик|продолжить\s+позже",
                value,
            )
        )
    if intent in {"general_help", "ambiguous_followup"}:
        return False
    if intent == "category_fields":
        return chunk.document_type in {"field_matrix", "regulation", "faq"} and bool(
            re.search(
                r"обязательн.*пол|категориальн|бизнес-требован|"
                r"интеграц|результат|приемк|описание предмета",
                value,
            )
        )
    if intent == "budget_policy":
        return chunk.document_type in {"faq", "regulation", "approval_rules"} and bool(
            re.search(r"бюджет|внебюджет|финанс|согласован", value)
        )
    if intent == "brand_policy":
        return chunk.document_type in {"faq", "regulation", "error_guide"} and bool(
            re.search(r"бренд|эквивалент|референс|обоснов", value)
        )
    if intent == "channel":
        return chunk.document_type in {"faq", "regulation"} and bool(
            re.search(r"устн|официальн|регистрац|ассистент", value)
        )
    if intent == "outside_kb":
        return False
    return chunk.document_type not in _EXAMPLE_TYPES


def _asks_for_category_fields(value: str) -> bool:
    asks = bool(
        re.search(
            r"\b(?:обязательн\w*|сведен\w*|пол(?:я|е|ей|ях|ями)|"
            r"что\s+(?:нужно\s+)?(?:напис|указ)|"
            r"как\s+оформ|какие\s+данн\w*\s+нужн)\w*",
            value,
        )
    )
    has_subject = bool(
        re.search(
            r"\b(?:категор\w*|интеграц\w*|разработ\w*|подключ\w*|ремонт\w*|"
            r"услуг\w*|товар\w*|мероприят\w*)\b",
            value,
        )
    )
    return asks and has_subject


def _status_phrase(value: str) -> str:
    if re.search(r"закупщик\w*\s+вернул\w*\s+заяв", value):
        return "требует доработки"
    if re.search(r"отправил\w*\s+заяв\w*\s+в\s+закуп", value):
        return "передана в отдел закупок"
    phrases = (
        "передана в отдел закупок",
        "требует доработки",
        "принята в работу",
        "на согласовании",
        "поставка ожидается",
        "отменена заказчиком",
        "в работе",
        "выполнена",
        "отклонена",
        "черновик",
    )
    return next((phrase for phrase in phrases if phrase in value), "")


def _select_diverse_chunks(
    plan: RegulationQueryPlan,
    chunks: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    selected: list[SearchResult] = []
    for intent in plan.intents:
        matches = [
            chunk
            for chunk in chunks
            if chunk not in selected and intent in matching_intents(plan, chunk)
        ]
        match = min(
            matches,
            key=lambda chunk: _intent_match_priority(plan, intent, chunk),
            default=None,
        )
        if match is not None:
            selected.append(match)
    if "urgency" in plan.intents:
        for marker in (
            r"p2\b|высокий",
            r"нормативн.*срок",
            r"обязательн.*данн.*сроч",
        ):
            match = next(
                (
                    chunk
                    for chunk in chunks
                    if chunk not in selected
                    and "urgency" in matching_intents(plan, chunk)
                    and re.search(
                        marker,
                        " ".join(
                            (
                                chunk.section_path,
                                chunk.heading or "",
                                chunk.content,
                            )
                        ).casefold(),
                    )
                ),
                None,
            )
            if match is not None:
                selected.append(match)
    if (
        "status" in plan.intents
        and plan.understanding.status_name == "transferred_to_procurement"
    ):
        completeness_rule = next(
            (
                chunk
                for chunk in chunks
                if chunk not in selected
                and chunk.document_type == "regulation"
                and re.search(
                    r"провер\w*\s+полнот",
                    normalize_regulation_text(chunk.content),
                )
            ),
            None,
        )
        if completeness_rule is not None:
            selected.append(completeness_rule)
    selected.extend(chunk for chunk in chunks if chunk not in selected)
    return selected[:limit]


def _intent_match_priority(
    plan: RegulationQueryPlan,
    intent: RegulationQueryIntent,
    chunk: SearchResult,
) -> tuple[int, int]:
    value = normalize_regulation_text(
        " ".join((chunk.section_path, chunk.heading or "", chunk.content))
    )
    marker = {
        "channel": r"устн",
        "approval": r"матрица согласования",
        "transport_fields": r"s03|транспорт|перевоз",
        "mixed_categories": r"разн.*категор|отдельн.*заяв",
        "status": _status_phrase(plan.normalized_query) or r"статус",
        "category_fields": r"s05|it-разработ|категориальн.*пол",
        "budget_policy": r"внебюджет|бюджет",
        "brand_policy": r"бренд|эквивалент",
        "urgency": r"сроч|p2|нормативн",
        "request_cancellation": r"отмен|до\s+статуса.*принята",
        "responsibility": r"отвечает\s+за\s+характерист|внутренн.*заказчик",
        "draft_history": r"мои\s+заяв|черновик|продолжить\s+позже",
        "general_help": r"$^",
        "ambiguous_followup": r"$^",
        "outside_kb": r"$^",
        "generic": r".",
    }[intent]
    return (0 if re.search(marker, value) else 1, 0)
