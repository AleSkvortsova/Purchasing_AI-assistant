from uuid import UUID

import pytest

from app.rag.embeddings import FakeEmbeddingProvider
from app.rag.exceptions import RetrievalError
from app.rag.models import EmbeddingItem
from app.rag.repository import InMemoryKnowledgeRepository
from app.rag.retrieval_service import KnowledgeRetrievalService
from tests.rag_helpers import make_chunk, make_document


def prepared_service():
    repository = InMemoryKnowledgeRepository()
    provider = FakeEmbeddingProvider(dimensions=16, model="fake")
    documents = [
        make_document("kb-rules", document_type="regulation"),
        make_document("kb-faq", document_type="faq"),
    ]
    chunks = [
        make_chunk(
            "Кто согласует закупку на 180000 рублей?",
            document_id="kb-rules",
            document_type="regulation",
            chunk_index=0,
        ),
        make_chunk(
            "Как сохранить заявку как черновик?",
            document_id="kb-faq",
            document_type="faq",
            chunk_index=0,
        ),
    ]
    repository.upsert_documents(documents)
    repository.upsert_chunks(chunks)
    repository.update_chunk_embeddings(
        [
            EmbeddingItem(
                chunk_id=UUID(str(chunk.chunk_id)),
                embedding=provider.embed_query(chunk.content),
                embedding_model="fake",
            )
            for chunk in chunks
        ]
    )
    return (
        KnowledgeRetrievalService(
            repository,
            provider,
            default_top_k=5,
            default_threshold=-1.0,
        ),
        chunks,
    )


def test_semantic_search_sorts_by_cosine_similarity() -> None:
    service, chunks = prepared_service()

    results = service.search(chunks[0].content)

    assert results[0].chunk_id == UUID(str(chunks[0].chunk_id))
    assert results[0].similarity == pytest.approx(1.0)
    assert results == sorted(
        results,
        key=lambda item: item.similarity,
        reverse=True,
    )


def test_semantic_search_obeys_top_k() -> None:
    service, chunks = prepared_service()

    assert len(service.search(chunks[0].content, top_k=1)) == 1


def test_semantic_search_filters_document_type() -> None:
    service, chunks = prepared_service()

    results = service.search(
        chunks[0].content,
        document_types=["faq"],
    )

    assert len(results) == 1
    assert results[0].document_type == "faq"


def test_empty_query_is_rejected() -> None:
    service, _ = prepared_service()

    with pytest.raises(RetrievalError, match="must not be empty"):
        service.search("   ")


def test_top_k_above_twenty_is_rejected() -> None:
    service, _ = prepared_service()

    with pytest.raises(RetrievalError, match="between 1 and 20"):
        service.search("query", top_k=21)
