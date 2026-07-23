create extension if not exists vector;

create table if not exists public.knowledge_documents (
    id uuid primary key default gen_random_uuid(),
    document_id text unique not null,
    filename text not null,
    title text not null,
    document_type text not null,
    version text not null,
    effective_date date not null,
    owner text not null,
    priority integer not null,
    status text not null,
    language text not null default 'ru',
    sha256 text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint knowledge_documents_priority_check check (priority > 0),
    constraint knowledge_documents_status_check
        check (status in ('active', 'inactive', 'archived'))
);

create table if not exists public.knowledge_chunks (
    id uuid primary key,
    document_id text not null
        references public.knowledge_documents(document_id)
        on update cascade on delete cascade,
    source_filename text not null,
    document_title text not null,
    document_type text not null,
    section_path text not null,
    heading text,
    content text not null,
    content_sha256 text not null,
    chunk_index integer not null,
    priority integer not null,
    version text not null,
    effective_date date not null,
    token_count_estimate integer not null,
    char_count integer not null,
    metadata jsonb not null default '{}'::jsonb,
    embedding vector(1536),
    embedding_model text,
    embedded_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint knowledge_chunks_document_index_key
        unique (document_id, chunk_index),
    constraint knowledge_chunks_index_check check (chunk_index >= 0),
    constraint knowledge_chunks_tokens_check check (token_count_estimate > 0),
    constraint knowledge_chunks_chars_check check (char_count > 0),
    constraint knowledge_chunks_priority_check check (priority > 0)
);

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgname = 'knowledge_documents_set_updated_at'
          and tgrelid = 'public.knowledge_documents'::regclass
    ) then
        execute '
            create trigger knowledge_documents_set_updated_at
            before update on public.knowledge_documents
            for each row execute function public.set_updated_at()
        ';
    end if;

    if not exists (
        select 1
        from pg_trigger
        where tgname = 'knowledge_chunks_set_updated_at'
          and tgrelid = 'public.knowledge_chunks'::regclass
    ) then
        execute '
            create trigger knowledge_chunks_set_updated_at
            before update on public.knowledge_chunks
            for each row execute function public.set_updated_at()
        ';
    end if;
end;
$$;

create index if not exists knowledge_documents_document_id_idx
    on public.knowledge_documents (document_id);
create index if not exists knowledge_documents_status_idx
    on public.knowledge_documents (status);
create index if not exists knowledge_chunks_document_id_idx
    on public.knowledge_chunks (document_id);
create index if not exists knowledge_chunks_document_type_idx
    on public.knowledge_chunks (document_type);
create index if not exists knowledge_chunks_priority_idx
    on public.knowledge_chunks (priority);
create index if not exists knowledge_chunks_content_sha256_idx
    on public.knowledge_chunks (content_sha256);

create index if not exists knowledge_chunks_embedding_hnsw_idx
    on public.knowledge_chunks
    using hnsw (embedding vector_cosine_ops)
    where embedding is not null;

create or replace function public.match_knowledge_chunks(
    query_embedding vector(1536),
    match_count integer default 5,
    similarity_threshold double precision default 0,
    filter_document_types text[] default null,
    filter_status text default 'active'
)
returns table (
    chunk_id uuid,
    document_id text,
    source_filename text,
    document_title text,
    document_type text,
    section_path text,
    heading text,
    content text,
    priority integer,
    similarity double precision,
    metadata jsonb
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
    select
        chunks.id as chunk_id,
        chunks.document_id,
        chunks.source_filename,
        chunks.document_title,
        chunks.document_type,
        chunks.section_path,
        chunks.heading,
        chunks.content,
        chunks.priority,
        (1 - (chunks.embedding <=> query_embedding))::double precision
            as similarity,
        chunks.metadata
    from public.knowledge_chunks as chunks
    join public.knowledge_documents as documents
      on documents.document_id = chunks.document_id
    where chunks.embedding is not null
      and documents.status = filter_status
      and (
          filter_document_types is null
          or chunks.document_type = any(filter_document_types)
      )
      and (1 - (chunks.embedding <=> query_embedding))
          >= similarity_threshold
    order by
        similarity desc,
        chunks.priority asc,
        chunks.chunk_index asc
    limit greatest(match_count, 0);
$$;

grant select, insert, update, delete
on public.knowledge_documents, public.knowledge_chunks
to service_role;
grant execute on function public.match_knowledge_chunks(
    vector,
    integer,
    double precision,
    text[],
    text
) to service_role;
