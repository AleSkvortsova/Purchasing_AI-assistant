from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.rag.embeddings import FakeEmbeddingProvider
from app.rag.exceptions import EmbeddingError, IndexingError
from app.rag.indexing_service import KnowledgeIndexingService
from app.rag.models import EmbeddingItem
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


class _ChunkTableOperation:
    def __init__(self, table, operation: str, payload=None, on_conflict=None):
        self._table = table
        self._operation = operation
        self._payload = payload
        self._on_conflict = on_conflict
        self._ids: set[str] | None = None

    def in_(self, column: str, values: list[str]):
        assert column == "id"
        self._ids = {str(value) for value in values}
        return self

    def execute(self):
        if self._operation == "select":
            rows = self._table.rows
            if self._ids is not None:
                rows = [row for row in rows if str(row["id"]) in self._ids]
            return SimpleNamespace(data=[dict(row) for row in rows])
        for incoming in self._payload:
            row = dict(incoming)
            if self._on_conflict == "id":
                match = next(
                    (item for item in self._table.rows if item["id"] == row["id"]),
                    None,
                )
            else:
                match = next(
                    (
                        item
                        for item in self._table.rows
                        if item["document_id"] == row["document_id"]
                        and item["chunk_index"] == row["chunk_index"]
                    ),
                    None,
                )
            for item in self._table.rows:
                if item is match:
                    continue
                if item["id"] == row["id"]:
                    raise ValueError("23505 knowledge_chunks_pkey")
                if (
                    item["document_id"] == row["document_id"]
                    and item["chunk_index"] == row["chunk_index"]
                ):
                    raise ValueError("23505 knowledge_chunks_document_index_key")
            if match is None:
                self._table.rows.append(row)
            else:
                match.update(row)
        return SimpleNamespace(data=[])


class _ChunkTable:
    def __init__(self, rows: list[dict]):
        self.rows = [dict(row) for row in rows]

    def select(self, _columns: str):
        return _ChunkTableOperation(self, "select")

    def upsert(self, payload, *, on_conflict: str, default_to_null: bool):
        del default_to_null
        return _ChunkTableOperation(self, "upsert", payload, on_conflict)


class _ChunkClient:
    def __init__(self, rows: list[dict]):
        self.chunk_table = _ChunkTable(rows)

    def table(self, name: str):
        assert name == "knowledge_chunks"
        return self.chunk_table


def _stored_row(chunk, *, embedding: list[float] | None = None) -> dict:
    row = chunk.model_dump(mode="json")
    row["id"] = row.pop("chunk_id")
    row["embedding"] = embedding
    row["embedding_model"] = "fake-v1" if embedding else None
    row["embedded_at"] = "2026-08-01T00:00:00+00:00" if embedding else None
    return row


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


def test_supabase_upsert_parks_stale_row_that_occupies_current_uuid() -> None:
    first = make_chunk("Первый фрагмент", chunk_index=0)
    shifted = make_chunk(
        "Второй фрагмент",
        chunk_index=1,
        fixed_id=first.chunk_id,
    )
    stale = make_chunk(
        "Второй фрагмент",
        chunk_index=0,
        fixed_id=first.chunk_id,
    )
    client = _ChunkClient([_stored_row(stale, embedding=[0.5, 0.5])])
    repository = SupabaseKnowledgeRepository(client)

    repository.upsert_chunks([shifted])

    assert len(client.chunk_table.rows) == 1
    row = client.chunk_table.rows[0]
    assert row["id"] == shifted.chunk_id
    assert (row["document_id"], row["chunk_index"]) == (
        shifted.document_id,
        1,
    )
    assert row["embedding"] == [0.5, 0.5]


def test_supabase_upsert_recovers_shifted_chunks_after_partial_run() -> None:
    inserted = make_chunk("Новый фрагмент", chunk_index=0)
    first = make_chunk("Первый фрагмент", chunk_index=1)
    second = make_chunk("Второй фрагмент", chunk_index=2)
    stale_first = first.model_copy(update={"chunk_index": 0})
    stale_second = second.model_copy(update={"chunk_index": 1})
    client = _ChunkClient(
        [
            _stored_row(stale_first, embedding=[0.2, 0.2]),
            _stored_row(stale_second, embedding=[0.3, 0.3]),
        ]
    )
    repository = SupabaseKnowledgeRepository(client)

    repository.upsert_chunks([inserted, first, second])
    repository.upsert_chunks([inserted, first, second])

    rows = sorted(client.chunk_table.rows, key=lambda item: item["chunk_index"])
    assert [(row["id"], row["chunk_index"]) for row in rows] == [
        (inserted.chunk_id, 0),
        (first.chunk_id, 1),
        (second.chunk_id, 2),
    ]
    assert [row["embedding"] for row in rows] == [
        None,
        [0.2, 0.2],
        [0.3, 0.3],
    ]


def test_supabase_repairs_118_expected_from_117_shifted_rows() -> None:
    expected = [
        make_chunk(f"Фрагмент {index}", chunk_index=index)
        for index in range(118)
    ]
    insertion_index = 6
    actual = [
        chunk.model_copy(
            update={
                "chunk_index": (
                    chunk.chunk_index
                    if chunk.chunk_index < insertion_index
                    else chunk.chunk_index - 1
                )
            }
        )
        for chunk in expected
        if chunk.chunk_index != insertion_index
    ]
    client = _ChunkClient(
        [_stored_row(chunk, embedding=[0.4, 0.6]) for chunk in actual]
    )
    repository = SupabaseKnowledgeRepository(client)

    repository.upsert_chunks(expected)

    missing = expected[insertion_index]
    assert len(client.chunk_table.rows) == 118
    assert sum(row["embedding"] is not None for row in client.chunk_table.rows) == 117
    repository.update_chunk_embeddings(
        [
            EmbeddingItem(
                chunk_id=UUID(str(missing.chunk_id)),
                embedding=[0.7, 0.3],
                embedding_model="fake-v1",
            )
        ]
    )
    assert len(client.chunk_table.rows) == 118
    assert all(row["embedding"] is not None for row in client.chunk_table.rows)


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
