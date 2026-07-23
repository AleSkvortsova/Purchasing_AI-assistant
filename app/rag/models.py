from datetime import datetime
from typing import Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, Field


class KnowledgeDocument(BaseModel):
    document_id: str
    filename: str
    title: str
    document_type: str
    version: str
    effective_date: str
    owner: str
    priority: int
    status: str
    language: str
    raw_content: str
    content_without_front_matter: str
    sha256: str


class KnowledgeChunk(BaseModel):
    chunk_id: str
    document_id: str
    source_filename: str
    document_title: str
    document_type: str
    section_path: str
    heading: str
    content: str
    content_sha256: str
    chunk_index: int
    priority: int
    version: str
    effective_date: str
    token_count_estimate: int
    char_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    filename: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None


class EmbeddingItem(BaseModel):
    chunk_id: UUID
    embedding: list[float]
    embedding_model: str


class RetrievalResult(BaseModel):
    chunk_id: UUID
    document_id: str
    source_filename: str
    document_title: str
    document_type: str
    section_path: str
    heading: str | None = None
    content: str
    priority: int
    similarity: float = Field(ge=-1.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


RetrievalMode: TypeAlias = Literal["semantic", "lexical", "hybrid"]


class LexicalRetrievalResult(BaseModel):
    chunk_id: UUID
    document_id: str
    source_filename: str
    document_title: str
    document_type: str
    section_path: str
    heading: str | None = None
    content: str
    priority: int
    lexical_score: float = Field(ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HybridRetrievalResult(BaseModel):
    chunk_id: UUID
    document_id: str
    source_filename: str
    document_title: str
    document_type: str
    section_path: str
    heading: str | None = None
    content: str
    priority: int
    similarity: float | None = Field(default=None, ge=-1.0, le=1.0)
    lexical_score: float | None = Field(default=None, ge=0.0)
    semantic_rank: int | None = Field(default=None, ge=1)
    lexical_rank: int | None = Field(default=None, ge=1)
    hybrid_score: float = Field(ge=0.0)
    retrieval_method: Literal["hybrid"] = "hybrid"
    metadata: dict[str, Any] = Field(default_factory=dict)


SearchResult: TypeAlias = (
    RetrievalResult | LexicalRetrievalResult | HybridRetrievalResult
)


class IndexStatistics(BaseModel):
    documents_total: int
    documents_active: int
    chunks_total: int
    chunks_embedded: int
    chunks_without_embedding: int
    embedding_models: list[str] = Field(default_factory=list)
    last_embedded_at: datetime | None = None


class IndexingReport(BaseModel):
    documents_upserted: int
    chunks_upserted: int
    embeddings_created: int
    embeddings_reused: int
    stale_chunks_deleted: int
    embedding_model: str
    duration_ms: int


class RagHealthResponse(BaseModel):
    configured: bool
    database_configured: bool
    openai_configured: bool
    embedding_model: str
    embedding_dimensions: int
    index_statistics: IndexStatistics | None = None


class RagSearchRequest(BaseModel):
    query: str
    mode: RetrievalMode | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    similarity_threshold: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
    )
    semantic_candidate_count: int | None = Field(default=None, ge=1, le=100)
    lexical_candidate_count: int | None = Field(default=None, ge=1, le=100)
    rrf_k: int | None = Field(default=None, ge=1, le=1000)
    semantic_weight: float | None = Field(default=None, gt=0, le=10)
    lexical_weight: float | None = Field(default=None, gt=0, le=10)
    document_types: list[str] | None = None


class RagSearchResponse(BaseModel):
    query: str
    mode: RetrievalMode
    count: int
    results: list[SearchResult]
    duration_ms: int


class RagIndexRequest(BaseModel):
    force_reembed: bool = False
    skip_delete: bool = False
