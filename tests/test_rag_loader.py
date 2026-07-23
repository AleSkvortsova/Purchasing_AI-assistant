from pathlib import Path

import pytest

from app.rag.loader import KnowledgeLoadError, load_document, load_documents
from app.rag.manifest import build_manifest
from app.rag.validator import validate_knowledge_base


def markdown_source(
    *,
    document_id: str = "kb-001",
    title: str = "Правила закупок",
    body: str = (
        "# Правила закупок\n\n"
        "Содержательный текст документа на русском языке для проверки загрузки. "
        "Он описывает порядок подготовки и передачи внутренних заявок."
    ),
) -> str:
    return f"""---
document_id: {document_id}
title: {title}
document_type: regulation
version: "1.0"
effective_date: "2026-07-21"
owner: Отдел закупок
priority: 1
status: active
language: ru
---

{body}
"""


def test_load_valid_markdown_with_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "01_Правила_закупок.md"
    path.write_text(markdown_source(), encoding="utf-8")

    document = load_document(path)

    assert document.document_id == "kb-001"
    assert document.filename == "01_Правила_закупок.md"
    assert document.title == "Правила закупок"
    assert document.content_without_front_matter.startswith("# Правила закупок")
    assert len(document.sha256) == 64


def test_missing_document_id_has_clear_filename(tmp_path: Path) -> None:
    path = tmp_path / "ошибка.md"
    source = markdown_source().replace("document_id: kb-001\n", "")
    path.write_text(source, encoding="utf-8")

    with pytest.raises(
        KnowledgeLoadError,
        match=r"ошибка\.md: missing required front matter fields: document_id",
    ):
        load_document(path)


def test_duplicate_document_id_is_error(tmp_path: Path) -> None:
    first = tmp_path / "01_Первый.md"
    second = tmp_path / "02_Второй.md"
    first.write_text(markdown_source(), encoding="utf-8")
    second.write_text(
        markdown_source(
            title="Другие правила",
            body="# Другие правила\n\n" + "Текст " * 60,
        ),
        encoding="utf-8",
    )

    issues = validate_knowledge_base(load_documents(tmp_path))

    assert any(
        issue.level == "error" and issue.code == "duplicate_document_id"
        for issue in issues
    )


def test_readme_is_excluded(tmp_path: Path) -> None:
    (tmp_path / "00_README.md").write_text("# Служебное описание", encoding="utf-8")
    (tmp_path / "01_Источник.md").write_text(
        markdown_source(),
        encoding="utf-8",
    )

    documents = load_documents(tmp_path)

    assert [document.filename for document in documents] == ["01_Источник.md"]


def test_manifest_contains_source_metadata(tmp_path: Path) -> None:
    path = tmp_path / "01_Источник.md"
    path.write_text(markdown_source(), encoding="utf-8")

    manifest = build_manifest(load_documents(tmp_path))

    assert manifest == [
        {
            "document_id": "kb-001",
            "filename": "01_Источник.md",
            "title": "Правила закупок",
            "document_type": "regulation",
            "version": "1.0",
            "effective_date": "2026-07-21",
            "owner": "Отдел закупок",
            "priority": 1,
            "status": "active",
            "sha256": manifest[0]["sha256"],
        }
    ]
