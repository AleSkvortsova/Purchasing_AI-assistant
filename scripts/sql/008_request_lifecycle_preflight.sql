-- Read-only preflight for migration 008. Every statement is SELECT-only.

-- 1. Existing request numbers and statuses.
select status, count(*) as request_count,
       count(*) filter (where request_number is not null) as numbered_count
from public.requests group by status order by status;

-- 2. Duplicate non-null request numbers; must be empty.
select request_number, count(*) as duplicate_count, array_agg(id) as request_ids
from public.requests where request_number is not null
group by request_number having count(*) > 1 order by request_number;

-- 3. Number-policy anomalies; must be empty.
select id, status, request_number,
       case
           when status = 'new' and request_number is null
               then 'registered_without_number'
           when status in ('draft', 'cancelled') and request_number is not null
               then 'non_registered_with_number'
       end as anomaly
from public.requests
where (status = 'new' and request_number is null)
   or (status in ('draft', 'cancelled') and request_number is not null)
order by id;

-- 4. Timestamp/actor inconsistencies, including unknown internal user UUIDs;
-- must be empty. Missing schema columns are reported by set 7, not mistaken
-- for missing row values here.
with schema_flags as (
    select
        exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'requests' and column_name = 'registered_at') as has_registered_at,
        exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'requests' and column_name = 'confirmed_by') as has_confirmed_by,
        exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'requests' and column_name = 'cancelled_at') as has_cancelled_at,
        exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'requests' and column_name = 'cancelled_by') as has_cancelled_by
)
select r.id, r.status,
       to_jsonb(r)->>'registered_at' as registered_at,
       r.confirmed_at,
       to_jsonb(r)->>'confirmed_by' as confirmed_by,
       to_jsonb(r)->>'cancelled_at' as cancelled_at,
       to_jsonb(r)->>'cancelled_by' as cancelled_by,
       case
           when r.status = 'new' and (
               r.confirmed_at is null
               or (sf.has_registered_at and coalesce(to_jsonb(r)->>'registered_at', '') = '')
               or (sf.has_confirmed_by and coalesce(to_jsonb(r)->>'confirmed_by', '') = '')
            ) then 'registered_fields_missing'
           when r.status = 'cancelled' and (
               (sf.has_cancelled_at and coalesce(to_jsonb(r)->>'cancelled_at', '') = '')
               or (sf.has_cancelled_by and coalesce(to_jsonb(r)->>'cancelled_by', '') = '')
           ) then 'cancellation_fields_missing'
           when r.status = 'draft' and (
               r.confirmed_at is not null
               or coalesce(to_jsonb(r)->>'registered_at', '') <> ''
               or coalesce(to_jsonb(r)->>'confirmed_by', '') <> ''
               or coalesce(to_jsonb(r)->>'cancelled_at', '') <> ''
               or coalesce(to_jsonb(r)->>'cancelled_by', '') <> ''
           ) then 'draft_has_terminal_fields'
           when to_jsonb(r)->>'confirmed_by' is not null and cu.id is null
               then 'unknown_confirmed_actor'
           when to_jsonb(r)->>'cancelled_by' is not null and xu.id is null
               then 'unknown_cancelled_actor'
       end as anomaly
from public.requests as r
cross join schema_flags as sf
left join public.users as cu
  on cu.id = (to_jsonb(r)->>'confirmed_by')::uuid
left join public.users as xu
  on xu.id = (to_jsonb(r)->>'cancelled_by')::uuid
where (r.status = 'new' and (
          r.confirmed_at is null
          or (sf.has_registered_at and coalesce(to_jsonb(r)->>'registered_at', '') = '')
          or (sf.has_confirmed_by and coalesce(to_jsonb(r)->>'confirmed_by', '') = '')
      ))
   or (r.status = 'cancelled' and (
          (sf.has_cancelled_at and coalesce(to_jsonb(r)->>'cancelled_at', '') = '')
          or (sf.has_cancelled_by and coalesce(to_jsonb(r)->>'cancelled_by', '') = '')
      ))
   or (r.status = 'draft' and (
          r.confirmed_at is not null
          or coalesce(to_jsonb(r)->>'registered_at', '') <> ''
          or coalesce(to_jsonb(r)->>'confirmed_by', '') <> ''
          or coalesce(to_jsonb(r)->>'cancelled_at', '') <> ''
          or coalesce(to_jsonb(r)->>'cancelled_by', '') <> ''
      ))
   or (to_jsonb(r)->>'confirmed_by' is not null and cu.id is null)
   or (to_jsonb(r)->>'cancelled_by' is not null and xu.id is null)
order by r.id;

-- 5. Active dialog states pointing to terminal requests; must be empty.
select ds.id as dialog_state_id, ds.user_id, ds.active_request_id, r.status
from public.dialog_states as ds
join public.requests as r on r.id = ds.active_request_id
where r.status in ('new', 'cancelled')
order by ds.user_id;

-- 6. Persistence status inconsistent with lifecycle JSON snapshot.
select id, status, request_number, data->'lifecycle' as lifecycle
from public.requests
where (status = 'new' and data->'lifecycle'->>'registered_schema_version' is null)
   or (status = 'cancelled' and data->'lifecycle'->>'cancelled_at' is null)
   or (status = 'draft' and data->'lifecycle'->>'registered_at' is not null)
order by id;

-- 7. Required columns after migration.
select table_name, column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public' and (
    (table_name = 'requests' and column_name in (
        'request_number', 'registered_at', 'confirmed_at', 'confirmed_by',
        'cancelled_at', 'cancelled_by', 'cancellation_reason', 'version'
    )) or
    (table_name = 'message_logs' and column_name in (
        'lifecycle_command_type', 'lifecycle_idempotency_key'
    )) or table_name = 'request_lifecycle_commands'
)
order by table_name, ordinal_position;

-- 8. Required indexes and sequence. Before migration this is expected to be
-- empty; after migration it must contain all four named objects.
select 'index'::text as object_type, indexname as object_name,
       indexdef as definition
from pg_indexes
where schemaname = 'public' and indexname in (
    'requests_request_number_lifecycle_uidx',
    'request_lifecycle_command_namespace_unique',
    'request_lifecycle_commands_request_idx'
)
union all
select 'sequence', c.relname,
       'owner=' || pg_get_userbyid(c.relowner)
from pg_class as c join pg_namespace as n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'S'
  and c.relname = 'request_number_seq'
order by object_type, object_name;

-- 9. RPC signatures, owners, security flags and ACLs.
select p.oid::regprocedure::text as function_signature,
       pg_get_userbyid(p.proowner) as function_owner,
       p.prosecdef as security_definer, p.proacl as function_acl
from pg_proc as p join pg_namespace as n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname in (
    'apply_request_lifecycle_command', 'confirm_request',
    'return_request_to_editing', 'cancel_request', 'mark_request_collecting',
    'record_request_lifecycle_failure'
) order by p.proname;
