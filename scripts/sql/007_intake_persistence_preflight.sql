-- Read-only preflight for migration 007. Every statement is SELECT-only.

-- 1. Current persistence status distribution.
select status, count(*) as request_count
from public.requests
group by status
order by status;

-- 2. Users that must manually resolve multiple active drafts before migration.
select
    user_id,
    count(*) as active_draft_count,
    array_agg(id order by updated_at desc) as request_ids,
    array_agg(updated_at order by updated_at desc) as updated_at_values
from public.requests
where status = 'draft'
group by user_id
having count(*) > 1
order by active_draft_count desc, user_id;

-- 3. Duplicate idempotency namespaces, including a partially applied 007.
select
    user_id,
    to_jsonb(ml)->>'idempotency_key' as idempotency_key,
    count(*) as duplicate_count,
    array_agg(id order by created_at) as message_log_ids
from public.message_logs as ml
where to_jsonb(ml)->>'direction' = 'incoming'
  and nullif(to_jsonb(ml)->>'idempotency_key', '') is not null
group by user_id, to_jsonb(ml)->>'idempotency_key'
having count(*) > 1
order by duplicate_count desc, user_id;

-- 4. Dialog states whose active request no longer exists.
select ds.id as dialog_state_id, ds.user_id, ds.active_request_id
from public.dialog_states as ds
left join public.requests as r on r.id = ds.active_request_id
where ds.active_request_id is not null and r.id is null
order by ds.user_id;

-- 5. Dialog state and active request owned by different users.
select
    ds.id as dialog_state_id,
    ds.user_id as dialog_user_id,
    r.id as request_id,
    r.user_id as request_user_id
from public.dialog_states as ds
join public.requests as r on r.id = ds.active_request_id
where ds.user_id <> r.user_id
order by ds.user_id;

-- 6. Message logs with missing/mismatched user or request relationships.
select
    ml.id as message_log_id,
    ml.user_id as log_user_id,
    ml.request_id,
    r.user_id as request_user_id,
    case
        when ml.user_id is not null and u.id is null then 'missing_user'
        when ml.request_id is not null and r.id is null then 'missing_request'
        when ml.user_id is not null and r.user_id is not null
             and ml.user_id <> r.user_id then 'user_request_mismatch'
    end as issue
from public.message_logs as ml
left join public.users as u on u.id = ml.user_id
left join public.requests as r on r.id = ml.request_id
where (ml.user_id is not null and u.id is null)
   or (ml.request_id is not null and r.id is null)
   or (ml.user_id is not null and r.user_id is not null
       and ml.user_id <> r.user_id)
order by ml.created_at;

-- 7. Required columns and their current defaults/nullability.
select table_name, column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
  and (
      (table_name = 'requests' and column_name = 'version')
      or (
          table_name = 'message_logs'
          and column_name in (
              'direction', 'message_type', 'message_id', 'idempotency_key',
              'idempotency_fingerprint', 'idempotency_result', 'field_code',
              'intake_status', 'payload', 'metadata'
          )
      )
  )
order by table_name, column_name;

-- 8. Required indexes and predicates.
select indexname, indexdef
from pg_indexes
where schemaname = 'public'
  and indexname in (
      'requests_one_active_draft_per_user_idx',
      'message_logs_intake_idempotency_idx'
  )
order by indexname;

-- 9. RPC signature, SECURITY DEFINER flag, owner and grants.
select
    p.oid::regprocedure::text as function_signature,
    pg_get_userbyid(p.proowner) as function_owner,
    p.prosecdef as security_definer,
    p.proacl as function_acl
from pg_proc as p
join pg_namespace as n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname = 'save_intake_step';
