from pathlib import Path

SQL = (
    Path("scripts/sql/008_request_lifecycle.sql").read_text(encoding="utf-8").casefold()
)
PREFLIGHT = (
    Path("scripts/sql/008_request_lifecycle_preflight.sql")
    .read_text(encoding="utf-8")
    .casefold()
)


def test_migration_is_transactional_idempotent_and_non_destructive() -> None:
    assert SQL.startswith("-- deterministic")
    assert "begin;" in SQL
    assert SQL.rstrip().endswith("commit;")
    assert "add column if not exists" in SQL
    assert "create sequence if not exists" in SQL
    assert "create table if not exists public.request_lifecycle_commands" in SQL
    assert "create unique index if not exists" in SQL
    assert "create or replace function" in SQL
    assert "drop table" not in SQL
    assert "truncate" not in SQL
    assert "delete from" not in SQL


def test_numbering_and_namespace_are_concurrency_safe() -> None:
    assert "public.request_number_seq" in SQL
    assert "nextval('public.request_number_seq')" in SQL
    assert "select max(" not in SQL
    assert "pr-' || to_char(v_now, 'yyyy')" in SQL
    assert "unique (user_id, command_type, idempotency_key)" in SQL
    assert "pg_advisory_xact_lock" in SQL
    assert SQL.index("replay precedes ownership") < SQL.index("for update")
    assert SQL.index("v_request.version <> v_expected_version") < SQL.index("nextval(")
    assert SQL.index("lifecycle_dialog_mismatch") < SQL.index("nextval(")
    assert SQL.index("ready_for_confirmation") < SQL.index("nextval(")


def test_rpc_security_ownership_version_and_atomic_writes() -> None:
    for name in (
        "confirm_request",
        "return_request_to_editing",
        "cancel_request",
        "mark_request_collecting",
    ):
        assert f"public.{name}" in SQL
    assert "security definer" in SQL
    assert "set search_path = public, pg_catalog" in SQL
    assert "from public, anon, authenticated" in SQL
    assert "to service_role" in SQL
    assert "owner to postgres" in SQL
    assert "lifecycle_ownership_mismatch" in SQL
    assert "concurrent_lifecycle_update" in SQL
    assert "version = version + 1" in SQL
    assert "insert into public.dialog_states" in SQL
    assert SQL.count("insert into public.message_logs") == 4
    assert "insert into public.request_lifecycle_commands" in SQL
    assert "record_request_lifecycle_failure" in SQL
    assert "execute format(" not in SQL
    assert "requests_confirmed_by_fkey" in SQL
    assert "requests_cancelled_by_fkey" in SQL
    assert "references public.users(id)" in SQL


def test_confirm_requires_current_snapshot_and_terminal_states_clear_active() -> None:
    assert "final_request_card" in SQL
    assert "final_completeness" in SQL
    assert "final_approval_route" in SQL
    assert "ready_for_confirmation" in SQL
    assert "request_number = v_number" in SQL
    assert "status = v_status" in SQL
    assert "case when v_status = 'draft' then v_request_id else null end" in SQL
    assert "final_completeness'->>'is_complete" not in SQL
    assert "final_approval_route'->>'status" not in SQL
    assert "from public.dialog_states" in SQL
    assert "v_dialog.active_request_id is distinct from v_request_id" in SQL
    assert "'{lifecycle,confirmed_by}'" in SQL
    assert "to_jsonb(v_user_id)" in SQL


def test_preflight_is_read_only_and_covers_lifecycle_anomalies() -> None:
    lines = [
        line.strip()
        for line in PREFLIGHT.splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    ]
    forbidden = ("update ", "delete ", "insert ", "alter ", "drop ", "truncate ")
    assert not any(line.startswith(forbidden) for line in lines)
    assert "having count(*) > 1" in PREFLIGHT
    assert "status = 'new' and request_number is null" in PREFLIGHT
    assert (
        "status in ('draft', 'cancelled') and request_number is not null" in PREFLIGHT
    )
    assert "unknown_confirmed_actor" in PREFLIGHT
    assert "unknown_cancelled_actor" in PREFLIGHT
    assert "data->'lifecycle'" in PREFLIGHT
    assert "dialog_states" in PREFLIGHT
    assert "information_schema.columns" in PREFLIGHT
    assert "pg_indexes" in PREFLIGHT
    assert "relkind = 's'" in PREFLIGHT
    assert "pg_proc" in PREFLIGHT
