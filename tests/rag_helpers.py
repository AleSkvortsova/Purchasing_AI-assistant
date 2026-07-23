import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.rag.models import KnowledgeChunk, KnowledgeDocument


def make_document(
    document_id: str = "kb-test",
    *,
    document_type: str = "regulation",
    status: str = "active",
) -> KnowledgeDocument:
    content = "# Test document\n\nRules for testing."
    return KnowledgeDocument(
        document_id=document_id,
        filename=f"{document_id}.md",
        title=f"Document {document_id}",
        document_type=document_type,
        version="1.0",
        effective_date="2026-07-21",
        owner="Отдел закупок",
        priority=1,
        status=status,
        language="ru",
        raw_content=content,
        content_without_front_matter=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def make_chunk(
    content: str,
    *,
    document_id: str = "kb-test",
    document_type: str = "regulation",
    chunk_index: int = 0,
    fixed_id: str | None = None,
) -> KnowledgeChunk:
    chunk_id = fixed_id or str(
        uuid5(NAMESPACE_URL, f"{document_id}:{chunk_index}:{content}")
    )
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        source_filename=f"{document_id}.md",
        document_title=f"Document {document_id}",
        document_type=document_type,
        section_path=f"Document {document_id} > Section {chunk_index}",
        heading=f"Section {chunk_index}",
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        chunk_index=chunk_index,
        priority=1,
        version="1.0",
        effective_date="2026-07-21",
        token_count_estimate=max(1, len(content) // 3),
        char_count=len(content),
        metadata={},
    )


def write_processed_data(
    directory: Path,
    documents: list[KnowledgeDocument],
    chunks: list[KnowledgeChunk],
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    documents_path = directory / "knowledge_documents.json"
    chunks_path = directory / "knowledge_chunks.json"
    documents_path.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in documents],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    chunks_path.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in chunks],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return documents_path, chunks_path
