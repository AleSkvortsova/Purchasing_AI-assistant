import re
from pathlib import Path


def test_hybrid_migration_is_non_destructive() -> None:
    sql = Path(
        "scripts/sql/003_hybrid_knowledge_search.sql"
    ).read_text(encoding="utf-8")

    assert not re.search(r"\bdrop\s+table\b", sql, re.IGNORECASE)
    assert not re.search(r"\btruncate\b", sql, re.IGNORECASE)
    assert not re.search(r"\bdelete\s+from\b", sql, re.IGNORECASE)
    assert "using gin (search_vector)" in sql
    assert "security invoker" in sql
    assert "semantic_weight /" in sql
    assert "lexical_weight /" in sql


def test_lexical_improvement_migration_is_safe_and_uses_broad_or() -> None:
    sql = Path(
        "scripts/sql/004_improve_lexical_retrieval.sql"
    ).read_text(encoding="utf-8")

    assert not re.search(r"\bdrop\s+table\b", sql, re.IGNORECASE)
    assert not re.search(r"\btruncate\b", sql, re.IGNORECASE)
    assert not re.search(r"\bdelete\b", sql, re.IGNORECASE)
    assert "add column" not in sql.casefold()
    assert "strict_query" in sql
    assert "text_query" in sql
    assert "broad_query" in sql
    assert "' | '" in sql
    assert "|| ':*'" in sql
    assert "then 3 *" in sql
    assert "then 2 *" in sql
    assert "from public, anon, authenticated" in sql
