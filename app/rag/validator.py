import re
from collections import Counter, defaultdict
from datetime import date

from app.rag.chunker import (
    SOFT_MAX_TOKENS,
    estimate_token_count,
    normalize_for_id,
    split_markdown_sections,
)
from app.rag.models import KnowledgeChunk, KnowledgeDocument, ValidationIssue

ALLOWED_DOCUMENT_TYPES = {
    "regulation",
    "template",
    "classifier",
    "field_matrix",
    "urgency_rules",
    "status_guide",
    "responsibility_matrix",
    "approval_rules",
    "faq",
    "examples",
    "glossary",
    "user_guide",
    "error_guide",
    "overview",
}
EXPECTED_PRIORITY = {
    "regulation": 1,
    "field_matrix": 2,
    "classifier": 3,
    "status_guide": 4,
    "approval_rules": 5,
    "urgency_rules": 6,
    "faq": 7,
    "examples": 8,
}
HEADING_PATTERN = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.MULTILINE)


def validate_knowledge_base(
    documents: list[KnowledgeDocument],
    chunks: list[KnowledgeChunk] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(_validate_document_ids(documents))
    issues.extend(_validate_document_hashes(documents))
    issues.extend(_validate_documents(documents))
    issues.extend(_validate_repeated_text(documents))
    issues.extend(_validate_known_consistency(documents))
    if chunks is not None:
        issues.extend(_validate_chunks(chunks))
    return sorted(
        issues,
        key=lambda issue: (
            0 if issue.level == "error" else 1,
            issue.filename or "",
            issue.code,
            issue.message,
        ),
    )


def _validate_document_ids(
    documents: list[KnowledgeDocument],
) -> list[ValidationIssue]:
    by_id: dict[str, list[KnowledgeDocument]] = defaultdict(list)
    for document in documents:
        by_id[document.document_id].append(document)
    return [
        ValidationIssue(
            level="error",
            code="duplicate_document_id",
            message=f"document_id {document_id!r} is used by multiple documents",
            document_id=document_id,
        )
        for document_id, matches in by_id.items()
        if len(matches) > 1
    ]


def _validate_document_hashes(
    documents: list[KnowledgeDocument],
) -> list[ValidationIssue]:
    by_hash: dict[str, list[KnowledgeDocument]] = defaultdict(list)
    for document in documents:
        by_hash[document.sha256].append(document)
    return [
        ValidationIssue(
            level="warning",
            code="duplicate_document_content",
            message="Potential duplicate documents: "
            + ", ".join(item.filename for item in matches),
        )
        for matches in by_hash.values()
        if len(matches) > 1
    ]


def _validate_documents(
    documents: list[KnowledgeDocument],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for document in documents:
        content = document.content_without_front_matter.strip()
        context = {
            "filename": document.filename,
            "document_id": document.document_id,
        }
        if not content:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="empty_document",
                    message="Document content is empty",
                    **context,
                )
            )
            continue
        if not re.search(r"^#\s+\S", content, re.MULTILINE):
            issues.append(
                ValidationIssue(
                    level="error",
                    code="missing_h1",
                    message="Document has no first-level heading",
                    **context,
                )
            )
        if len(content) < 200:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="short_document",
                    message=f"Document is unusually short ({len(content)} chars)",
                    **context,
                )
            )
        if not document.owner.strip():
            issues.append(
                ValidationIssue(
                    level="error",
                    code="missing_owner",
                    message="Document owner is empty",
                    **context,
                )
            )
        if document.document_type not in ALLOWED_DOCUMENT_TYPES:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="unknown_document_type",
                    message=f"Unknown document_type: {document.document_type}",
                    **context,
                )
            )
        if document.status != "active":
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="inactive_document",
                    message=f"Document status is {document.status!r}, not 'active'",
                    **context,
                )
            )
        try:
            date.fromisoformat(document.effective_date)
        except ValueError:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="invalid_effective_date",
                    message=f"Invalid ISO date: {document.effective_date!r}",
                    **context,
                )
            )
        expected_priority = EXPECTED_PRIORITY.get(document.document_type, 9)
        if document.priority != expected_priority:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="unexpected_priority",
                    message=(
                        f"Priority {document.priority} differs from expected "
                        f"{expected_priority} for {document.document_type}"
                    ),
                    **context,
                )
            )
        issues.extend(_validate_headings(document))
        for section in split_markdown_sections(content):
            tokens = estimate_token_count(section.markdown)
            if tokens > SOFT_MAX_TOKENS:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="long_section",
                        message=(
                            f"Section {section.section_path!r} is approximately "
                            f"{tokens} tokens and requires secondary splitting"
                        ),
                        **context,
                    )
                )
    return issues


def _validate_headings(document: KnowledgeDocument) -> list[ValidationIssue]:
    headings = [
        normalize_for_id(match).casefold()
        for match in HEADING_PATTERN.findall(
            document.content_without_front_matter
        )
    ]
    counts = Counter(headings)
    return [
        ValidationIssue(
            level="warning",
            code="duplicate_heading",
            message=f"Heading {heading!r} occurs {count} times",
            filename=document.filename,
            document_id=document.document_id,
        )
        for heading, count in counts.items()
        if count > 1
    ]


def _validate_repeated_text(
    documents: list[KnowledgeDocument],
) -> list[ValidationIssue]:
    occurrences: dict[str, list[str]] = defaultdict(list)
    for document in documents:
        paragraphs = re.split(
            r"\n\s*\n",
            document.content_without_front_matter,
        )
        for paragraph in paragraphs:
            normalized = normalize_for_id(paragraph).casefold()
            if len(normalized) >= 160 and not normalized.startswith("#"):
                occurrences[normalized].append(document.filename)

    return [
        ValidationIssue(
            level="warning",
            code="repeated_text",
            message=(
                f"A long text fragment is repeated {len(filenames)} times in: "
                + ", ".join(sorted(set(filenames)))
            ),
        )
        for filenames in occurrences.values()
        if len(filenames) >= 3
    ]


def _validate_known_consistency(
    documents: list[KnowledgeDocument],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    corpus = "\n".join(item.content_without_front_matter for item in documents)
    if (
        "`Новая`" in corpus
        and "`Передана в отдел закупок`" in corpus
    ):
        issues.append(
            ValidationIssue(
                level="warning",
                code="status_terminology_conflict",
                message=(
                    "Sources use both `Новая` and `Передана в отдел закупок` "
                    "for the post-registration status; authoritative sources "
                    "must be reviewed manually"
                ),
            )
        )
    one_day_mentions = re.findall(
        r"\b(?:один рабочий день|одного рабочего дня)\b",
        corpus,
        flags=re.IGNORECASE,
    )
    if len(one_day_mentions) < 2:
        issues.append(
            ValidationIssue(
                level="warning",
                code="one_day_rule_review",
                message=(
                    "The one-working-day rule was not found in both the "
                    "regulation and approval guidance"
                ),
            )
        )
    return issues


def _validate_chunks(chunks: list[KnowledgeChunk]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    by_content: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    for chunk in chunks:
        body = chunk.content.split("\n\n", maxsplit=1)[-1]
        normalized = normalize_for_id(body)
        by_content[normalized].append(chunk)
        if not normalized:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="empty_chunk",
                    message="Chunk content is empty",
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                )
            )
    for matches in by_content.values():
        if len(matches) > 1:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="duplicate_chunk_content",
                    message=(
                        "Potential duplicate chunk content: "
                        + ", ".join(item.chunk_id for item in matches)
                    ),
                )
            )
    return issues
