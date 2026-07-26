from pathlib import Path

SQL = (
    Path("scripts/sql/007_intake_persistence_orchestration.sql")
    .read_text(encoding="utf-8")
    .casefold()
)


def test_migration_adds_only_required_guarantees() -> None:
    assert "add column if not exists version bigint" in SQL
    assert "requests_one_active_draft_per_user_idx" in SQL
    assert "message_logs_intake_idempotency_idx" in SQL
    assert "create or replace function public.save_intake_step" in SQL
    assert "where id = save_intake_step.request_id" in SQL
    assert "version = save_intake_step.expected_version" in SQL


def test_migration_is_non_destructive_and_idempotent() -> None:
    assert "drop table" not in SQL
    assert "truncate" not in SQL
    assert "delete from" not in SQL
    assert "add column if not exists" in SQL
    assert "create unique index if not exists" in SQL
    assert "if not exists (" in SQL
    assert "begin;" in SQL
    assert "commit;" in SQL
    assert SQL.index("duplicate_active_intake_drafts") < SQL.index(
        "requests_one_active_draft_per_user_idx"
    )


def test_rpc_is_service_role_only_and_atomic() -> None:
    assert "revoke all on function public.save_intake_step" in SQL
    assert "from public, anon, authenticated" in SQL
    assert "to service_role" in SQL
    assert "insert into public.dialog_states" in SQL
    assert SQL.count("insert into public.message_logs") == 2
    assert "owner to postgres" in SQL
    assert "set search_path = public, pg_catalog" in SQL
    assert "is distinct from request_id" in SQL
    assert "locked_user_id is distinct from" in SQL
    assert "execute format(" not in SQL
    assert "execute immediate" not in SQL


def test_rpc_replay_precedes_lock_version_and_writes() -> None:
    replay = SQL.index("database-level replay")
    ownership = SQL.index("lock and verify ownership")
    version_update = SQL.index("update public.requests")
    dialog_write = SQL.index("insert into public.dialog_states")
    assert replay < ownership < version_update < dialog_write
    assert "version = version + 1" in SQL
    assert "and version = save_intake_step.expected_version" in SQL


def test_preflight_is_read_only_and_covers_required_checks() -> None:
    preflight = Path(
        "scripts/sql/007_intake_persistence_preflight.sql"
    ).read_text(encoding="utf-8").casefold()
    statement_lines = [
        line.strip()
        for line in preflight.splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    ]
    forbidden = ("update ", "delete ", "insert ", "alter ", "drop ", "truncate ")
    assert not any(line.startswith(forbidden) for line in statement_lines)
    assert "having count(*) > 1" in preflight
    assert "dialog_states" in preflight
    assert "user_request_mismatch" in preflight
    assert "information_schema.columns" in preflight
    assert "pg_indexes" in preflight
    assert "pg_proc" in preflight
