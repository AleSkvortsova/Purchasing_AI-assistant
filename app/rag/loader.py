import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.rag.models import KnowledgeDocument

REQUIRED_METADATA = {
    "document_id",
    "title",
    "document_type",
    "version",
    "effective_date",
    "owner",
    "priority",
    "status",
    "language",
}
README_FILENAME = "00_README.md"


class KnowledgeLoadError(ValueError):
    """Raised when a Markdown source cannot be loaded safely."""


def discover_markdown_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise KnowledgeLoadError(f"Knowledge-base directory not found: {directory}")
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".md"
            and path.name != README_FILENAME
        ),
        key=lambda path: path.name.casefold(),
    )


def load_document(path: Path) -> KnowledgeDocument:
    try:
        raw_bytes = path.read_bytes()
        raw_content = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KnowledgeLoadError(f"{path.name}: cannot read UTF-8 Markdown") from exc

    metadata, content = parse_front_matter(raw_content, path.name)
    missing = sorted(REQUIRED_METADATA - metadata.keys())
    if missing:
        fields = ", ".join(missing)
        raise KnowledgeLoadError(
            f"{path.name}: missing required front matter fields: {fields}"
        )

    normalized_metadata = dict(metadata)
    effective_date = normalized_metadata.get("effective_date")
    if isinstance(effective_date, date):
        normalized_metadata["effective_date"] = effective_date.isoformat()

    try:
        return KnowledgeDocument(
            **normalized_metadata,
            filename=path.name,
            raw_content=raw_content,
            content_without_front_matter=content,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )
    except ValidationError as exc:
        raise KnowledgeLoadError(f"{path.name}: invalid front matter: {exc}") from exc


def load_documents(directory: Path = Path("knowledge_base")) -> list[KnowledgeDocument]:
    return [load_document(path) for path in discover_markdown_files(directory)]


def parse_front_matter(
    raw_content: str,
    filename: str,
) -> tuple[dict[str, Any], str]:
    lines = raw_content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise KnowledgeLoadError(f"{filename}: YAML front matter is missing")

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing_index is None:
        raise KnowledgeLoadError(f"{filename}: YAML front matter is not closed")

    try:
        metadata = yaml.safe_load("".join(lines[1:closing_index]))
    except yaml.YAMLError as exc:
        raise KnowledgeLoadError(f"{filename}: invalid YAML front matter") from exc
    if not isinstance(metadata, dict):
        raise KnowledgeLoadError(f"{filename}: front matter must be a mapping")

    content = "".join(lines[closing_index + 1 :]).lstrip("\r\n")
    return metadata, content
