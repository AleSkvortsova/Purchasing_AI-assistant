import ast
import re
from pathlib import Path

MIGRATION_PATH = Path("scripts/sql/010_enable_row_level_security.sql")
TARGET_TABLES = {
    "users",
    "requests",
    "dialog_states",
    "message_logs",
    "knowledge_documents",
    "knowledge_chunks",
    "request_lifecycle_commands",
}


def _production_python_files() -> list[Path]:
    return sorted(Path("app").rglob("*.py")) + sorted(Path("scripts").glob("*.py"))


def _create_client_keys(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    keys: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        function = node.func
        if not isinstance(function, ast.Name) or function.id != "create_client":
            continue
        keys.append(ast.unparse(node.args[1]))
    return keys


def test_production_supabase_clients_use_only_service_role_credentials() -> None:
    client_keys = {
        str(path): key
        for path in _production_python_files()
        for key in _create_client_keys(path)
    }

    assert client_keys
    assert all("service_role_key" in key for key in client_keys.values())
    assert not any(
        re.search(r"anon|publishable", key, re.IGNORECASE)
        for key in client_keys.values()
    )


def test_production_settings_do_not_define_public_supabase_key() -> None:
    settings_source = Path("app/core/config.py").read_text(encoding="utf-8")

    assert "supabase_service_role_key" in settings_source
    assert not re.search(
        r"supabase_(?:anon|publishable)_key\s*:",
        settings_source,
        re.IGNORECASE,
    )


def test_rls_migration_enables_exactly_the_expected_tables() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8").casefold()
    enabled_tables = re.findall(
        r"alter\s+table\s+public\.([a-z_]+)\s+enable\s+row\s+level\s+security\s*;",
        sql,
    )

    assert set(enabled_tables) == TARGET_TABLES
    assert len(enabled_tables) == len(TARGET_TABLES)
    assert sql.startswith("-- block direct data api row access")
    assert "begin;" in sql
    assert sql.rstrip().endswith("commit;")


def test_rls_migration_adds_no_policies_grants_or_unrelated_changes() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8").casefold()

    forbidden = (
        "create policy",
        "alter policy",
        "drop policy",
        "force row level security",
        "grant ",
        "revoke ",
        "create function",
        "create or replace function",
        "create index",
        "drop table",
        "truncate",
        "delete from",
        "insert into",
        "update public.",
        "create extension",
        "alter extension",
    )
    assert not any(fragment in sql for fragment in forbidden)


def test_rls_migration_contains_only_transaction_and_enable_statements() -> None:
    sql = re.sub(
        r"--[^\n]*",
        "",
        MIGRATION_PATH.read_text(encoding="utf-8").casefold(),
    )
    statements = [
        statement.strip() for statement in sql.split(";") if statement.strip()
    ]

    assert statements[0] == "begin"
    assert statements[-1] == "commit"
    assert all(
        re.fullmatch(
            r"alter\s+table\s+public\.[a-z_]+\s+enable\s+row\s+level\s+security",
            statement,
        )
        for statement in statements[1:-1]
    )
