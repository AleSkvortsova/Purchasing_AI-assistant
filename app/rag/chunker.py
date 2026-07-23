import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID, uuid5

from app.rag.models import KnowledgeChunk, KnowledgeDocument

TARGET_TOKENS = 500
SOFT_MAX_TOKENS = 700
OVERLAP_TOKENS = 60
CHUNK_NAMESPACE = UUID("92dc18a0-b0e3-5b32-8e6e-9b31baea151b")
HEADING_PATTERN = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class MarkdownSection:
    level: int
    heading: str
    section_path: str
    body: str

    @property
    def markdown(self) -> str:
        heading_line = f"{'#' * self.level} {self.heading}"
        return f"{heading_line}\n\n{self.body}".strip()


def estimate_token_count(text: str) -> int:
    return math.ceil(len(text) / 3)


def normalize_for_id(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.split())


def split_markdown_sections(markdown: str) -> list[MarkdownSection]:
    sections: list[MarkdownSection] = []
    path_by_level: dict[int, str] = {}
    current_level: int | None = None
    current_heading = ""
    current_path = ""
    current_body: list[str] = []

    def flush() -> None:
        if current_level is None:
            return
        body = "\n".join(current_body).strip()
        if body:
            sections.append(
                MarkdownSection(
                    level=current_level,
                    heading=current_heading,
                    section_path=current_path,
                    body=body,
                )
            )

    for line in markdown.splitlines():
        match = HEADING_PATTERN.match(line)
        if not match:
            if current_level is not None:
                current_body.append(line)
            continue

        flush()
        level = len(match.group(1))
        heading = match.group(2).strip()
        path_by_level = {
            existing_level: value
            for existing_level, value in path_by_level.items()
            if existing_level < level
        }
        path_by_level[level] = heading
        current_level = level
        current_heading = heading
        current_path = " > ".join(
            path_by_level[key] for key in sorted(path_by_level)
        )
        current_body = []

    flush()
    return sections


def chunk_document(
    document: KnowledgeDocument,
    *,
    target_tokens: int = TARGET_TOKENS,
    soft_max_tokens: int = SOFT_MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    sections = split_markdown_sections(document.content_without_front_matter)

    for section in sections:
        parts = _split_long_section(
            section,
            target_tokens=target_tokens,
            soft_max_tokens=soft_max_tokens,
            overlap_tokens=overlap_tokens,
        )
        for part_index, part in enumerate(parts):
            prefix = (
                f"Документ: {document.title}\n"
                f"Раздел: {section.section_path}\n\n"
            )
            content = f"{prefix}{part}".strip()
            normalized_part = normalize_for_id(part)
            identity = (
                f"{document.document_id}\n"
                f"{section.section_path}\n"
                f"{normalized_part}"
            )
            chunks.append(
                KnowledgeChunk(
                    chunk_id=str(uuid5(CHUNK_NAMESPACE, identity)),
                    document_id=document.document_id,
                    source_filename=document.filename,
                    document_title=document.title,
                    document_type=document.document_type,
                    section_path=section.section_path,
                    heading=section.heading,
                    content=content,
                    content_sha256=hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                    chunk_index=len(chunks),
                    priority=document.priority,
                    version=document.version,
                    effective_date=document.effective_date,
                    token_count_estimate=estimate_token_count(content),
                    char_count=len(content),
                    metadata={
                        "heading_level": section.level,
                        "part_index": part_index,
                        "part_count": len(parts),
                        "token_estimation": "ceil(chars/3)",
                    },
                )
            )
    return chunks


def chunk_documents(documents: list[KnowledgeDocument]) -> list[KnowledgeChunk]:
    return [
        chunk
        for document in sorted(documents, key=lambda item: item.filename.casefold())
        for chunk in chunk_document(document)
    ]


def _split_long_section(
    section: MarkdownSection,
    *,
    target_tokens: int,
    soft_max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    if estimate_token_count(section.markdown) <= soft_max_tokens:
        return [section.markdown]

    heading_line = f"{'#' * section.level} {section.heading}"
    max_body_chars = max(1, soft_max_tokens * 3 - len(heading_line) - 2)
    target_body_chars = max(1, target_tokens * 3 - len(heading_line) - 2)
    overlap_chars = overlap_tokens * 3
    blocks = _expand_oversized_blocks(
        _markdown_blocks(section.body),
        max_body_chars,
    )
    grouped = _group_blocks(
        blocks,
        target_chars=target_body_chars,
        max_chars=max_body_chars,
        overlap_chars=overlap_chars,
    )
    return [f"{heading_line}\n\n{body}".strip() for body in grouped if body.strip()]


def _markdown_blocks(text: str) -> list[tuple[str, bool]]:
    raw_blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", text)
        if block.strip()
    ]
    expanded: list[tuple[str, bool]] = []
    index = 0
    while index < len(raw_blocks):
        block = raw_blocks[index]
        if _is_example_label(block):
            protected = [block]
            index += 1
            if index < len(raw_blocks) and raw_blocks[index].lstrip().startswith(">"):
                protected.append(raw_blocks[index])
                index += 1
            while index < len(raw_blocks) and _is_example_label(raw_blocks[index]):
                protected.append(raw_blocks[index])
                index += 1
                if (
                    index < len(raw_blocks)
                    and raw_blocks[index].lstrip().startswith(">")
                ):
                    protected.append(raw_blocks[index])
                    index += 1
            expanded.append(("\n\n".join(protected), True))
            continue

        if _is_list_block(block):
            expanded.extend((item, False) for item in _split_list_items(block))
        else:
            expanded.append((block, _is_atomic_markdown(block)))
        index += 1
    return expanded


def _expand_oversized_blocks(
    blocks: list[tuple[str, bool]],
    max_chars: int,
) -> list[tuple[str, bool]]:
    expanded: list[tuple[str, bool]] = []
    for block, protected in blocks:
        if len(block) <= max_chars or protected:
            expanded.append((block, protected))
            continue
        for part in _split_prose(block, max_chars):
            expanded.append((part, False))
    return expanded


def _group_blocks(
    blocks: list[tuple[str, bool]],
    *,
    target_chars: int,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    if not blocks:
        return []

    groups: list[str] = []
    current: list[str] = []
    current_length = 0

    for block, _ in blocks:
        separator_length = 2 if current else 0
        candidate_length = current_length + separator_length + len(block)
        should_flush = bool(
            current
            and (
                candidate_length > max_chars
                or (
                    candidate_length > target_chars
                    and current_length >= int(target_chars * 0.7)
                )
            )
        )
        if should_flush:
            groups.append("\n\n".join(current))
            current = _overlap_tail(current, overlap_chars)
            current_length = len("\n\n".join(current))

        current.append(block)
        current_length = len("\n\n".join(current))

    if current:
        groups.append("\n\n".join(current))
    return groups


def _overlap_tail(blocks: list[str], overlap_chars: int) -> list[str]:
    tail: list[str] = []
    length = 0
    for block in reversed(blocks):
        tail.insert(0, block)
        length += len(block)
        if length >= overlap_chars:
            break
    return tail


def _split_prose(text: str, max_chars: int) -> list[str]:
    sentences = [part.strip() for part in SENTENCE_BOUNDARY.split(text) if part.strip()]
    if len(sentences) <= 1:
        return _split_words(text, max_chars)

    parts: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        parts.extend(_split_words(current, max_chars))
    return parts


def _split_words(text: str, max_chars: int) -> list[str]:
    words = text.split()
    parts: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and len(candidate) > max_chars:
            parts.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        parts.append(" ".join(current))
    return parts


def _is_table(block: str) -> bool:
    lines = block.splitlines()
    return (
        len(lines) >= 2
        and all(line.strip().startswith("|") for line in lines)
        and any(re.match(r"^\s*\|?\s*:?-{3,}", line) for line in lines[1:3])
    )


def _is_atomic_markdown(block: str) -> bool:
    stripped = block.strip()
    return _is_table(block) or stripped.startswith("```") or stripped.startswith("~~~")


def _is_example_label(block: str) -> bool:
    normalized = block.strip().casefold()
    return normalized.startswith("**плохо") or normalized.startswith("**хорошо")


def _is_list_block(block: str) -> bool:
    return bool(LIST_ITEM_PATTERN.match(block.splitlines()[0]))


def _split_list_items(block: str) -> list[str]:
    items: list[list[str]] = []
    for line in block.splitlines():
        if LIST_ITEM_PATTERN.match(line):
            items.append([line])
        elif items:
            items[-1].append(line)
        else:
            items.append([line])
    return ["\n".join(item).strip() for item in items if any(item)]
