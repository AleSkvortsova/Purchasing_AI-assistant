from pathlib import Path

from app.core.config import get_settings
from scripts import index_knowledge_base
from tests.rag_helpers import make_chunk, make_document, write_processed_data


def test_index_dry_run_does_not_call_external_services(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    documents_path, chunks_path = write_processed_data(
        tmp_path,
        [make_document()],
        [make_chunk("Тестовый фрагмент.")],
    )
    monkeypatch.setattr(
        index_knowledge_base,
        "DOCUMENTS_PATH",
        documents_path,
    )
    monkeypatch.setattr(index_knowledge_base, "CHUNKS_PATH", chunks_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("External service must not be initialized")

    monkeypatch.setattr(
        index_knowledge_base,
        "OpenAIEmbeddingProvider",
        forbidden,
    )
    monkeypatch.setattr(
        index_knowledge_base,
        "SupabaseKnowledgeRepository",
        forbidden,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    get_settings.cache_clear()

    try:
        result = index_knowledge_base.main(["--dry-run"])
    finally:
        get_settings.cache_clear()

    output = capsys.readouterr().out
    assert result == 0
    assert "Documents found: 1" in output
    assert "Chunks found: 1" in output
    assert "OpenAI configured: False" in output
    assert "Supabase configured: False" in output
