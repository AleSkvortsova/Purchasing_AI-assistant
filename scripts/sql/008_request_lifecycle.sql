-- Deterministic request lifecycle after intake. Prepared only; do not auto-apply.
begin;

alter table public.requests
    add column if not exists registered_at timestamptz,
    add column if not exists confirmed_by uuid,
    add column if not exists cancelled_at timestamptz,
    add column if not exists cancelled_by uuid,
    add column if not exists cancellation_reason text;

alter table public.message_logs
    add column if not exists lifecycle_command_type text,
    add column if not exists lifecycle_idempotency_key text;

create sequence if not exists public.request_number_seq as bigint start with 1;

-- Existing non-null numbers must be unique before the stable partial index is built.
do $$
begin
    if exists (
        select 1
        from public.requests
        where request_number is not null
        group by request_number
        having count(*) > 1
    ) then
        raise exception 'duplicate_request_numbers: run 008 preflight'
            using errcode = '23505';
    end if;
    if exists (
        select 1
        from public.requests as r
        left join public.users as u on u.id = r.confirmed_by
        where r.confirmed_by is not null and u.id is null
    ) or exists (
        select 1
        from public.requests as r
        left join public.users as u on u.id = r.cancelled_by
        where r.cancelled_by is not null and u.id is null
    ) then
        raise exception 'unknown_lifecycle_actor: run 008 preflight'
            using errcode = '23503';
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'requests_confirmed_by_fkey'
          and conrelid = 'public.requests'::regclass
    ) then
        alter table public.requests
            add constraint requests_confirmed_by_fkey
            foreign key (confirmed_by) references public.users(id);
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'requests_cancelled_by_fkey'
          and conrelid = 'public.requests'::regclass
    ) then
        alter table public.requests
            add constraint requests_cancelled_by_fkey
            foreign key (cancelled_by) references public.users(id);
    end if;
end;
$$;

create unique index if not exists requests_request_number_lifecycle_uidx
    on public.requests (request_number)
    where request_number is not null;

create table if not exists public.request_lifecycle_commands (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id),
    request_id uuid not null references public.requests(id),
    command_type text not null,
    idempotency_key text not null,
    fingerprint text not null,
    result jsonb not null,
    created_at timestamptz not null default now(),
    constraint request_lifecycle_command_type_check
        check (command_type in ('confirm', 'return_to_editing', 'cancel')),
    constraint request_lifecycle_command_namespace_unique
        unique (user_id, command_type, idempotency_key)
);

create index if not exists request_lifecycle_commands_request_idx
    on public.request_lifecycle_commands (request_id, created_at desc);

-- Internal implementation. Advisory locking serializes one idempotency namespace,
-- including concurrent calls that refer to different request IDs.
create or replace function public.apply_request_lifecycle_command(command jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
declare
    v_user_id uuid := (command->>'user_id')::uuid;
    v_request_id uuid := (command->>'request_id')::uuid;
    v_command_type text := command->>'command_type';
    v_key text := command->>'idempotency_key';
    v_fingerprint text := command->>'fingerprint';
    v_expected_version bigint := (command->>'expected_version')::bigint;
    v_request public.requests%rowtype;
    v_dialog public.dialog_states%rowtype;
    v_existing public.request_lifecycle_commands%rowtype;
    v_now timestamptz := now();
    v_number text;
    v_data jsonb := command->'request_data';
    v_status text;
    v_intake_status text;
    v_result jsonb;
    v_incoming_type text;
    v_outgoing_type text;
begin
    if v_user_id is null or v_request_id is null or v_key is null
       or btrim(v_key) = '' or v_fingerprint is null then
        raise exception 'invalid_lifecycle_command' using errcode = '22023';
    end if;
    if v_command_type not in ('confirm', 'return_to_editing', 'cancel') then
        raise exception 'invalid_lifecycle_command_type' using errcode = '22023';
    end if;

    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            v_user_id::text || ':' || v_command_type || ':' || v_key,
            0
        )
    );

    -- Replay precedes ownership, status and stale-version rejection.
    select * into v_existing
    from public.request_lifecycle_commands as c
    where c.user_id = v_user_id
      and c.command_type = v_command_type
      and c.idempotency_key = v_key;
    if found then
        if v_existing.fingerprint is distinct from v_fingerprint
           or v_existing.request_id is distinct from v_request_id then
            raise exception 'lifecycle_idempotency_conflict' using errcode = '23505';
        end if;
        return jsonb_build_object('result', v_existing.result, 'replayed', true);
    end if;

    select * into v_request
    from public.requests as r
    where r.id = v_request_id
    for update;
    if not found then
        raise exception 'lifecycle_request_not_found' using errcode = 'P0002';
    end if;
    if v_request.user_id is distinct from v_user_id then
        raise exception 'lifecycle_ownership_mismatch' using errcode = '42501';
    end if;
    if v_request.status = 'new' then
        raise exception 'request_already_registered' using errcode = '55000';
    end if;
    if v_request.status = 'cancelled' then
        raise exception 'request_already_cancelled' using errcode = '55000';
    end if;
    if v_request.status <> 'draft' then
        raise exception 'lifecycle_transition_not_allowed' using errcode = '55000';
    end if;
    if v_request.version <> v_expected_version then
        raise exception 'concurrent_lifecycle_update' using errcode = '40001';
    end if;

    -- Dialog state is server-owned persistence state. Python owns readiness
    -- computation; SQL only validates the allowed persisted transition.
    select * into v_dialog
    from public.dialog_states as ds
    where ds.user_id = v_user_id
    for update;
    if not found
       or v_dialog.active_request_id is distinct from v_request_id
       or v_dialog.state_data->>'request_id' is distinct from v_request_id::text
       or v_dialog.state_data->>'user_id' is distinct from v_user_id::text
       or (v_dialog.state_data->>'state_version')::bigint is distinct from
          v_request.version then
        raise exception 'lifecycle_dialog_mismatch' using errcode = '55000';
    end if;
    if v_command_type in ('confirm', 'return_to_editing')
       and coalesce(
           v_dialog.state_data->>'intake_status', v_dialog.current_step
       ) <> 'ready_for_confirmation' then
        raise exception 'lifecycle_transition_not_allowed' using errcode = '55000';
    end if;
    if v_command_type = 'cancel'
       and coalesce(v_dialog.state_data->>'intake_status', v_dialog.current_step)
           not in ('collecting', 'conflict', 'ready_for_confirmation', 'editing') then
        raise exception 'lifecycle_transition_not_allowed' using errcode = '55000';
    end if;

    if v_command_type = 'confirm' then
        if v_data->'lifecycle'->'final_request_card' is null
           or v_data->'lifecycle'->'final_completeness' is null
           or v_data->'lifecycle'->'final_approval_route' is null
           or v_data->'lifecycle'->>'registered_schema_version' is null
           or v_data->'lifecycle'->>'registry_version' is null
           or v_data->'lifecycle'->>'approval_rules_version' is null then
            raise exception 'invalid_lifecycle_snapshot' using errcode = '22023';
        end if;
        -- Python sends a version-checked canonical intake representation plus
        -- its synchronized legacy projections and final lifecycle snapshot.
        -- Unrelated keys were copied from the locked request during mapping.
        v_number := 'PR-' || to_char(v_now, 'YYYY') || '-' ||
            lpad(nextval('public.request_number_seq')::text, 6, '0');
        v_status := 'new';
        v_intake_status := 'completed';
        v_data := jsonb_set(
            jsonb_set(
                jsonb_set(
                    v_data,
                    '{lifecycle,registered_at}',
                    to_jsonb(v_now),
                    true
                ),
                '{lifecycle,confirmed_at}',
                to_jsonb(v_now),
                true
            ),
            '{lifecycle,confirmed_by}',
            to_jsonb(v_user_id),
            true
        );
        update public.requests
        set data = v_data,
            request_type = command->>'request_type',
            category_code = command->>'category_code',
            title = command->>'title',
            request_number = v_number,
            status = v_status,
            registered_at = v_now,
            confirmed_at = v_now,
            confirmed_by = v_user_id,
            version = version + 1
        where id = v_request_id and version = v_expected_version
        returning * into v_request;
        v_incoming_type := 'confirm_command';
        v_outgoing_type := 'request_registered';
    elsif v_command_type = 'return_to_editing' then
        v_status := 'draft';
        v_intake_status := 'editing';
        v_data := jsonb_set(
            v_request.data,
            '{intake,intake_status}',
            to_jsonb(v_intake_status),
            true
        );
        update public.requests
        set data = v_data,
            version = version + 1
        where id = v_request_id and version = v_expected_version
        returning * into v_request;
        v_incoming_type := 'return_to_editing_command';
        v_outgoing_type := 'request_returned_to_editing';
    else
        v_status := 'cancelled';
        v_intake_status := 'cancelled';
        v_data := jsonb_set(
            jsonb_set(
                v_request.data,
                '{intake,intake_status}',
                to_jsonb(v_intake_status),
                true
            ),
            '{lifecycle}',
            coalesce(v_request.data->'lifecycle', '{}'::jsonb) ||
                jsonb_build_object(
                    'cancelled_at', v_now,
                    'cancelled_by', v_user_id,
                    'cancellation_reason', command->>'cancellation_reason'
                ),
            true
        );
        update public.requests
        set data = v_data,
            status = v_status,
            cancelled_at = v_now,
            cancelled_by = v_user_id,
            cancellation_reason = command->>'cancellation_reason',
            version = version + 1
        where id = v_request_id and version = v_expected_version
        returning * into v_request;
        v_incoming_type := 'cancel_command';
        v_outgoing_type := 'request_cancelled';
    end if;

    if not found then
        raise exception 'concurrent_lifecycle_update' using errcode = '40001';
    end if;

    insert into public.dialog_states (
        user_id, active_request_id, current_intent, current_step, state_data
    ) values (
        v_user_id,
        case when v_status = 'draft' then v_request_id else null end,
        'request_lifecycle',
        v_intake_status,
        jsonb_build_object(
            'user_id', v_user_id,
            'request_id', v_request_id,
            'intake_status', v_intake_status,
            'state_version', v_request.version,
            'metadata', jsonb_build_object(
                'request_status', v_status,
                'active', v_status = 'draft'
            )
        )
    )
    on conflict (user_id) do update
    set active_request_id = excluded.active_request_id,
        current_intent = excluded.current_intent,
        current_step = excluded.current_step,
        state_data = excluded.state_data;

    insert into public.message_logs (
        user_id, request_id, user_message, direction, message_type,
        lifecycle_command_type, lifecycle_idempotency_key, intake_status,
        payload, metadata, duration_ms
    ) values (
        v_user_id, v_request_id, '[request lifecycle command]', 'incoming',
        v_incoming_type, v_command_type, v_key, v_intake_status,
        jsonb_build_object(
            'command_type', v_command_type,
            'expected_version', v_expected_version,
            'resulting_version', v_request.version
        ),
        '{"contains_secrets":false,"lifecycle":true}'::jsonb,
        nullif(command->>'duration_ms', '')::integer
    );

    insert into public.message_logs (
        user_id, request_id, user_message, assistant_message, direction,
        message_type, lifecycle_command_type, lifecycle_idempotency_key,
        intake_status, payload, metadata, duration_ms
    ) values (
        v_user_id, v_request_id, '[request lifecycle result]', v_outgoing_type,
        'outgoing', v_outgoing_type, v_command_type, v_key, v_intake_status,
        jsonb_build_object(
            'status', v_status,
            'request_number', v_request.request_number,
            'resulting_version', v_request.version
        ),
        '{"contains_secrets":false,"lifecycle":true}'::jsonb,
        nullif(command->>'duration_ms', '')::integer
    );

    v_result := jsonb_build_object(
        'request_id', v_request.id,
        'user_id', v_request.user_id,
        'request_number', v_request.request_number,
        'status', v_request.status,
        'intake_status', v_intake_status,
        'version', v_request.version,
        'registered_at', v_request.registered_at,
        'confirmed_at', v_request.confirmed_at,
        'cancelled_at', v_request.cancelled_at,
        'cancellation_reason', v_request.cancellation_reason,
        'replayed', false,
        'request_card', case when v_command_type = 'confirm'
            then v_data->'lifecycle'->'final_request_card'
            else command->'request_card' end,
        'approval_route', case when v_command_type = 'confirm'
            then v_data->'lifecycle'->'final_approval_route'
            else command->'approval_route' end,
        'editable', v_command_type = 'return_to_editing',
        'editable_field_codes', coalesce(command->'editable_field_codes', '[]'::jsonb),
        'instruction', case when v_command_type = 'return_to_editing'
            then 'Отправьте structured update с изменяемыми полями' else null end,
        'warnings', '[]'::jsonb
    );

    insert into public.request_lifecycle_commands (
        user_id, request_id, command_type, idempotency_key, fingerprint, result
    ) values (
        v_user_id, v_request_id, v_command_type, v_key, v_fingerprint, v_result
    );
    return jsonb_build_object('result', v_result, 'replayed', false);
end;
$$;

create or replace function public.confirm_request(command jsonb)
returns jsonb language sql security definer
set search_path = public, pg_catalog
as $$ select public.apply_request_lifecycle_command(command || '{"command_type":"confirm"}'::jsonb) $$;

create or replace function public.return_request_to_editing(command jsonb)
returns jsonb language sql security definer
set search_path = public, pg_catalog
as $$ select public.apply_request_lifecycle_command(command || '{"command_type":"return_to_editing"}'::jsonb) $$;

create or replace function public.cancel_request(command jsonb)
returns jsonb language sql security definer
set search_path = public, pg_catalog
as $$ select public.apply_request_lifecycle_command(command || '{"command_type":"cancel"}'::jsonb) $$;

create or replace function public.mark_request_collecting(
    user_id uuid, request_id uuid, expected_version bigint, intake_status text,
    next_question jsonb
)
returns void language plpgsql security definer
set search_path = public, pg_catalog
as $$
declare
    v_request public.requests%rowtype;
    v_dialog public.dialog_states%rowtype;
begin
    select * into v_request from public.requests as r
    where r.id = mark_request_collecting.request_id for update;
    if not found then raise exception 'lifecycle_request_not_found' using errcode='P0002'; end if;
    if v_request.user_id is distinct from user_id then
        raise exception 'lifecycle_ownership_mismatch' using errcode='42501';
    end if;
    if v_request.status <> 'draft' then
        raise exception 'lifecycle_transition_not_allowed' using errcode='55000';
    end if;
    if v_request.version <> expected_version then
        raise exception 'concurrent_lifecycle_update' using errcode='40001';
    end if;
    select * into v_dialog from public.dialog_states as ds
    where ds.user_id = mark_request_collecting.user_id for update;
    if not found
       or v_dialog.active_request_id is distinct from mark_request_collecting.request_id
       or v_dialog.state_data->>'request_id' is distinct from
          mark_request_collecting.request_id::text
       or v_dialog.state_data->>'user_id' is distinct from
          mark_request_collecting.user_id::text
       or (v_dialog.state_data->>'state_version')::bigint is distinct from
          mark_request_collecting.expected_version then
        raise exception 'lifecycle_dialog_mismatch' using errcode='55000';
    end if;
    update public.dialog_states
    set current_intent = 'intake', current_step = intake_status,
        state_data = state_data || jsonb_build_object(
            'intake_status', mark_request_collecting.intake_status,
            'state_version', mark_request_collecting.expected_version,
            'awaiting_field_code', next_question->>'field_code',
            'next_question', next_question
        )
    where dialog_states.user_id = mark_request_collecting.user_id;
end;
$$;

-- Failure audit is best effort and never changes request/dialog lifecycle state.
create or replace function public.record_request_lifecycle_failure(event jsonb)
returns void language plpgsql security definer
set search_path = public, pg_catalog
as $$
declare
    v_user_id uuid := (event->>'user_id')::uuid;
    v_request_id uuid := (event->>'request_id')::uuid;
    v_command_type text := event->>'command_type';
    v_incoming_type text;
    v_outgoing_type text;
begin
    if not exists (
        select 1 from public.requests as r
        where r.id = v_request_id and r.user_id = v_user_id
    ) then
        return;
    end if;
    v_incoming_type := case v_command_type
        when 'confirm' then 'confirm_command'
        when 'return_to_editing' then 'return_to_editing_command'
        when 'cancel' then 'cancel_command'
        else null
    end;
    if v_incoming_type is null then return; end if;
    v_outgoing_type := case
        when event->>'error_type' = 'LifecyclePersistenceError'
            then 'lifecycle_error'
        else 'lifecycle_conflict'
    end;
    insert into public.message_logs (
        user_id, request_id, user_message, direction, message_type,
        lifecycle_command_type, lifecycle_idempotency_key, payload, metadata,
        duration_ms
    ) values (
        v_user_id, v_request_id, '[request lifecycle command]', 'incoming',
        v_incoming_type, v_command_type, event->>'idempotency_key',
        jsonb_build_object(
            'command_type', v_command_type,
            'expected_version', (event->>'expected_version')::bigint
        ),
        '{"contains_secrets":false,"lifecycle":true}'::jsonb,
        nullif(event->>'duration_ms', '')::integer
    );
    insert into public.message_logs (
        user_id, request_id, user_message, assistant_message, direction,
        message_type, lifecycle_command_type, lifecycle_idempotency_key,
        payload, metadata, duration_ms
    ) values (
        v_user_id, v_request_id, '[request lifecycle result]', v_outgoing_type,
        'outgoing', v_outgoing_type, v_command_type,
        event->>'idempotency_key',
        jsonb_build_object(
            'command_type', v_command_type,
            'error_type', event->>'error_type'
        ),
        '{"contains_secrets":false,"lifecycle":true}'::jsonb,
        nullif(event->>'duration_ms', '')::integer
    );
end;
$$;

-- The implementation function is never callable by client roles.
revoke all on function public.apply_request_lifecycle_command(jsonb)
from public, anon, authenticated, service_role;

revoke all on function public.confirm_request(jsonb)
from public, anon, authenticated;
revoke all on function public.return_request_to_editing(jsonb)
from public, anon, authenticated;
revoke all on function public.cancel_request(jsonb)
from public, anon, authenticated;
revoke all on function public.mark_request_collecting(uuid, uuid, bigint, text, jsonb)
from public, anon, authenticated;
revoke all on function public.record_request_lifecycle_failure(jsonb)
from public, anon, authenticated;

grant execute on function public.confirm_request(jsonb) to service_role;
grant execute on function public.return_request_to_editing(jsonb) to service_role;
grant execute on function public.cancel_request(jsonb) to service_role;
grant execute on function public.mark_request_collecting(uuid, uuid, bigint, text, jsonb)
to service_role;
grant execute on function public.record_request_lifecycle_failure(jsonb)
to service_role;
grant select, insert on public.request_lifecycle_commands to service_role;
grant usage, select on sequence public.request_number_seq to service_role;

alter function public.apply_request_lifecycle_command(jsonb) owner to postgres;
alter function public.confirm_request(jsonb) owner to postgres;
alter function public.return_request_to_editing(jsonb) owner to postgres;
alter function public.cancel_request(jsonb) owner to postgres;
alter function public.mark_request_collecting(uuid, uuid, bigint, text, jsonb)
owner to postgres;
alter function public.record_request_lifecycle_failure(jsonb) owner to postgres;

commit;
