from pathlib import Path

from app.rag.chunker import chunk_document, split_markdown_sections
from app.rag.loader import load_document
from tests.test_rag_loader import markdown_source


def load_source(tmp_path: Path, body: str):
    path = tmp_path / "01_Русские_правила.md"
    path.write_text(markdown_source(body=body), encoding="utf-8")
    return load_document(path)


def test_section_path_preserves_heading_hierarchy(tmp_path: Path) -> None:
    document = load_source(
        tmp_path,
        "# Правила работы\n\nВведение.\n\n"
        "## Товары\n\nОписание товаров.\n\n"
        "### Требования к характеристикам\n\nИзмеримые требования.",
    )

    chunks = chunk_document(document)

    assert any(
        chunk.section_path
        == "Правила работы > Товары > Требования к характеристикам"
        for chunk in chunks
    )


def test_chunk_id_is_stable(tmp_path: Path) -> None:
    document = load_source(
        tmp_path,
        "# Правила\n\n" + "Стабильный русский текст. " * 20,
    )

    first = chunk_document(document)
    second = chunk_document(document)

    assert [chunk.chunk_id for chunk in first] == [
        chunk.chunk_id for chunk in second
    ]


def test_chunk_id_changes_when_text_changes(tmp_path: Path) -> None:
    document = load_source(tmp_path, "# Правила\n\nИсходный текст правила.")
    changed = document.model_copy(
        update={
            "content_without_front_matter": (
                "# Правила\n\nИзменённый текст правила."
            )
        }
    )

    assert chunk_document(document)[0].chunk_id != chunk_document(changed)[0].chunk_id


def test_long_section_is_split_without_empty_chunks(tmp_path: Path) -> None:
    paragraphs = "\n\n".join(
        f"Абзац {index}. " + "Подробное требование для заявки. " * 12
        for index in range(12)
    )
    document = load_source(tmp_path, f"# Большой раздел\n\n{paragraphs}")

    chunks = chunk_document(
        document,
        target_tokens=100,
        soft_max_tokens=140,
        overlap_tokens=15,
    )

    assert len(chunks) > 1
    assert all(chunk.content.strip() for chunk in chunks)
    assert all(chunk.heading == "Большой раздел" for chunk in chunks)


def test_markdown_table_is_not_split(tmp_path: Path) -> None:
    table = (
        "| Поле | Требование |\n"
        "|---|---|\n"
        "| Количество | Больше нуля |\n"
        "| Бюджет | Общая сумма |"
    )
    body = (
        "# Табличные правила\n\n"
        + "Вводный текст. " * 50
        + f"\n\n{table}\n\n"
        + "Заключительный текст. " * 50
    )
    document = load_source(tmp_path, body)

    chunks = chunk_document(
        document,
        target_tokens=80,
        soft_max_tokens=120,
        overlap_tokens=10,
    )

    assert any(table in chunk.content for chunk in chunks)
    assert not any(
        "| Поле | Требование |" in chunk.content
        and "| Бюджет | Общая сумма |" not in chunk.content
        for chunk in chunks
    )


def test_bad_good_example_block_stays_together(tmp_path: Path) -> None:
    example = (
        "**Плохо:**\n\n> Нужны хорошие ноутбуки.\n\n"
        "**Хорошо:**\n\n> Нужны пять ноутбуков с RAM от 16 ГБ."
    )
    document = load_source(
        tmp_path,
        "# Примеры\n\n" + "Пояснение. " * 120 + f"\n\n{example}",
    )

    chunks = chunk_document(
        document,
        target_tokens=100,
        soft_max_tokens=140,
        overlap_tokens=10,
    )

    assert any(example in chunk.content for chunk in chunks)


def test_heading_without_body_does_not_create_empty_section() -> None:
    sections = split_markdown_sections(
        "# Правила\n\n## Товары\n\n### Детали\n\nСодержимое."
    )

    assert [section.heading for section in sections] == ["Детали"]
