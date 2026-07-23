from pathlib import Path

from scripts import prepare_knowledge_base
from tests.test_rag_loader import markdown_source


def test_prepare_script_is_deterministic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    knowledge_base = tmp_path / "knowledge_base"
    output = tmp_path / "data" / "processed"
    knowledge_base.mkdir()
    (knowledge_base / "00_README.md").write_text(
        "# Служебное описание",
        encoding="utf-8",
    )
    (knowledge_base / "01_Русские_правила.md").write_text(
        markdown_source(body="# Правила\n\n" + "Содержательное правило. " * 30),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        prepare_knowledge_base,
        "KNOWLEDGE_BASE_DIR",
        knowledge_base,
    )
    monkeypatch.setattr(prepare_knowledge_base, "OUTPUT_DIR", output)

    assert prepare_knowledge_base.main() == 0
    first_results = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*.json"))
    }
    assert prepare_knowledge_base.main() == 0
    second_results = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*.json"))
    }

    assert first_results == second_results
    assert (knowledge_base / "manifest.json").is_file()
    assert (output / "knowledge_documents.json").is_file()
    assert (output / "knowledge_chunks.json").is_file()
