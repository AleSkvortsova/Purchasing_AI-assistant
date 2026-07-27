-- Keep legacy requests.data projections consistent on lifecycle confirmation.
-- Prepared only; do not auto-apply.
begin;

create or replace function public.synchronized_request_data_projection(
    source_data jsonb,
    persistence_request_type text,
    persistence_category_code text,
    persistence_title text,
    terminal_intake_status text default null
)
returns jsonb
language plpgsql
immutable
set search_path = pg_catalog
as $$
declare
    v_draft jsonb := source_data #> '{intake,draft}';
    v_result jsonb;
    v_request_type text;
    v_category_code text;
    v_title text;
begin
    if v_draft is null or jsonb_typeof(v_draft) <> 'object' then
        return source_data;
    end if;

    -- A damaged or legacy draft must never replace system-owned containers or
    -- separately derived projections by key collision.
    v_result := source_data || (
        v_draft
        - 'intake'
        - 'lifecycle'
        - 'schema_version'
        - 'request_type'
        - 'category_code'
        - 'title'
        - 'required_date'
    );
    if v_draft ? 'procurement_type' then
        v_request_type := case v_draft->>'procurement_type'
            when 'goods' then 'product'
            when 'service' then 'service'
            when 'work' then 'service'
            when null then null
            else null
        end;
        if v_draft->>'procurement_type' is not null
           and v_request_type is null then
            raise exception 'invalid_procurement_type_projection'
                using errcode = '22023';
        end if;
    else
        -- Backward-compatible fallback for early schema-version-1 drafts.
        v_request_type := persistence_request_type;
    end if;
    v_category_code := case when v_draft ? 'category_code'
        then v_draft->>'category_code' else persistence_category_code end;
    v_title := case when v_draft ? 'title'
        then v_draft->>'title' else persistence_title end;
    v_result := jsonb_set(
        v_result,
        '{required_date}',
        coalesce(v_draft->'desired_delivery_date', 'null'::jsonb),
        true
    );
    v_result := jsonb_set(
        v_result,
        '{request_type}',
        coalesce(to_jsonb(v_request_type), 'null'::jsonb),
        true
    );
    v_result := jsonb_set(
        v_result,
        '{category_code}',
        coalesce(to_jsonb(v_category_code), 'null'::jsonb),
        true
    );
    v_result := jsonb_set(
        v_result,
        '{title}',
        coalesce(to_jsonb(v_title), 'null'::jsonb),
        true
    );
    if terminal_intake_status is not null then
        v_result := jsonb_set(
            v_result,
            '{intake,intake_status}',
            to_jsonb(terminal_intake_status),
            true
        );
        v_result := jsonb_set(
            v_result,
            '{intake,next_question}',
            'null'::jsonb,
            true
        );
    end if;
    return v_result;
end;
$$;

create or replace function public.sync_request_data_on_registration()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
    v_draft jsonb;
    v_request_type text;
    v_category_code text;
    v_title text;
begin
    if old.status = 'draft' and new.status = 'new' then
        v_draft := new.data #> '{intake,draft}';
        if jsonb_typeof(v_draft) is distinct from 'object' then
            raise exception 'invalid_intake_draft_on_registration'
                using errcode = '22023';
        end if;

        if v_draft ? 'procurement_type' then
            v_request_type := case v_draft->>'procurement_type'
                when 'goods' then 'product'
                when 'service' then 'service'
                when 'work' then 'service'
                when null then null
                else null
            end;
            if v_draft->>'procurement_type' is not null
               and v_request_type is null then
                raise exception 'invalid_procurement_type_projection'
                    using errcode = '22023';
            end if;
        else
            v_request_type := new.request_type;
        end if;
        v_category_code := case when v_draft ? 'category_code'
            then v_draft->>'category_code' else new.category_code end;
        v_title := case when v_draft ? 'title'
            then v_draft->>'title' else new.title end;

        new.data := new.data || (
            v_draft
            - 'intake'
            - 'lifecycle'
            - 'schema_version'
            - 'request_type'
            - 'category_code'
            - 'title'
            - 'required_date'
        );
        new.data := jsonb_set(
            new.data,
            '{required_date}',
            coalesce(v_draft->'desired_delivery_date', 'null'::jsonb),
            true
        );
        new.data := jsonb_set(
            new.data,
            '{request_type}',
            coalesce(to_jsonb(v_request_type), 'null'::jsonb),
            true
        );
        new.data := jsonb_set(
            new.data,
            '{category_code}',
            coalesce(to_jsonb(v_category_code), 'null'::jsonb),
            true
        );
        new.data := jsonb_set(
            new.data,
            '{title}',
            coalesce(to_jsonb(v_title), 'null'::jsonb),
            true
        );
        new.data := jsonb_set(
            new.data,
            '{intake,intake_status}',
            '"completed"'::jsonb,
            true
        );
        new.data := jsonb_set(
            new.data,
            '{intake,next_question}',
            'null'::jsonb,
            true
        );
        new.request_type := new.data->>'request_type';
        new.category_code := new.data->>'category_code';
        new.title := new.data->>'title';
    end if;
    return new;
end;
$$;

drop trigger if exists requests_sync_data_on_registration
on public.requests;

create trigger requests_sync_data_on_registration
before update on public.requests
for each row
execute function public.sync_request_data_on_registration();

-- The current requests_status_check and RequestStatus model allow only
-- draft/new/cancelled. `new` is the only registered status in this MVP;
-- cancelled is an immutable cancelled draft and intentionally remains outside
-- this repair. lifecycle, including both final snapshots, is preserved.
with projected as (
    select
        r.id,
        public.synchronized_request_data_projection(
            r.data,
            r.request_type,
            r.category_code,
            r.title,
            'completed'
        ) as synchronized_data
    from public.requests as r
    where r.status = 'new'
      and jsonb_typeof(r.data #> '{intake,draft}') = 'object'
)
update public.requests as r
set data = p.synchronized_data,
    request_type = p.synchronized_data->>'request_type',
    category_code = p.synchronized_data->>'category_code',
    title = p.synchronized_data->>'title'
from projected as p
where r.id = p.id
  and row(r.data, r.request_type, r.category_code, r.title) is distinct from
      row(
          p.synchronized_data,
          p.synchronized_data->>'request_type',
          p.synchronized_data->>'category_code',
          p.synchronized_data->>'title'
      );

revoke all on function public.synchronized_request_data_projection(
    jsonb, text, text, text, text
) from public, anon, authenticated, service_role;

revoke all on function public.sync_request_data_on_registration()
from public, anon, authenticated, service_role;

alter function public.synchronized_request_data_projection(
    jsonb, text, text, text, text
) owner to postgres;
alter function public.sync_request_data_on_registration() owner to postgres;

commit;
