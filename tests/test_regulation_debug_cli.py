from pathlib import Path

import pytest

from scripts.debug_regulation_answer import main as answer_debug_main
from scripts.debug_regulation_retrieval import main


def test_debug_cli_help_does_not_require_configuration() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_answer_debug_cli_help_does_not_require_configuration() -> None:
    with pytest.raises(SystemExit) as exc_info:
        answer_debug_main(["--help"])
    assert exc_info.value.code == 0


def test_debug_cli_is_read_only_and_reports_all_retrieval_stages() -> None:
    source = Path("scripts/debug_regulation_retrieval.py").read_text(
        encoding="utf-8"
    )
    for mutation in (
        ".table(",
        "upsert_documents(",
        "upsert_chunks(",
        "update_chunk_embeddings(",
        "delete_chunks_not_in(",
    ):
        assert mutation not in source
    assert "semantic_search(" in source
    assert "lexical_search(" in source
    assert "hybrid_search(" in source
    assert '"strict_query"' in source
    assert '"chunks_passed_to_answer_provider"' in source
    assert '"relevance_decisions"' in source
    assert '"source_kind"' in source
    assert '"matched_intents"' in source
    assert '"below_threshold"' in source
    assert '"no_chunks"' in source


def test_answer_debug_cli_is_read_only_and_reports_validation_details() -> None:
    source = Path("scripts/debug_regulation_answer.py").read_text(
        encoding="utf-8"
    )
    for mutation in (
        ".table(",
        "upsert_documents(",
        "upsert_chunks(",
        "update_chunk_embeddings(",
        "delete_chunks_not_in(",
    ):
        assert mutation not in source
    assert '"structured_output_summary"' in source
    assert 'if args.show_structured_output' in source
    assert '"claim_concrete_values"' in source
    for forbidden in (
        "raw_structured_output",
        '"content": chunk.content',
        '"validation_exception"',
    ):
        assert forbidden not in source
