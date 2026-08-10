from uuid import UUID

import pytest

from app.rag.models import HybridRetrievalResult
from app.rag.regulation_queries import (
    build_regulation_query_plan,
    fuse_regulation_results,
    normalize_regulation_query,
    select_relevant_regulation_chunks,
)


@pytest.mark.parametrize(
    "value",
    [
        "180000 рублей",
        "180 000 рублей",
        "180\u00a0000 рублей",
        "180.000 рублей",
        "180 тыс рублей",
        "180 тысяч рублей",
    ],
)
def test_amount_spellings_have_one_search_normalization(value: str) -> None:
    assert normalize_regulation_query(value) == "180000 рублей"


@pytest.mark.parametrize(
    "word",
    ["срочная", "срочной", "срочность"],
)
def test_urgency_morphology_selects_urgency_query(word: str) -> None:
    plan = build_regulation_query_plan(f"В каком случае заявка {word}?")
    assert plan.intent == "urgency"
    assert "срочность" in plan.broad_query
    assert any("нормативн" in query for query in plan.variants)


def test_approval_query_adds_route_and_deadline_terminology() -> None:
    plan = build_regulation_query_plan(
        "Закупка на 180 000 рублей предусмотрена бюджетом. "
        "Кто её согласует и за какой срок?"
    )
    assert plan.normalized_query.startswith("закупка на 180000 рублей")
    assert plan.intent == "approval"
    assert "матрица согласования" in plan.variants[1]
    assert "срок согласования" in plan.variants[1]
    assert len(plan.variants) <= 5


def test_exact_status_phrase_is_preserved() -> None:
    plan = build_regulation_query_plan(
        "Что означает статус «Требует доработки»?"
    )
    assert "требует доработки" in plan.strict_query
    assert "требует доработки" in plan.variants[1]


def test_transferred_status_phrase_is_used_in_targeted_query() -> None:
    plan = build_regulation_query_plan(
        "Что означает статус «Передана в отдел закупок»?"
    )
    assert plan.intents == ("status",)
    assert "передана в отдел закупок" in plan.variants[1]


def test_on_approval_status_uses_decision_and_transition_concepts() -> None:
    plan = build_regulation_query_plan(
        "У заявки статус «На согласовании». Мне нужно что-то делать?"
    )

    assert plan.intents == ("status",)
    combined = " ".join(plan.variants)
    assert "согласующие" in combined
    assert "ожидание решения" in combined
    assert "переход после согласования" in combined


def test_multi_intent_question_keeps_urgency_and_form_fields() -> None:
    plan = build_regulation_query_plan(
        "Мероприятие состоится через десять дней. Как оформить заявку "
        "и будет ли она срочной?"
    )
    assert plan.intents == ("urgency", "category_fields")
    assert any("P2" in query for query in plan.variants)
    assert any("обязательные поля" in query for query in plan.variants)


def test_it_integration_question_targets_category_fields() -> None:
    plan = build_regulation_query_plan(
        "Хочу заказать разработку интеграции. Что обязательно написать?"
    )
    assert plan.intents == ("category_fields",)
    assert any("S05" in query for query in plan.variants)


def test_it_connection_wording_targets_category_fields() -> None:
    plan = build_regulation_query_plan(
        "Нужно подключить корпоративную систему к внешнему сервису. "
        "Какие данные нужны для заявки?"
    )
    assert plan.intents == ("category_fields",)
    assert any("S05" in query for query in plan.variants)


def test_multi_query_fusion_is_position_based() -> None:
    first = _chunk("11111111-1111-4111-8111-111111111111", "kb-009")
    second = _chunk("22222222-2222-4222-8222-222222222222", "kb-001")
    fused = fuse_regulation_results(
        [[first, second], [second, first]],
        rrf_k=60,
    )
    assert len(fused) == 2
    assert fused[0].metadata["regulation_query_rrf_score"] == (
        1 / 61 + 1 / 62
    )


def test_example_is_removed_when_normative_chunk_answers_question() -> None:
    plan = build_regulation_query_plan(
        "Какие сведения нужны для перевозки 12 паллет?"
    )
    normative = _chunk(
        "11111111-1111-4111-8111-111111111111",
        "kb-005",
        document_type="field_matrix",
        content="S03 Транспорт: маршрут, груз, вес или объём, даты, погрузка.",
    )
    example = _chunk(
        "22222222-2222-4222-8222-222222222222",
        "kb-011",
        document_type="examples",
        content="12 паллет, 4 тонны, Химки, Москва, гидроборт, 35 000 рублей.",
    )
    selected = select_relevant_regulation_chunks(
        plan,
        [example, normative],
        limit=5,
    )
    assert [item.document_id for item in selected] == ["kb-005"]


def test_live_supplier_price_question_has_no_regulation_context() -> None:
    plan = build_regulation_query_plan(
        "Какой поставщик сейчас продаёт самые дешёвые ноутбуки?"
    )
    nearby = _chunk(
        "11111111-1111-4111-8111-111111111111",
        "kb-010",
        document_type="faq",
        content="Ассистент не выбирает поставщика.",
    )
    assert plan.intent == "outside_kb"
    assert select_relevant_regulation_chunks(plan, [nearby], limit=5) == []


def test_outside_domain_plan_does_not_expand_query() -> None:
    plan = build_regulation_query_plan("Какая погода завтра в Москве?")

    assert plan.intent == "outside_domain"
    assert plan.variants == ()
    assert plan.broad_query == ""


def _chunk(
    chunk_id: str,
    document_id: str,
    *,
    document_type: str = "approval_rules",
    content: str = "Матрица согласования",
) -> HybridRetrievalResult:
    return HybridRetrievalResult(
        chunk_id=UUID(chunk_id),
        document_id=document_id,
        source_filename=f"{document_id}.md",
        document_title="Документ",
        document_type=document_type,
        section_path="Раздел",
        content=content,
        priority=1,
        hybrid_score=0.03,
    )
