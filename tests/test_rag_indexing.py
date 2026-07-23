from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.rag.embeddings import FakeEmbeddingProvider
from app.rag.exceptions import EmbeddingError, IndexingError
from app.rag.indexing_service import KnowledgeIndexingService
from app.rag.repository import (
    InMemoryKnowledgeRepository,
    SupabaseKnowledgeRepository,
)
from tests.rag_helpers import make_chunk, make_document, write_processed_data


class FailingEmbeddingProvider:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("Synthetic embedding failure")

    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingError("Synthetic embedding failure")


def make_service(
    tmp_path: Path,
    repository: InMemoryKnowledgeRepository,
    provider,
    *,
    model: str = "fake-v1",
    chunks=None,
) -> KnowledgeIndexingService:
    documents = [make_document()]
    selected_chunks = chunks or [
        make_chunk("Первый тестовый фрагмент.", chunk_index=0),
        make_chunk("Второй тестовый фрагмент.", chunk_index=1),
    ]
    documents_path, chunks_path = write_processed_data(
        tmp_path,
        documents,
        selected_chunks,
    )
    return KnowledgeIndexingService(
        repository,
        provider,
        embedding_model=model,
        documents_path=documents_path,
        chunks_path=chunks_path,
    )


def test_indexes_new_documents_and_chunks(tmp_path: Path) -> None:
    repository = InMemoryKnowledgeRepository()
    service = make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
    )

    report = service.index()
    stats = repository.get_index_statistics()

    assert report.documents_upserted == 1
    assert report.chunks_upserted == 2
    assert report.embeddings_created == 2
    assert stats.chunks_embedded == 2


def test_repeated_indexing_reuses_embeddings(tmp_path: Path) -> None:
    repository = InMemoryKnowledgeRepository()
    provider = FakeEmbeddingProvider(model="fake-v1")
    service = make_service(tmp_path, repository, provider)

    service.index()
    second = service.index()

    assert second.embeddings_created == 0
    assert second.embeddings_reused == 2
    assert provider.calls == 1


def test_changed_chunk_gets_new_embedding(tmp_path: Path) -> None:
    repository = InMemoryKnowledgeRepository()
    original = make_chunk("Исходный фрагмент.", chunk_index=0)
    service = make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
        chunks=[original],
    )
    service.index()
    changed = make_chunk(
        "Изменённый фрагмент.",
        chunk_index=0,
        fixed_id=original.chunk_id,
    )
    changed_service = make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
        chunks=[changed],
    )

    report = changed_service.index()

    assert report.embeddings_created == 1
    assert report.embeddings_reused == 0


def test_changed_chunk_replaces_old_logical_position_safely(
    tmp_path: Path,
) -> None:
    repository = InMemoryKnowledgeRepository()
    provider = FakeEmbeddingProvider(model="fake-v1")
    original = make_chunk(
        "Старое содержимое.",
        chunk_index=0,
    ).model_copy(update={"metadata": {"revision": "old"}})
    unrelated = make_chunk(
        "Посторонний актуальный чанк.",
        chunk_index=1,
    ).model_copy(update={"metadata": {"keep": True}})
    make_service(
        tmp_path,
        repository,
        provider,
        chunks=[original, unrelated],
    ).index()
    updated = make_chunk(
        "Новое содержимое.",
        chunk_index=0,
    ).model_copy(update={"metadata": {"revision": "new"}})

    report = make_service(
        tmp_path,
        repository,
        provider,
        chunks=[updated, unrelated],
    ).index()
    results = repository.semantic_search(
        provider.embed_query(updated.content),
        top_k=10,
        threshold=-1.0,
    )
    result_by_id = {result.chunk_id: result for result in results}

    assert UUID(str(original.chunk_id)) not in result_by_id
    assert UUID(str(updated.chunk_id)) in result_by_id
    assert UUID(str(unrelated.chunk_id)) in result_by_id
    assert result_by_id[UUID(str(updated.chunk_id))].content == updated.content
    assert result_by_id[UUID(str(updated.chunk_id))].metadata == {
        "revision": "new"
    }
    assert result_by_id[UUID(str(updated.chunk_id))].similarity == pytest.approx(
        1.0
    )
    assert repository.get_index_statistics().chunks_total == 2
    assert report.embeddings_created == 1
    assert report.embeddings_reused == 1
    assert report.stale_chunks_deleted == 0


def test_supabase_chunk_upsert_targets_logical_unique_constraint() -> None:
    client = MagicMock()
    table = client.table.return_value
    original = make_chunk("Старое содержимое.", chunk_index=0)
    updated = make_chunk("Новое содержимое.", chunk_index=0)
    table.select.return_value.execute.return_value.data = [
        {
            "id": original.chunk_id,
            "document_id": original.document_id,
            "chunk_index": original.chunk_index,
            "content_sha256": original.content_sha256,
        }
    ]
    table.upsert.return_value.execute.return_value.data = []
    repository = SupabaseKnowledgeRepository(client)

    repository.upsert_chunks([updated])

    payload = table.upsert.call_args.args[0]
    assert table.upsert.call_args.kwargs["on_conflict"] == (
        "document_id,chunk_index"
    )
    assert payload[0]["id"] == updated.chunk_id
    assert payload[0]["embedding"] is None
    assert payload[0]["embedding_model"] is None
    assert payload[0]["embedded_at"] is None


def test_embedding_model_change_requires_reembedding(tmp_path: Path) -> None:
    repository = InMemoryKnowledgeRepository()
    make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
        model="fake-v1",
    ).index()

    report = make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v2"),
        model="fake-v2",
    ).index()

    assert report.embeddings_created == 2
    assert repository.get_index_statistics().embedding_models == ["fake-v2"]


def test_stale_chunk_is_deleted(tmp_path: Path) -> None:
    repository = InMemoryKnowledgeRepository()
    make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
    ).index()
    remaining = make_chunk("Первый тестовый фрагмент.", chunk_index=0)

    report = make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
        chunks=[remaining],
    ).index()

    assert report.stale_chunks_deleted == 1
    assert repository.get_index_statistics().chunks_total == 1


def test_skip_delete_keeps_stale_chunk(tmp_path: Path) -> None:
    repository = InMemoryKnowledgeRepository()
    make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
    ).index()
    remaining = make_chunk("Первый тестовый фрагмент.", chunk_index=0)

    report = make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
        chunks=[remaining],
    ).index(skip_delete=True)

    assert report.stale_chunks_deleted == 0
    assert repository.get_index_statistics().chunks_total == 2


def test_embedding_error_does_not_delete_stale_chunk(tmp_path: Path) -> None:
    repository = InMemoryKnowledgeRepository()
    original_chunks = [
        make_chunk("Первый тестовый фрагмент.", chunk_index=0),
        make_chunk("Второй тестовый фрагмент.", chunk_index=1),
    ]
    original_service = make_service(
        tmp_path,
        repository,
        FakeEmbeddingProvider(model="fake-v1"),
        chunks=original_chunks,
    )
    original_service.index()
    changed = make_chunk(
        "Изменено и требует embedding.",
        chunk_index=0,
        fixed_id=original_chunks[0].chunk_id,
    )
    failing_service = make_service(
        tmp_path,
        repository,
        FailingEmbeddingProvider(),
        chunks=[changed],
    )

    with pytest.raises(IndexingError, match="before stale chunk cleanup"):
        failing_service.index()

    assert repository.get_index_statistics().chunks_total == 2
