import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.chunker import chunk_documents  # noqa: E402
from app.rag.loader import KnowledgeLoadError, load_documents  # noqa: E402
from app.rag.manifest import build_manifest  # noqa: E402
from app.rag.models import ValidationIssue  # noqa: E402
from app.rag.validator import validate_knowledge_base  # noqa: E402

KNOWLEDGE_BASE_DIR = PROJECT_ROOT / "knowledge_base"
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def build_validation_report(issues: list[ValidationIssue]) -> dict[str, Any]:
    errors = sum(issue.level == "error" for issue in issues)
    warnings = sum(issue.level == "warning" for issue in issues)
    return {
        "summary": {
            "errors": errors,
            "warnings": warnings,
        },
        "issues": [issue.model_dump(mode="json") for issue in issues],
    }


def build_statistics(documents, chunks, issues) -> dict[str, Any]:
    sizes = [chunk.token_count_estimate for chunk in chunks]
    errors = sum(issue.level == "error" for issue in issues)
    warnings = sum(issue.level == "warning" for issue in issues)
    largest = sorted(
        chunks,
        key=lambda chunk: (-chunk.token_count_estimate, chunk.chunk_id),
    )[:5]
    return {
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "average_chunks_per_document": (
            round(len(chunks) / len(documents), 2) if documents else 0
        ),
        "chunk_size_tokens": {
            "min": min(sizes, default=0),
            "median": statistics.median(sizes) if sizes else 0,
            "max": max(sizes, default=0),
        },
        "errors": errors,
        "warnings": warnings,
        "document_type_distribution": dict(
            sorted(Counter(item.document_type for item in documents).items())
        ),
        "five_largest_chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "document_title": chunk.document_title,
                "section_path": chunk.section_path,
                "token_count_estimate": chunk.token_count_estimate,
            }
            for chunk in largest
        ],
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        documents = load_documents(KNOWLEDGE_BASE_DIR)
    except KnowledgeLoadError as exc:
        issue = ValidationIssue(
            level="error",
            code="document_load_error",
            message=str(exc),
        )
        report = build_validation_report([issue])
        write_json(OUTPUT_DIR / "validation_report.json", report)
        print(f"Knowledge-base preparation failed: {exc}")
        return 1

    chunks = chunk_documents(documents)
    issues = validate_knowledge_base(documents, chunks)
    report = build_validation_report(issues)
    stats = build_statistics(documents, chunks, issues)
    write_json(OUTPUT_DIR / "validation_report.json", report)
    write_json(OUTPUT_DIR / "chunk_statistics.json", stats)

    if report["summary"]["errors"]:
        print(
            "Knowledge-base preparation failed: "
            f"{report['summary']['errors']} validation error(s)"
        )
        return 1

    write_json(KNOWLEDGE_BASE_DIR / "manifest.json", build_manifest(documents))
    write_json(
        OUTPUT_DIR / "knowledge_documents.json",
        [item.model_dump(mode="json") for item in documents],
    )
    write_json(
        OUTPUT_DIR / "knowledge_chunks.json",
        [item.model_dump(mode="json") for item in chunks],
    )

    sizes = stats["chunk_size_tokens"]
    print(f"Documents: {stats['document_count']}")
    print(f"Chunks: {stats['chunk_count']}")
    print(
        "Chunks per document: "
        f"{stats['average_chunks_per_document']}"
    )
    print(
        "Chunk tokens min/median/max: "
        f"{sizes['min']}/{sizes['median']}/{sizes['max']}"
    )
    print(f"Errors: {stats['errors']}")
    print(f"Warnings: {stats['warnings']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
