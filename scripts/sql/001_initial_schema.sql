create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create table if not exists public.users (
    id uuid primary key default gen_random_uuid(),
    telegram_id bigint unique,
    full_name text not null,
    department text,
    role text not null default 'requester',
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint users_role_check
        check (role in ('requester', 'buyer', 'admin'))
);

create table if not exists public.requests (
    id uuid primary key default gen_random_uuid(),
    request_number text unique,
    user_id uuid not null references public.users(id),
    request_type text,
    category_code text,
    title text,
    status text not null default 'draft',
    data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    confirmed_at timestamptz,
    constraint requests_type_check
        check (request_type is null or request_type in ('product', 'service')),
    constraint requests_status_check
        check (status in ('draft', 'new', 'cancelled'))
);

create table if not exists public.dialog_states (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null unique
        references public.users(id) on delete cascade,
    active_request_id uuid
        references public.requests(id) on delete set null,
    current_intent text,
    current_step text,
    state_data jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create table if not exists public.message_logs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references public.users(id) on delete set null,
    request_id uuid references public.requests(id) on delete set null,
    user_message text not null,
    assistant_message text,
    intent text,
    model text,
    input_tokens integer,
    output_tokens integer,
    duration_ms integer,
    sources jsonb not null default '[]'::jsonb,
    error text,
    created_at timestamptz not null default now(),
    constraint message_logs_input_tokens_check
        check (input_tokens is null or input_tokens >= 0),
    constraint message_logs_output_tokens_check
        check (output_tokens is null or output_tokens >= 0),
    constraint message_logs_duration_check
        check (duration_ms is null or duration_ms >= 0)
);

create index if not exists requests_user_status_idx
    on public.requests (user_id, status);
create index if not exists message_logs_user_created_idx
    on public.message_logs (user_id, created_at desc);
create index if not exists message_logs_request_idx
    on public.message_logs (request_id);

drop trigger if exists users_set_updated_at on public.users;
create trigger users_set_updated_at
before update on public.users
for each row execute function public.set_updated_at();

drop trigger if exists requests_set_updated_at on public.requests;
create trigger requests_set_updated_at
before update on public.requests
for each row execute function public.set_updated_at();

drop trigger if exists dialog_states_set_updated_at on public.dialog_states;
create trigger dialog_states_set_updated_at
before update on public.dialog_states
for each row execute function public.set_updated_at();

grant usage on schema public to service_role;
grant select, insert, update, delete
on public.users, public.requests, public.dialog_states, public.message_logs
to service_role;
