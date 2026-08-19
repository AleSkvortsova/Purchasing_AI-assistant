-- Block direct Data API row access while server-side clients use service_role.
-- No anon/authenticated policies are intentional for the Telegram-only MVP.

begin;

alter table public.users enable row level security;
alter table public.requests enable row level security;
alter table public.dialog_states enable row level security;
alter table public.message_logs enable row level security;
alter table public.knowledge_documents enable row level security;
alter table public.knowledge_chunks enable row level security;
alter table public.request_lifecycle_commands enable row level security;

commit;
