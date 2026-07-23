from typing import Any, Literal

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
