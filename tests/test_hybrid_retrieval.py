from uuid import UUID

import pytest

from app.rag.embeddings import FakeEmbeddingProvider
from app.rag.exceptions import RetrievalError
from app.rag.models import EmbeddingItem, HybridRetrievalResult
from app.rag.repository import (
    InMemoryKnowledgeRepository,
    normalize_lexical_terms,
    normalize_lexical_text,
)
from app.rag.retrieval_service import KnowledgeRetrievalService
from tests.rag_helpers import make_chunk, make_document


class CountingProvider(FakeEmbeddingProvider):
    pass


def _repository_with_ranked_chunks() -> InMemoryKnowledgeRepository:
    repository = InMemoryKnowledgeRepository()
    repository.upsert_documents([make_document("kb-rank")])
    chunks = [
        make_chunk(
            "Общий смысловой источник",
            document_id="kb-rank",
            chunk_index=0,
        ),
        make_chunk(
            "Матрица согласования 180 000 рублей",
            document_id="kb-rank",
            chunk_index=1,
        ),
        make_chunk(
            "Только лексическое совпадение матрица согласования",
            document_id="kb-rank",
            chunk_index=2,
        ),
    ]
    repository.upsert_chunks(chunks)
    repository.update_chunk_embeddings(
        [
            EmbeddingItem(
                chunk_id=UUID(str(chunks[0].chunk_id)),
                embedding=[1.0, 0.0],
                embedding_model="test",
            ),
            EmbeddingItem(
                chunk_id=UUID(str(chunks[1].chunk_id)),
                embedding=[0.8, 0.6],
                embedding_model="test",
            ),
        ]
    )
    return repository


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("180 000", "180000"),
        ("180000", "180000"),
        ("180\u00a0000", "180000"),
    ],
)
def test_number_normalization(value: str, expected: str) -> None:
    assert normalize_lexical_text(value) == expected


def test_yo_normalization() -> None:
    assert normalize_lexical_text("Ёлка и ещё") == "елка и еще"


@pytest.mark.parametrize(
    "amount",
    ["180000", "180 000", "180\u00a0000"],
)
def test_text_terms_drop_amount_currency_and_service_words(
    amount: str,
) -> None:
    query = f"Кто согласует закупку на {amount} рублей?"

    assert normalize_lexical_terms(query) == "согласует закупку"


def test_lexical_search_finds_matrix_without_literal_amount() -> None:
    repository = InMemoryKnowledgeRepository()
    repository.upsert_documents(
        [
            make_document("kb-009").model_copy(update={"priority": 5}),
            make_document("kb-014").model_copy(update={"priority": 9}),
        ]
    )
    matrix = make_chunk(
        "Согласование закупки с бюджетом от 100 001–500 000 рублей.",
        document_id="kb-009",
    ).model_copy(
        update={
            "document_title": "Правила согласования заявок",
            "section_path": (
                "Правила согласования заявок > Матрица согласования"
            ),
            "heading": "Матрица согласования",
            "priority": 5,
        }
    )
    instruction = make_chunk(
        "Темы: обязательные поля, статусы заявки, правила согласования, "
        "бренды и эквиваленты, работа с черновиком.",
        document_id="kb-014",
    ).model_copy(
        update={
            "document_title": "Инструкция по работе с ассистентом",
            "section_path": (
                "Инструкция по работе с ассистентом > "
                "8. Как задать справочный вопрос"
            ),
            "heading": "8. Как задать справочный вопрос",
            "priority": 9,
        }
    )
    repository.upsert_chunks([matrix, instruction])

    results = repository.lexical_search(
        "Кто согласует закупку на 180000 рублей?",
        10,
    )

    assert results[0].document_id == "kb-009"
    instruction_result = next(
        result for result in results if result.document_id == "kb-014"
    )
    assert results[0].lexical_score > instruction_result.lexical_score


def test_exact_match_ranks_above_weak_broad_match() -> None:
    repository = InMemoryKnowledgeRepository()
    repository.upsert_documents([make_document("kb-exact")])
    exact = make_chunk(
        "Правила согласования",
        document_id="kb-exact",
        chunk_index=0,
    )
    broad = make_chunk(
        "Общее согласование",
        document_id="kb-exact",
        chunk_index=1,
    )
    repository.upsert_chunks([exact, broad])

    results = repository.lexical_search("Правила согласования", 5)

    assert results[0].chunk_id == UUID(str(exact.chunk_id))
    assert results[0].lexical_score > results[1].lexical_score


def test_instruction_no_longer_contains_literal_control_question() -> None:
    source = (
        "knowledge_base/13_Инструкция_по_работе_с_ассистентом.md"
    )

    assert "Кто согласует закупку" not in open(
        source,
        encoding="utf-8",
    ).read()


def test_lexical_search_finds_exact_status_without_embedding() -> None:
    repository = InMemoryKnowledgeRepository()
    repository.upsert_documents([make_document("kb-status")])
    repository.upsert_chunks(
        [
            make_chunk(
                "Статус «Требует доработки» означает возврат заказчику.",
                document_id="kb-status",
            )
        ]
    )

    results = repository.lexical_search("требует доработки", 5)

    assert results[0].document_id == "kb-status"


def test_lexical_search_finds_approval_matrix() -> None:
    repository = _repository_with_ranked_chunks()

    results = repository.lexical_search("Матрица согласования", 5)

    assert "Матрица согласования" in results[0].content


def test_lexical_mode_does_not_call_embedding_provider() -> None:
    repository = _repository_with_ranked_chunks()
    provider = CountingProvider(dimensions=2)
    service = KnowledgeRetrievalService(repository, provider)

    service.search("Матрица согласования", mode="lexical")

    assert provider.calls == 0


def test_rrf_combines_without_duplicates_and_tracks_contributions() -> None:
    repository = _repository_with_ranked_chunks()

    results = repository.hybrid_search(
        "Матрица согласования",
        [1.0, 0.0],
        10,
        10,
        10,
        -1.0,
        60,
        1.0,
        1.0,
    )

    assert len({result.chunk_id for result in results}) == len(results)
    semantic_only = next(
        item
        for item in results
        if item.semantic_rank is not None and item.lexical_rank is None
    )
    lexical_only = next(
        item
        for item in results
        if item.semantic_rank is None and item.lexical_rank is not None
    )
    both = next(
        item
        for item in results
        if item.semantic_rank is not None and item.lexical_rank is not None
    )
    assert semantic_only.hybrid_score > 0
    assert lexical_only.hybrid_score > 0
    assert both.hybrid_score > semantic_only.hybrid_score
    assert both.hybrid_score > lexical_only.hybrid_score


def test_rrf_weights_change_ranking() -> None:
    repository = _repository_with_ranked_chunks()

    semantic_heavy = repository.hybrid_search(
        "Матрица согласования",
        [1.0, 0.0],
        3,
        3,
        3,
        -1.0,
        60,
        10.0,
        0.1,
    )
    lexical_heavy = repository.hybrid_search(
        "Матрица согласования",
        [1.0, 0.0],
        3,
        3,
        3,
        -1.0,
        60,
        0.1,
        10.0,
    )

    assert semantic_heavy[0].semantic_rank == 1
    assert lexical_heavy[0].lexical_rank == 1


def test_candidate_count_is_validated() -> None:
    service = KnowledgeRetrievalService(
        _repository_with_ranked_chunks(),
        FakeEmbeddingProvider(dimensions=2),
    )

    with pytest.raises(RetrievalError, match="between top_k and 100"):
        service.search(
            "query",
            mode="hybrid",
            top_k=5,
            semantic_candidate_count=4,
        )


def test_hybrid_embeds_query_once_and_obeys_top_k() -> None:
    provider = CountingProvider(dimensions=2)
    service = KnowledgeRetrievalService(
        _repository_with_ranked_chunks(),
        provider,
        default_threshold=-1.0,
    )

    results = service.search(
        "Матрица согласования",
        mode="hybrid",
        top_k=1,
    )

    assert len(results) == 1
    assert isinstance(results[0], HybridRetrievalResult)
    assert provider.calls == 1


def test_semantic_mode_preserves_result_contract() -> None:
    repository = _repository_with_ranked_chunks()

    results = KnowledgeRetrievalService(
        repository,
        FakeEmbeddingProvider(dimensions=2),
        default_threshold=-1.0,
    ).search("query", mode="semantic")

    assert all(hasattr(result, "similarity") for result in results)
    assert all(not hasattr(result, "hybrid_score") for result in results)
