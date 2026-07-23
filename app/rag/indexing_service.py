import json
import time
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from app.rag.embeddings import EmbeddingProvider
from app.rag.exceptions import EmbeddingError, IndexingError, KnowledgeRepositoryError
from app.rag.models import (
    EmbeddingItem,
    IndexingReport,
    KnowledgeChunk,
    KnowledgeDocument,
)
from app.rag.repository import KnowledgeRepository


class KnowledgeIndexingService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        embedding_provider: EmbeddingProvider,
        *,
        embedding_model: str,
        documents_path: Path = Path(
            "data/processed/knowledge_documents.json"
        ),
        chunks_path: Path = Path("data/processed/knowledge_chunks.json"),
    ) -> None:
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._embedding_model = embedding_model
        self._documents_path = documents_path
        self._chunks_path = chunks_path

    def index(
        self,
        *,
        force_reembed: bool = False,
        skip_delete: bool = False,
    ) -> IndexingReport:
        started = time.perf_counter()
        documents, chunks = self._load_processed_data()
        current_ids = {UUID(str(chunk.chunk_id)) for chunk in chunks}
        if not current_ids:
            raise IndexingError("Processed knowledge base contains no chunks")

        try:
            self._repository.upsert_documents(documents)
            self._repository.upsert_chunks(chunks)
            requiring_embedding = (
                self._repository.get_chunks_requiring_embedding(
                    self._embedding_model,
                    force=force_reembed,
                )
            )
            if requiring_embedding:
                vectors = self._embedding_provider.embed_texts(
                    [chunk.content for chunk in requiring_embedding]
                )
                if len(vectors) != len(requiring_embedding):
                    raise EmbeddingError(
                        "Embedding provider returned an unexpected vector count"
                    )
                self._repository.update_chunk_embeddings(
                    [
                        EmbeddingItem(
                            chunk_id=UUID(str(chunk.chunk_id)),
                            embedding=embedding,
                            embedding_model=self._embedding_model,
                        )
                        for chunk, embedding in zip(
                            requiring_embedding,
                            vectors,
                            strict=True,
                        )
                    ]
                )
            stale_deleted = (
                0
                if skip_delete
                else self._repository.delete_chunks_not_in(current_ids)
            )
        except (EmbeddingError, KnowledgeRepositoryError) as exc:
            raise IndexingError(
                "Knowledge indexing failed before stale chunk cleanup"
            ) from exc

        duration_ms = round((time.perf_counter() - started) * 1000)
        embedded_count = len(requiring_embedding)
        return IndexingReport(
            documents_upserted=len(documents),
            chunks_upserted=len(chunks),
            embeddings_created=embedded_count,
            embeddings_reused=(
                0 if force_reembed else len(chunks) - embedded_count
            ),
            stale_chunks_deleted=stale_deleted,
            embedding_model=self._embedding_model,
            duration_ms=duration_ms,
        )

    def inspect_processed_data(self) -> tuple[int, int]:
        documents, chunks = self._load_processed_data()
        return len(documents), len(chunks)

    def _load_processed_data(
        self,
    ) -> tuple[list[KnowledgeDocument], list[KnowledgeChunk]]:
        return load_processed_knowledge_data(
            self._documents_path,
            self._chunks_path,
        )

    @staticmethod
    def _read_json(path: Path) -> list[dict]:
        if not path.is_file():
            raise IndexingError(f"Processed knowledge file not found: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IndexingError(
                f"Cannot read processed knowledge file: {path}"
            ) from exc
        if not isinstance(value, list):
            raise IndexingError(
                f"Processed knowledge file must contain a JSON array: {path}"
            )
        return value


def load_processed_knowledge_data(
    documents_path: Path = Path(
        "data/processed/knowledge_documents.json"
    ),
    chunks_path: Path = Path("data/processed/knowledge_chunks.json"),
) -> tuple[list[KnowledgeDocument], list[KnowledgeChunk]]:
    document_values = KnowledgeIndexingService._read_json(documents_path)
    chunk_values = KnowledgeIndexingService._read_json(chunks_path)
    try:
        documents = [
            KnowledgeDocument.model_validate(item)
            for item in document_values
        ]
        chunks = [
            KnowledgeChunk.model_validate(item)
            for item in chunk_values
        ]
    except ValidationError as exc:
        raise IndexingError(
            "Processed knowledge JSON does not match the expected schema"
        ) from exc

    document_ids = {document.document_id for document in documents}
    if len(document_ids) != len(documents):
        raise IndexingError("Processed documents contain duplicate IDs")
    chunk_ids = {UUID(str(chunk.chunk_id)) for chunk in chunks}
    if len(chunk_ids) != len(chunks):
        raise IndexingError("Processed chunks contain duplicate IDs")
    chunk_positions = {
        (chunk.document_id, chunk.chunk_index)
        for chunk in chunks
    }
    if len(chunk_positions) != len(chunks):
        raise IndexingError(
            "Processed chunks contain duplicate document positions"
        )
    unknown_documents = {
        chunk.document_id
        for chunk in chunks
        if chunk.document_id not in document_ids
    }
    if unknown_documents:
        raise IndexingError(
            "Processed chunks reference unknown documents: "
            + ", ".join(sorted(unknown_documents))
        )
    return documents, chunks
