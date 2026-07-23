-- Deterministic approval routing rules. Does not approve or mutate requests.

create or replace function public.is_nonempty_text_jsonb_array(value jsonb)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
    select case
        when pg_catalog.jsonb_typeof(value) <> 'array' then false
        when pg_catalog.jsonb_array_length(value) = 0 then false
        else not exists (
            select 1
            from pg_catalog.jsonb_array_elements(value) as item
            where pg_catalog.jsonb_typeof(item) <> 'string'
               or pg_catalog.btrim(item #>> '{}') = ''
        )
    end;
$$;

create table if not exists public.approval_base_rules (
    id uuid primary key default gen_random_uuid(),
    rule_code text unique not null,
    budget_status text not null,
    min_amount numeric(15, 2) not null,
    max_amount numeric(15, 2),
    approvers jsonb not null,
    priority integer not null default 100,
    is_active boolean not null default true,
    effective_from date not null,
    effective_to date,
    source_document_id text not null,
    source_section text not null,
    description text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint approval_base_rules_budget_status_check
        check (budget_status in ('budgeted', 'unbudgeted')),
    constraint approval_base_rules_min_amount_check
        check (min_amount >= 0),
    constraint approval_base_rules_amount_range_check
        check (max_amount is null or max_amount >= min_amount),
    constraint approval_base_rules_approvers_check
        check (public.is_nonempty_text_jsonb_array(approvers)),
    constraint approval_base_rules_priority_check
        check (priority > 0),
    constraint approval_base_rules_effective_dates_check
        check (effective_to is null or effective_to >= effective_from)
);

create table if not exists public.approval_additional_rules (
    id uuid primary key default gen_random_uuid(),
    rule_code text unique not null,
    condition_type text not null,
    condition_value text not null,
    min_amount numeric(15, 2),
    max_amount numeric(15, 2),
    approvers jsonb not null,
    priority integer not null default 100,
    is_active boolean not null default true,
    effective_from date not null,
    effective_to date,
    source_document_id text not null,
    source_section text not null,
    description text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint approval_additional_rules_condition_type_check
        check (
            condition_type in (
                'urgency',
                'single_supplier',
                'category',
                'data_access',
                'work_on_site'
            )
        ),
    constraint approval_additional_rules_min_amount_check
        check (min_amount is null or min_amount >= 0),
    constraint approval_additional_rules_amount_range_check
        check (
            max_amount is null
            or (min_amount is not null and max_amount >= min_amount)
        ),
    constraint approval_additional_rules_approvers_check
        check (public.is_nonempty_text_jsonb_array(approvers)),
    constraint approval_additional_rules_priority_check
        check (priority > 0),
    constraint approval_additional_rules_effective_dates_check
        check (effective_to is null or effective_to >= effective_from)
);

do $triggers$
begin
    if not exists (
        select 1
        from pg_catalog.pg_trigger
        where tgname = 'approval_base_rules_set_updated_at'
          and tgrelid = 'public.approval_base_rules'::regclass
    ) then
        create trigger approval_base_rules_set_updated_at
        before update on public.approval_base_rules
        for each row execute function public.set_updated_at();
    end if;

    if not exists (
        select 1
        from pg_catalog.pg_trigger
        where tgname = 'approval_additional_rules_set_updated_at'
          and tgrelid = 'public.approval_additional_rules'::regclass
    ) then
        create trigger approval_additional_rules_set_updated_at
        before update on public.approval_additional_rules
        for each row execute function public.set_updated_at();
    end if;
end;
$triggers$;

create index if not exists approval_base_rules_status_active_idx
    on public.approval_base_rules (budget_status, is_active);
create index if not exists approval_base_rules_amount_idx
    on public.approval_base_rules (min_amount, max_amount);
create index if not exists approval_base_rules_effective_dates_idx
    on public.approval_base_rules (effective_from, effective_to);
create index if not exists approval_additional_rules_condition_active_idx
    on public.approval_additional_rules (
        condition_type,
        condition_value,
        is_active
    );
create index if not exists approval_additional_rules_effective_dates_idx
    on public.approval_additional_rules (effective_from, effective_to);

insert into public.approval_base_rules (
    rule_code,
    budget_status,
    min_amount,
    max_amount,
    approvers,
    priority,
    is_active,
    effective_from,
    effective_to,
    source_document_id,
    source_section,
    description
)
values
    (
        'BUDGETED_0_100000', 'budgeted', 0, 100000,
        '["Руководитель подразделения"]'::jsonb,
        10, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Бюджетная закупка до 100 000 рублей включительно.'
    ),
    (
        'BUDGETED_100000_01_500000', 'budgeted', 100000.01, 500000,
        '["Руководитель подразделения", "Финансовый контролёр"]'::jsonb,
        20, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Бюджетная закупка свыше 100 000 и до 500 000 рублей включительно.'
    ),
    (
        'BUDGETED_500000_01_PLUS', 'budgeted', 500000.01, null,
        '["Руководитель подразделения", "Финансовый блок", "Руководитель закупок"]'::jsonb,
        30, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Бюджетная закупка свыше 500 000 рублей.'
    ),
    (
        'UNBUDGETED_0_100000', 'unbudgeted', 0, 100000,
        '["Руководитель подразделения", "Финансовый директор"]'::jsonb,
        10, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Внебюджетная закупка до 100 000 рублей включительно.'
    ),
    (
        'UNBUDGETED_100000_01_PLUS', 'unbudgeted', 100000.01, null,
        '["Руководитель подразделения", "Финансовый директор", "Генеральный директор"]'::jsonb,
        20, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Внебюджетная закупка свыше 100 000 рублей.'
    )
on conflict (rule_code) do update set
    budget_status = excluded.budget_status,
    min_amount = excluded.min_amount,
    max_amount = excluded.max_amount,
    approvers = excluded.approvers,
    priority = excluded.priority,
    is_active = excluded.is_active,
    effective_from = excluded.effective_from,
    effective_to = excluded.effective_to,
    source_document_id = excluded.source_document_id,
    source_section = excluded.source_section,
    description = excluded.description;

insert into public.approval_additional_rules (
    rule_code,
    condition_type,
    condition_value,
    min_amount,
    max_amount,
    approvers,
    priority,
    is_active,
    effective_from,
    effective_to,
    source_document_id,
    source_section,
    description
)
values
    (
        'URGENCY_P1', 'urgency', 'P1', null, null,
        '["Руководитель закупок"]'::jsonb,
        10, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Срочная заявка P1; определение приоритета описано в kb-006.'
    ),
    (
        'URGENCY_P2', 'urgency', 'P2', null, null,
        '["Руководитель закупок"]'::jsonb,
        20, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Срочная заявка P2; определение приоритета описано в kb-006.'
    ),
    (
        'SINGLE_SUPPLIER', 'single_supplier', 'true', null, null,
        '["Руководитель закупок"]'::jsonb,
        30, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Закупка у единственного поставщика.'
    ),
    (
        'SINGLE_SUPPLIER_OVER_500000', 'single_supplier', 'true',
        500000.01, null, '["Финансовый блок"]'::jsonb,
        40, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Единственный поставщик при сумме свыше 500 000 рублей.'
    ),
    (
        'IT_DATA_ACCESS', 'data_access', 'true', null, null,
        '["IT / информационная безопасность"]'::jsonb,
        50, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'IT-закупка с доступом к данным.'
    ),
    (
        'LEGAL_SERVICES', 'category', 'S11', null, null,
        '["Юридическая служба"]'::jsonb,
        60, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Категория S11: консалтинг и юридические услуги.'
    ),
    (
        'WORK_ON_SITE', 'work_on_site', 'true', null, null,
        '["АХО / охрана труда"]'::jsonb,
        70, true, '2026-07-21', null, 'kb-009',
        'Матрица согласования',
        'Работы на объекте.'
    )
on conflict (rule_code) do update set
    condition_type = excluded.condition_type,
    condition_value = excluded.condition_value,
    min_amount = excluded.min_amount,
    max_amount = excluded.max_amount,
    approvers = excluded.approvers,
    priority = excluded.priority,
    is_active = excluded.is_active,
    effective_from = excluded.effective_from,
    effective_to = excluded.effective_to,
    source_document_id = excluded.source_document_id,
    source_section = excluded.source_section,
    description = excluded.description;

revoke all on table public.approval_base_rules
from public, anon, authenticated;
revoke all on table public.approval_additional_rules
from public, anon, authenticated;
revoke execute on function public.is_nonempty_text_jsonb_array(jsonb)
from public, anon, authenticated;

grant select on table public.approval_base_rules to service_role;
grant select on table public.approval_additional_rules to service_role;
grant execute on function public.is_nonempty_text_jsonb_array(jsonb)
to service_role;
