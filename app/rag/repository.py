import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from supabase import Client, create_client

from app.rag.exceptions import KnowledgeRepositoryError
from app.rag.models import (
    EmbeddingItem,
    HybridRetrievalResult,
    IndexStatistics,
    KnowledgeChunk,
    KnowledgeDocument,
    LexicalRetrievalResult,
    RetrievalResult,
)

_IGNORED_LEXICAL_TERMS = {
    "руб",
    "рубль",
    "рубля",
    "рублей",
    "рубли",
    "р",
    "кто",
    "что",
    "какой",
    "какая",
    "какие",
    "можно",
    "ли",
    "на",
}


class KnowledgeRepository(Protocol):
    def upsert_documents(self, documents: list[KnowledgeDocument]) -> None: ...

    def upsert_chunks(self, chunks: list[KnowledgeChunk]) -> None: ...

    def update_chunk_embeddings(self, items: list[EmbeddingItem]) -> None: ...

    def delete_chunks_not_in(self, chunk_ids: set[UUID]) -> int: ...

    def get_chunks_requiring_embedding(
        self,
        model_name: str,
        *,
        force: bool = False,
    ) -> list[KnowledgeChunk]: ...

    def semantic_search(
        self,
        query_embedding: list[float],
        top_k: int,
        threshold: float,
        document_types: list[str] | None = None,
    ) -> list[RetrievalResult]: ...

    def lexical_search(
        self,
        query_text: str,
        top_k: int,
        document_types: list[str] | None = None,
    ) -> list[LexicalRetrievalResult]: ...

    def hybrid_search(
        self,
        query_text: str,
        query_embedding: list[float],
        top_k: int,
        semantic_candidate_count: int,
        lexical_candidate_count: int,
        threshold: float,
        rrf_k: int,
        semantic_weight: float,
        lexical_weight: float,
        document_types: list[str] | None = None,
    ) -> list[HybridRetrievalResult]: ...

    def get_index_statistics(self) -> IndexStatistics: ...


@dataclass
class _StoredChunk:
    chunk: KnowledgeChunk
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedded_at: datetime | None = None


class InMemoryKnowledgeRepository:
    def __init__(self) -> None:
        self._documents: dict[str, KnowledgeDocument] = {}
        self._chunks: dict[UUID, _StoredChunk] = {}

    def upsert_documents(self, documents: list[KnowledgeDocument]) -> None:
        for document in documents:
            self._documents[document.document_id] = document.model_copy(deep=True)

    def upsert_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        for chunk in chunks:
            chunk_id = UUID(str(chunk.chunk_id))
            current_by_id = self._chunks.get(chunk_id)
            logical_match = next(
                (
                    (stored_id, stored)
                    for stored_id, stored in self._chunks.items()
                    if stored.chunk.document_id == chunk.document_id
                    and stored.chunk.chunk_index == chunk.chunk_index
                ),
                None,
            )
            current_id, current = (
                logical_match
                if logical_match is not None
                else (chunk_id, current_by_id)
            )
            if current is not None and current_id != chunk_id:
                del self._chunks[current_id]
            if (
                current is not None
                and current_id == chunk_id
                and current.chunk.content_sha256 == chunk.content_sha256
            ):
                self._chunks[chunk_id] = _StoredChunk(
                    chunk=chunk.model_copy(deep=True),
                    embedding=list(current.embedding) if current.embedding else None,
                    embedding_model=current.embedding_model,
                    embedded_at=current.embedded_at,
                )
            else:
                self._chunks[chunk_id] = _StoredChunk(
                    chunk=chunk.model_copy(deep=True)
                )

    def update_chunk_embeddings(self, items: list[EmbeddingItem]) -> None:
        now = datetime.now(UTC)
        for item in items:
            stored = self._chunks.get(item.chunk_id)
            if stored is None:
                raise KnowledgeRepositoryError(
                    f"Chunk {item.chunk_id} does not exist"
                )
            stored.embedding = list(item.embedding)
            stored.embedding_model = item.embedding_model
            stored.embedded_at = now

    def delete_chunks_not_in(self, chunk_ids: set[UUID]) -> int:
        stale = set(self._chunks) - chunk_ids
        for chunk_id in stale:
            del self._chunks[chunk_id]
        return len(stale)

    def get_chunks_requiring_embedding(
        self,
        model_name: str,
        *,
        force: bool = False,
    ) -> list[KnowledgeChunk]:
        matches = [
            stored.chunk.model_copy(deep=True)
            for stored in self._chunks.values()
            if force
            or stored.embedding is None
            or stored.embedding_model != model_name
        ]
        return sorted(
            matches,
            key=lambda chunk: (chunk.source_filename.casefold(), chunk.chunk_index),
        )

    def semantic_search(
        self,
        query_embedding: list[float],
        top_k: int,
        threshold: float,
        document_types: list[str] | None = None,
    ) -> list[RetrievalResult]:
        matches: list[tuple[RetrievalResult, int]] = []
        for stored in self._chunks.values():
            chunk = stored.chunk
            document = self._documents.get(chunk.document_id)
            if (
                stored.embedding is None
                or document is None
                or document.status != "active"
                or (
                    document_types is not None
                    and chunk.document_type not in document_types
                )
            ):
                continue
            similarity = _cosine_similarity(query_embedding, stored.embedding)
            if similarity < threshold:
                continue
            matches.append(
                (
                    RetrievalResult(
                        chunk_id=UUID(str(chunk.chunk_id)),
                        document_id=chunk.document_id,
                        source_filename=chunk.source_filename,
                        document_title=chunk.document_title,
                        document_type=chunk.document_type,
                        section_path=chunk.section_path,
                        heading=chunk.heading,
                        content=chunk.content,
                        priority=chunk.priority,
                        similarity=similarity,
                        metadata=chunk.metadata,
                    ),
                    chunk.chunk_index,
                )
            )
        matches.sort(
            key=lambda item: (
                -item[0].similarity,
                item[0].priority,
                item[1],
            )
        )
        return [result for result, _ in matches[:top_k]]

    def lexical_search(
        self,
        query_text: str,
        top_k: int,
        document_types: list[str] | None = None,
    ) -> list[LexicalRetrievalResult]:
        strict_text = normalize_lexical_text(query_text)
        text_terms = normalize_lexical_terms(query_text)
        strict_tokens = _lexical_tokens(strict_text)
        text_tokens = _lexical_tokens(text_terms)
        if not strict_tokens and not text_tokens:
            return []
        matches: list[tuple[LexicalRetrievalResult, int]] = []
        for stored in self._chunks.values():
            chunk = stored.chunk
            document = self._documents.get(chunk.document_id)
            if (
                document is None
                or document.status != "active"
                or (
                    document_types is not None
                    and chunk.document_type not in document_types
                )
            ):
                continue
            title_text = normalize_lexical_text(
                " ".join(
                    (
                        chunk.document_title,
                        chunk.section_path,
                        chunk.heading,
                    )
                )
            )
            content_text = normalize_lexical_text(chunk.content)
            title_tokens = _lexical_tokens(title_text)
            content_tokens = _lexical_tokens(content_text)
            all_tokens = title_tokens | content_tokens
            strict_matches = _matching_terms(strict_tokens, all_tokens)
            text_matches = _matching_terms(text_tokens, all_tokens)
            strict_score = (
                _weighted_token_score(
                    strict_tokens,
                    title_tokens,
                    content_tokens,
                )
                if len(strict_matches) == len(strict_tokens)
                else 0.0
            )
            text_score = (
                _weighted_token_score(
                    text_tokens,
                    title_tokens,
                    content_tokens,
                )
                if len(text_matches) == len(text_tokens)
                else 0.0
            )
            broad_score = _weighted_token_score(
                text_matches,
                title_tokens,
                content_tokens,
            )
            score = (strict_score * 3) + (text_score * 2) + broad_score
            if score <= 0:
                continue
            matches.append(
                (
                    LexicalRetrievalResult(
                        chunk_id=UUID(str(chunk.chunk_id)),
                        document_id=chunk.document_id,
                        source_filename=chunk.source_filename,
                        document_title=chunk.document_title,
                        document_type=chunk.document_type,
                        section_path=chunk.section_path,
                        heading=chunk.heading,
                        content=chunk.content,
                        priority=chunk.priority,
                        lexical_score=score,
                        metadata=chunk.metadata,
                    ),
                    chunk.chunk_index,
                )
            )
        matches.sort(
            key=lambda item: (
                -item[0].lexical_score,
                item[0].priority,
                item[1],
            )
        )
        return [result for result, _ in matches[:top_k]]

    def hybrid_search(
        self,
        query_text: str,
        query_embedding: list[float],
        top_k: int,
        semantic_candidate_count: int,
        lexical_candidate_count: int,
        threshold: float,
        rrf_k: int,
        semantic_weight: float,
        lexical_weight: float,
        document_types: list[str] | None = None,
    ) -> list[HybridRetrievalResult]:
        semantic = self.semantic_search(
            query_embedding,
            semantic_candidate_count,
            threshold,
            document_types,
        )
        lexical = self.lexical_search(
            query_text,
            lexical_candidate_count,
            document_types,
        )
        semantic_by_id = {
            result.chunk_id: (position, result)
            for position, result in enumerate(semantic, start=1)
        }
        lexical_by_id = {
            result.chunk_id: (position, result)
            for position, result in enumerate(lexical, start=1)
        }
        fused: list[HybridRetrievalResult] = []
        for chunk_id in semantic_by_id.keys() | lexical_by_id.keys():
            semantic_item = semantic_by_id.get(chunk_id)
            lexical_item = lexical_by_id.get(chunk_id)
            if semantic_item is not None:
                source = semantic_item[1]
            elif lexical_item is not None:
                source = lexical_item[1]
            else:
                continue
            semantic_rank = semantic_item[0] if semantic_item else None
            lexical_rank = lexical_item[0] if lexical_item else None
            hybrid_score = (
                semantic_weight / (rrf_k + semantic_rank)
                if semantic_rank is not None
                else 0.0
            ) + (
                lexical_weight / (rrf_k + lexical_rank)
                if lexical_rank is not None
                else 0.0
            )
            fused.append(
                HybridRetrievalResult(
                    chunk_id=chunk_id,
                    document_id=source.document_id,
                    source_filename=source.source_filename,
                    document_title=source.document_title,
                    document_type=source.document_type,
                    section_path=source.section_path,
                    heading=source.heading,
                    content=source.content,
                    priority=source.priority,
                    similarity=(
                        semantic_item[1].similarity if semantic_item else None
                    ),
                    lexical_score=(
                        lexical_item[1].lexical_score if lexical_item else None
                    ),
                    semantic_rank=semantic_rank,
                    lexical_rank=lexical_rank,
                    hybrid_score=hybrid_score,
                    metadata=source.metadata,
                )
            )
        fused.sort(
            key=lambda item: (
                -item.hybrid_score,
                item.priority,
                min(
                    item.semantic_rank or 2**31 - 1,
                    item.lexical_rank or 2**31 - 1,
                ),
            )
        )
        return fused[:top_k]

    def get_index_statistics(self) -> IndexStatistics:
        embedded = [
            stored for stored in self._chunks.values() if stored.embedding is not None
        ]
        embedded_dates = [
            stored.embedded_at for stored in embedded if stored.embedded_at is not None
        ]
        return IndexStatistics(
            documents_total=len(self._documents),
            documents_active=sum(
                document.status == "active"
                for document in self._documents.values()
            ),
            chunks_total=len(self._chunks),
            chunks_embedded=len(embedded),
            chunks_without_embedding=len(self._chunks) - len(embedded),
            embedding_models=sorted(
                {
                    stored.embedding_model
                    for stored in embedded
                    if stored.embedding_model
                }
            ),
            last_embedded_at=max(embedded_dates, default=None),
        )


class SupabaseKnowledgeRepository:
    _CHUNK_COLUMNS = (
        "id,document_id,source_filename,document_title,document_type,"
        "section_path,heading,content,content_sha256,chunk_index,priority,"
        "version,effective_date,token_count_estimate,char_count,metadata"
    )

    def __init__(self, client: Client, *, batch_size: int = 100) -> None:
        self._client = client
        self._batch_size = batch_size

    @classmethod
    def from_credentials(
        cls,
        url: str,
        service_role_key: str,
        *,
        batch_size: int = 100,
    ) -> "SupabaseKnowledgeRepository":
        return cls(
            create_client(url, service_role_key),
            batch_size=batch_size,
        )

    def upsert_documents(self, documents: list[KnowledgeDocument]) -> None:
        payloads = [
            {
                "document_id": item.document_id,
                "filename": item.filename,
                "title": item.title,
                "document_type": item.document_type,
                "version": item.version,
                "effective_date": item.effective_date,
                "owner": item.owner,
                "priority": item.priority,
                "status": item.status,
                "language": item.language,
                "sha256": item.sha256,
                "metadata": {},
            }
            for item in documents
        ]
        self._upsert_batches(
            "knowledge_documents",
            payloads,
            on_conflict="document_id",
            operation="upsert knowledge documents",
        )

    def upsert_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        try:
            response = (
                self._client.table("knowledge_chunks")
                .select("id,document_id,chunk_index,content_sha256")
                .execute()
            )
            current_by_id = {
                UUID(str(row["id"])): row
                for row in response.data
            }
            current_by_position = {
                (row["document_id"], row["chunk_index"]): row
                for row in response.data
            }
        except Exception as exc:
            raise KnowledgeRepositoryError(
                "Failed to inspect existing knowledge chunks"
            ) from exc

        unchanged: list[dict[str, Any]] = []
        reset: list[dict[str, Any]] = []
        for chunk in chunks:
            chunk_id = UUID(str(chunk.chunk_id))
            payload = _chunk_payload(chunk)
            current = current_by_position.get(
                (chunk.document_id, chunk.chunk_index)
            ) or current_by_id.get(chunk_id)
            identity_changed = (
                current is not None
                and UUID(str(current["id"])) != chunk_id
            )
            content_changed = (
                current is not None
                and current["content_sha256"] != chunk.content_sha256
            )
            if identity_changed or content_changed:
                payload.update(
                    embedding=None,
                    embedding_model=None,
                    embedded_at=None,
                )
                reset.append(payload)
            else:
                unchanged.append(payload)
        for payloads in (unchanged, reset):
            self._upsert_batches(
                "knowledge_chunks",
                payloads,
                on_conflict="document_id,chunk_index",
                operation="upsert knowledge chunks",
            )

    def update_chunk_embeddings(self, items: list[EmbeddingItem]) -> None:
        by_id = {item.chunk_id: item for item in items}
        now = datetime.now(UTC).isoformat()
        for ids in _batched(list(by_id), self._batch_size):
            try:
                response = (
                    self._client.table("knowledge_chunks")
                    .select(self._CHUNK_COLUMNS)
                    .in_("id", [str(chunk_id) for chunk_id in ids])
                    .execute()
                )
                rows = response.data
                if len(rows) != len(ids):
                    raise KnowledgeRepositoryError(
                        "Some chunks disappeared before embedding update"
                    )
                for row in rows:
                    item = by_id[UUID(str(row["id"]))]
                    row["embedding"] = item.embedding
                    row["embedding_model"] = item.embedding_model
                    row["embedded_at"] = now
                (
                    self._client.table("knowledge_chunks")
                    .upsert(rows, on_conflict="id", default_to_null=False)
                    .execute()
                )
            except KnowledgeRepositoryError:
                raise
            except Exception as exc:
                raise KnowledgeRepositoryError(
                    "Failed to update chunk embeddings"
                ) from exc

    def delete_chunks_not_in(self, chunk_ids: set[UUID]) -> int:
        if not chunk_ids:
            raise KnowledgeRepositoryError(
                "Refusing to delete chunks with an empty current ID set"
            )
        try:
            response = (
                self._client.table("knowledge_chunks")
                .select("id")
                .execute()
            )
            stale_ids = [
                UUID(str(row["id"]))
                for row in response.data
                if UUID(str(row["id"])) not in chunk_ids
            ]
            for batch in _batched(stale_ids, self._batch_size):
                (
                    self._client.table("knowledge_chunks")
                    .delete()
                    .in_("id", [str(chunk_id) for chunk_id in batch])
                    .execute()
                )
            return len(stale_ids)
        except Exception as exc:
            raise KnowledgeRepositoryError(
                "Failed to delete stale knowledge chunks"
            ) from exc

    def get_chunks_requiring_embedding(
        self,
        model_name: str,
        *,
        force: bool = False,
    ) -> list[KnowledgeChunk]:
        try:
            query = self._client.table("knowledge_chunks").select(
                self._CHUNK_COLUMNS
            )
            if not force:
                query = query.or_(
                    "embedding.is.null,"
                    "embedding_model.is.null,"
                    f"embedding_model.neq.{model_name}"
                )
            response = query.order("source_filename").order("chunk_index").execute()
            return [_chunk_from_row(row) for row in response.data]
        except Exception as exc:
            raise KnowledgeRepositoryError(
                "Failed to find chunks requiring embeddings"
            ) from exc

    def semantic_search(
        self,
        query_embedding: list[float],
        top_k: int,
        threshold: float,
        document_types: list[str] | None = None,
    ) -> list[RetrievalResult]:
        try:
            response = self._client.rpc(
                "match_knowledge_chunks",
                {
                    "query_embedding": query_embedding,
                    "match_count": top_k,
                    "similarity_threshold": threshold,
                    "filter_document_types": document_types,
                    "filter_status": "active",
                },
            ).execute()
            return [RetrievalResult.model_validate(row) for row in response.data]
        except Exception as exc:
            raise KnowledgeRepositoryError(
                "Semantic search failed"
            ) from exc

    def lexical_search(
        self,
        query_text: str,
        top_k: int,
        document_types: list[str] | None = None,
    ) -> list[LexicalRetrievalResult]:
        try:
            response = self._client.rpc(
                "match_knowledge_chunks_lexical",
                {
                    "query_text": query_text,
                    "match_count": top_k,
                    "filter_document_types": document_types,
                    "filter_status": "active",
                },
            ).execute()
            return [
                LexicalRetrievalResult.model_validate(
                    {**row, "lexical_score": row["lexical_rank"]}
                )
                for row in response.data
            ]
        except Exception as exc:
            raise KnowledgeRepositoryError("Lexical search failed") from exc

    def hybrid_search(
        self,
        query_text: str,
        query_embedding: list[float],
        top_k: int,
        semantic_candidate_count: int,
        lexical_candidate_count: int,
        threshold: float,
        rrf_k: int,
        semantic_weight: float,
        lexical_weight: float,
        document_types: list[str] | None = None,
    ) -> list[HybridRetrievalResult]:
        try:
            response = self._client.rpc(
                "match_knowledge_chunks_hybrid",
                {
                    "query_text": query_text,
                    "query_embedding": query_embedding,
                    "final_match_count": top_k,
                    "semantic_candidate_count": semantic_candidate_count,
                    "lexical_candidate_count": lexical_candidate_count,
                    "rrf_k": rrf_k,
                    "semantic_weight": semantic_weight,
                    "lexical_weight": lexical_weight,
                    "filter_document_types": document_types,
                    "filter_status": "active",
                },
            ).execute()
            return [
                HybridRetrievalResult.model_validate(row)
                for row in response.data
            ]
        except Exception as exc:
            raise KnowledgeRepositoryError("Hybrid search failed") from exc

    def get_index_statistics(self) -> IndexStatistics:
        try:
            document_rows = (
                self._client.table("knowledge_documents")
                .select("document_id,status")
                .execute()
                .data
            )
            chunk_rows = (
                self._client.table("knowledge_chunks")
                .select("id,embedding_model,embedded_at")
                .execute()
                .data
            )
        except Exception as exc:
            raise KnowledgeRepositoryError(
                "Failed to read knowledge index statistics"
            ) from exc

        embedded_rows = [row for row in chunk_rows if row["embedded_at"] is not None]
        embedded_dates = [
            datetime.fromisoformat(row["embedded_at"].replace("Z", "+00:00"))
            for row in embedded_rows
        ]
        return IndexStatistics(
            documents_total=len(document_rows),
            documents_active=sum(row["status"] == "active" for row in document_rows),
            chunks_total=len(chunk_rows),
            chunks_embedded=len(embedded_rows),
            chunks_without_embedding=len(chunk_rows) - len(embedded_rows),
            embedding_models=sorted(
                {
                    row["embedding_model"]
                    for row in embedded_rows
                    if row["embedding_model"]
                }
            ),
            last_embedded_at=max(embedded_dates, default=None),
        )

    def _upsert_batches(
        self,
        table: str,
        payloads: list[dict[str, Any]],
        *,
        on_conflict: str,
        operation: str,
    ) -> None:
        for batch in _batched(payloads, self._batch_size):
            try:
                (
                    self._client.table(table)
                    .upsert(
                        batch,
                        on_conflict=on_conflict,
                        default_to_null=False,
                    )
                    .execute()
                )
            except Exception as exc:
                raise KnowledgeRepositoryError(
                    f"Failed to {operation}"
                ) from exc


def _chunk_payload(chunk: KnowledgeChunk) -> dict[str, Any]:
    payload = chunk.model_dump(mode="json")
    payload["id"] = payload.pop("chunk_id")
    return payload


def _chunk_from_row(row: dict[str, Any]) -> KnowledgeChunk:
    values = dict(row)
    values["chunk_id"] = values.pop("id")
    return KnowledgeChunk.model_validate(values)


def _batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise KnowledgeRepositoryError("Embedding dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    similarity = sum(a * b for a, b in zip(left, right, strict=True))
    similarity /= left_norm * right_norm
    return max(-1.0, min(1.0, similarity))


def normalize_lexical_text(value: str) -> str:
    normalized = value.casefold().replace("ё", "е").replace("\u00a0", " ")
    normalized = normalized.replace("–", "-").replace("—", "-")
    normalized = re.sub(r"(?<=\d)\s+(?=\d)", "", normalized)
    return " ".join(normalized.split())


def normalize_lexical_terms(value: str) -> str:
    normalized = normalize_lexical_text(value)
    terms = _lexical_tokens_in_order(normalized)
    return " ".join(
        term
        for term in terms
        if not term.isdigit() and term not in _IGNORED_LEXICAL_TERMS
    )


def _lexical_tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-zа-я0-9]+", value))


def _lexical_tokens_in_order(value: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", value)


def _matching_terms(query: set[str], candidates: set[str]) -> set[str]:
    return {
        term
        for term in query
        if any(_terms_match(term, candidate) for candidate in candidates)
    }


def _weighted_token_score(
    query: set[str],
    title_tokens: set[str],
    content_tokens: set[str],
) -> float:
    return float(
        sum(
            3
            if any(_terms_match(term, candidate) for candidate in title_tokens)
            else 1
            if any(_terms_match(term, candidate) for candidate in content_tokens)
            else 0
            for term in query
        )
    )


def _terms_match(left: str, right: str) -> bool:
    return left == right or _russian_stem(left) == _russian_stem(right)


def _russian_stem(value: str) -> str:
    suffixes = (
        "ования",
        "ование",
        "ениями",
        "ение",
        "ениями",
        "ировать",
        "уется",
        "уют",
        "ует",
        "ами",
        "ями",
        "ого",
        "ему",
        "ом",
        "ах",
        "ях",
        "ку",
        "ки",
        "ка",
        "ок",
        "а",
        "я",
        "ы",
        "и",
        "у",
        "ю",
    )
    for suffix in suffixes:
        if value.endswith(suffix) and len(value) - len(suffix) >= 4:
            return value[: -len(suffix)]
    return value
