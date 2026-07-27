from pathlib import Path

SQL = (
    Path("scripts/sql/008_request_lifecycle.sql").read_text(encoding="utf-8").casefold()
)
PREFLIGHT = (
    Path("scripts/sql/008_request_lifecycle_preflight.sql")
    .read_text(encoding="utf-8")
    .casefold()
)
SYNC_SQL = Path("scripts/sql/009_sync_request_data_projections.sql").read_text(
    encoding="utf-8"
).casefold()


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
    assert "request_type = command->>'request_type'" in SQL
    assert "category_code = command->>'category_code'" in SQL
    assert "title = command->>'title'" in SQL


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


def test_projection_sync_migration_guards_and_repeat_safety() -> None:
    # The offline test environment has no PostgreSQL server. These assertions
    # verify the trigger/function contract statically; the runbook smoke test
    # must execute the transition after the reviewed migration is applied.
    assert SYNC_SQL.startswith("-- keep legacy")
    assert "begin;" in SYNC_SQL
    assert SYNC_SQL.rstrip().endswith("commit;")
    assert "create or replace function" in SYNC_SQL
    assert "drop trigger if exists requests_sync_data_on_registration" in SYNC_SQL
    assert SYNC_SQL.index("drop trigger if exists") < SYNC_SQL.index(
        "create trigger requests_sync_data_on_registration"
    )
    assert "before update on public.requests" in SYNC_SQL
    assert "old.status = 'draft' and new.status = 'new'" in SYNC_SQL
    assert "v_result := source_data || (" in SYNC_SQL
    protected = (
        "intake",
        "lifecycle",
        "schema_version",
        "request_type",
        "category_code",
        "title",
        "required_date",
    )
    merge_start = SYNC_SQL.index("v_result := source_data || (")
    merge_end = SYNC_SQL.index(");", merge_start)
    protected_merge = SYNC_SQL[merge_start:merge_end]
    for key in protected:
        assert f"- '{key}'" in protected_merge
        assert SYNC_SQL.count(f"- '{key}'") == 2
    assert "'{intake,intake_status}'" in SYNC_SQL
    assert "'{intake,next_question}'" in SYNC_SQL
    assert "'completed'" in SYNC_SQL
    assert "'{lifecycle,final_request_card}'" not in SYNC_SQL
    assert "'{lifecycle,final_approval_route}'" not in SYNC_SQL
    assert "update public.requests as r" in SYNC_SQL
    assert "where r.status = 'new'" in SYNC_SQL
    assert "row(r.data, r.request_type, r.category_code, r.title) is distinct from" in (
        SYNC_SQL
    )
    assert "request_type = p.synchronized_data->>'request_type'" in SYNC_SQL
    assert "category_code = p.synchronized_data->>'category_code'" in SYNC_SQL
    assert "title = p.synchronized_data->>'title'" in SYNC_SQL
    assert "security definer" not in SYNC_SQL
    assert "from public, anon, authenticated, service_role" in SYNC_SQL
    assert "drop table" not in SYNC_SQL
    assert "truncate" not in SYNC_SQL
    assert "delete from" not in SYNC_SQL
    assert "version =" not in SYNC_SQL

    trigger_start = SYNC_SQL.index(
        "create or replace function public.sync_request_data_on_registration()"
    )
    trigger_end = SYNC_SQL.index("$$;", trigger_start)
    trigger_body = SYNC_SQL[trigger_start:trigger_end]
    assert "public.synchronized_request_data_projection(" not in trigger_body
    assert "new.data := new.data || (" in trigger_body


def test_projection_sync_covers_types_nulls_and_registered_status_contract() -> None:
    for field in (
        "quantity",
        "amount",
        "unit",
        "desired_delivery_date",
        "budget_status",
        "delivery_location",
        "department",
        "contact_person",
    ):
        # Ordinary draft keys are copied verbatim, retaining JSON type/null.
        assert f"- '{field}'" not in SYNC_SQL
    assert "v_draft->'desired_delivery_date'" in SYNC_SQL
    assert "coalesce(to_jsonb(v_request_type), 'null'::jsonb)" in SYNC_SQL
    assert "coalesce(to_jsonb(v_category_code), 'null'::jsonb)" in SYNC_SQL
    assert "coalesce(to_jsonb(v_title), 'null'::jsonb)" in SYNC_SQL
    assert "when 'goods' then 'product'" in SYNC_SQL
    assert "when 'service' then 'service'" in SYNC_SQL
    assert "when 'work' then 'service'" in SYNC_SQL

    schema = Path("scripts/sql/001_initial_schema.sql").read_text(
        encoding="utf-8"
    ).casefold()
    model = Path("app/schemas/common.py").read_text(encoding="utf-8").casefold()
    assert "status in ('draft', 'new', 'cancelled')" in schema
    assert 'draft = "draft"' in model
    assert 'new = "new"' in model
    assert 'cancelled = "cancelled"' in model
