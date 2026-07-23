from typing import Any

from app.rag.models import KnowledgeDocument

MANIFEST_FIELDS = (
    "document_id",
    "filename",
    "title",
    "document_type",
    "version",
    "effective_date",
    "owner",
    "priority",
    "status",
    "sha256",
)


def build_manifest(documents: list[KnowledgeDocument]) -> list[dict[str, Any]]:
    return [
        {field: getattr(document, field) for field in MANIFEST_FIELDS}
        for document in sorted(documents, key=lambda item: item.filename.casefold())
    ]
