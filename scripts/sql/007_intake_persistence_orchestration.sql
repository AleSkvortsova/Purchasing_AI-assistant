-- Persistence guarantees for deterministic multi-step intake.
-- Prepared only: do not apply without reviewing existing duplicate drafts.

begin;

alter table public.requests
    add column if not exists version bigint not null default 1;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'requests_version_positive'
          and conrelid = 'public.requests'::regclass
    ) then
        alter table public.requests
            add constraint requests_version_positive check (version >= 1);
    end if;
end;
$$;

alter table public.message_logs
    add column if not exists direction text,
    add column if not exists message_type text,
    add column if not exists message_id text,
    add column if not exists idempotency_key text,
    add column if not exists idempotency_fingerprint text,
    add column if not exists idempotency_result jsonb,
    add column if not exists field_code text,
    add column if not exists intake_status text,
    add column if not exists payload jsonb not null default '{}'::jsonb,
    add column if not exists metadata jsonb not null default '{}'::jsonb;

-- Fail before either unique index is built. BEGIN/COMMIT rolls back every DDL
-- statement in this file, so existing duplicates cannot leave a partial schema.
do $$
begin
    if exists (
        select 1
        from public.requests
        where status = 'draft'
        group by user_id
        having count(*) > 1
    ) then
        raise exception 'duplicate_active_intake_drafts: run 007 preflight'
            using errcode = '23505';
    end if;

    if exists (
        select 1
        from public.message_logs
        where direction = 'incoming'
          and idempotency_key is not null
        group by user_id, idempotency_key
        having count(*) > 1
    ) then
        raise exception 'duplicate_intake_idempotency_keys: run 007 preflight'
            using errcode = '23505';
    end if;
end;
$$;

create unique index if not exists requests_one_active_draft_per_user_idx
    on public.requests (user_id)
    where status = 'draft';

create unique index if not exists message_logs_intake_idempotency_idx
    on public.message_logs (user_id, idempotency_key)
    where direction = 'incoming' and idempotency_key is not null;

create or replace function public.save_intake_step(
    request_id uuid,
    expected_version bigint,
    request_type text,
    category_code text,
    title text,
    request_data jsonb,
    dialog_state jsonb,
    incoming_log jsonb,
    outgoing_log jsonb,
    idempotency_record jsonb default null
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
declare
    updated_request public.requests%rowtype;
    locked_user_id uuid;
    locked_status text;
    replay_fingerprint text;
    replay_result jsonb;
begin
    -- Database-level replay happens before ownership/status/version checks.
    -- This makes a completed delivery replayable even after request version moves.
    if idempotency_record is not null then
        select ml.idempotency_fingerprint, ml.idempotency_result
        into replay_fingerprint, replay_result
        from public.message_logs as ml
        where ml.user_id = (idempotency_record->>'user_id')::uuid
          and ml.direction = 'incoming'
          and ml.idempotency_key = idempotency_record->>'key'
        limit 1;

        if found then
            if replay_fingerprint is distinct from
               idempotency_record->>'fingerprint' then
                raise exception 'idempotency_conflict'
                    using errcode = '23505';
            end if;
            if replay_result->>'request_id' is distinct from request_id::text then
                raise exception 'idempotency_request_mismatch'
                    using errcode = '42501';
            end if;
            return jsonb_build_object(
                'request_version', (replay_result->>'request_version')::bigint,
                'dialog_state', replay_result->'dialog_state',
                'replayed', true
            );
        end if;
    end if;

    -- All identifiers used below must describe the same server-owned objects.
    if (dialog_state->>'request_id')::uuid is distinct from request_id
       or (incoming_log->>'request_id')::uuid is distinct from request_id
       or (outgoing_log->>'request_id')::uuid is distinct from request_id
       or (incoming_log->>'user_id')::uuid is distinct from
          (dialog_state->>'user_id')::uuid
       or (outgoing_log->>'user_id')::uuid is distinct from
          (dialog_state->>'user_id')::uuid then
        raise exception 'intake_payload_identity_mismatch'
            using errcode = '42501';
    end if;

    -- Lock and verify ownership/editability before applying the versioned update.
    select r.user_id, r.status
    into locked_user_id, locked_status
    from public.requests as r
    where r.id = request_id
    for update;

    if not found then
        raise exception 'intake_request_not_found'
            using errcode = 'P0002';
    end if;
    if locked_user_id is distinct from (dialog_state->>'user_id')::uuid then
        raise exception 'intake_request_ownership_mismatch'
            using errcode = '42501';
    end if;
    if locked_status <> 'draft' then
        raise exception 'intake_request_not_editable'
            using errcode = '55000';
    end if;

    update public.requests
    set request_type = save_intake_step.request_type,
        category_code = save_intake_step.category_code,
        title = save_intake_step.title,
        data = save_intake_step.request_data,
        version = version + 1
    where id = save_intake_step.request_id
      and version = save_intake_step.expected_version
      and status = 'draft'
      and user_id = locked_user_id
    returning * into updated_request;

    if not found then
        raise exception 'concurrent_intake_update'
            using errcode = '40001';
    end if;

    insert into public.dialog_states (
        user_id,
        active_request_id,
        current_intent,
        current_step,
        state_data
    ) values (
        (dialog_state->>'user_id')::uuid,
        (dialog_state->>'request_id')::uuid,
        'intake',
        dialog_state->>'intake_status',
        dialog_state || jsonb_build_object(
            'state_version', updated_request.version
        )
    )
    on conflict (user_id) do update
    set active_request_id = excluded.active_request_id,
        current_intent = excluded.current_intent,
        current_step = excluded.current_step,
        state_data = excluded.state_data;

    insert into public.message_logs (
        user_id,
        request_id,
        user_message,
        direction,
        message_type,
        message_id,
        idempotency_key,
        idempotency_fingerprint,
        idempotency_result,
        field_code,
        intake_status,
        payload,
        metadata,
        duration_ms
    ) values (
        updated_request.user_id,
        updated_request.id,
        '[structured intake update]',
        'incoming',
        incoming_log->>'message_type',
        incoming_log->>'message_id',
        idempotency_record->>'key',
        idempotency_record->>'fingerprint',
        idempotency_record->'result',
        incoming_log->>'field_code',
        incoming_log->>'intake_status',
        coalesce(incoming_log->'payload', '{}'::jsonb),
        coalesce(incoming_log->'metadata', '{}'::jsonb),
        nullif(incoming_log->>'duration_ms', '')::integer
    );

    insert into public.message_logs (
        user_id,
        request_id,
        user_message,
        assistant_message,
        direction,
        message_type,
        message_id,
        field_code,
        intake_status,
        payload,
        metadata,
        duration_ms
    ) values (
        updated_request.user_id,
        updated_request.id,
        '[structured intake result]',
        outgoing_log->'payload'->>'status',
        'outgoing',
        outgoing_log->>'message_type',
        outgoing_log->>'message_id',
        outgoing_log->>'field_code',
        outgoing_log->>'intake_status',
        coalesce(outgoing_log->'payload', '{}'::jsonb),
        coalesce(outgoing_log->'metadata', '{}'::jsonb),
        nullif(outgoing_log->>'duration_ms', '')::integer
    );

    return jsonb_build_object(
        'request_version', updated_request.version,
        'dialog_state', dialog_state || jsonb_build_object(
            'state_version', updated_request.version
        )
    );
end;
$$;

revoke all on function public.save_intake_step(
    uuid, bigint, text, text, text, jsonb, jsonb, jsonb, jsonb, jsonb
) from public, anon, authenticated;
grant execute on function public.save_intake_step(
    uuid, bigint, text, text, text, jsonb, jsonb, jsonb, jsonb, jsonb
) to service_role;

-- Supabase migrations are expected to run as the postgres owner. Keeping the
-- owner explicit prevents SECURITY DEFINER semantics from drifting on reruns.
alter function public.save_intake_step(
    uuid, bigint, text, text, text, jsonb, jsonb, jsonb, jsonb, jsonb
) owner to postgres;

commit;
