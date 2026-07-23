-- Hybrid retrieval: Russian full-text search plus Reciprocal Rank Fusion.
-- Safe for an existing knowledge index: no tables or embeddings are replaced.

create or replace function public.normalize_knowledge_search_text(input_text text)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
    select regexp_replace(
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        lower(
                            replace(
                                replace(
                                    replace(coalesce(input_text, ''), chr(160), ' '),
                                    '–',
                                    '-'
                                ),
                                '—',
                                '-'
                            )
                        ),
                        '([0-9])[[:space:]]+([0-9])',
                        '\1\2',
                        'g'
                    ),
                    '([0-9])[[:space:]]+([0-9])',
                    '\1\2',
                    'g'
                ),
                '([0-9])[[:space:]]+([0-9])',
                '\1\2',
                'g'
            ),
            '[[:space:]]+',
            ' ',
            'g'
        ),
        '^[[:space:]]+|[[:space:]]+$',
        '',
        'g'
    );
$$;

do $migration$
declare
    selected_config text;
begin
    if exists (
        select 1
        from pg_catalog.pg_ts_config
        where cfgname = 'russian'
    ) then
        selected_config := 'pg_catalog.russian';
    else
        selected_config := 'pg_catalog.simple';
        raise notice 'Russian FTS config unavailable; using simple';
    end if;

    if not exists (
        select 1
        from pg_catalog.pg_attribute
        where attrelid = 'public.knowledge_chunks'::regclass
          and attname = 'search_vector'
          and not attisdropped
    ) then
        execute format(
            $sql$
            alter table public.knowledge_chunks
            add column search_vector tsvector
            generated always as (
                setweight(
                    pg_catalog.to_tsvector(%L::regconfig, coalesce(document_title, '')),
                    'A'
                )
                || setweight(
                    pg_catalog.to_tsvector(%L::regconfig, coalesce(section_path, '')),
                    'A'
                )
                || setweight(
                    pg_catalog.to_tsvector(%L::regconfig, coalesce(heading, '')),
                    'A'
                )
                || setweight(
                    pg_catalog.to_tsvector(%L::regconfig, coalesce(content, '')),
                    'B'
                )
                || setweight(
                    pg_catalog.to_tsvector(
                        %L::regconfig,
                        public.normalize_knowledge_search_text(
                            coalesce(document_title, '') || ' ' ||
                            coalesce(section_path, '') || ' ' ||
                            coalesce(heading, '') || ' ' ||
                            coalesce(content, '')
                        )
                    ),
                    'B'
                )
            ) stored
            $sql$,
            selected_config,
            selected_config,
            selected_config,
            selected_config,
            selected_config
        );
    end if;
end;
$migration$;

create index if not exists knowledge_chunks_search_vector_gin_idx
    on public.knowledge_chunks using gin (search_vector);

create or replace function public.knowledge_search_tsquery(query_text text)
returns tsquery
language sql
stable
parallel safe
set search_path = ''
as $$
    select
        pg_catalog.websearch_to_tsquery(
            case
                when exists (
                    select 1
                    from pg_catalog.pg_ts_config
                    where cfgname = 'russian'
                )
                then 'pg_catalog.russian'::regconfig
                else 'pg_catalog.simple'::regconfig
            end,
            coalesce(query_text, '')
        )
        ||
        pg_catalog.websearch_to_tsquery(
            case
                when exists (
                    select 1
                    from pg_catalog.pg_ts_config
                    where cfgname = 'russian'
                )
                then 'pg_catalog.russian'::regconfig
                else 'pg_catalog.simple'::regconfig
            end,
            public.normalize_knowledge_search_text(query_text)
        );
$$;

create or replace function public.match_knowledge_chunks_lexical(
    query_text text,
    match_count integer default 20,
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
    lexical_rank double precision,
    metadata jsonb
)
language sql
stable
security invoker
set search_path = ''
as $$
    with prepared as (
        select public.knowledge_search_tsquery(query_text) as query
    )
    select
        chunks.id,
        chunks.document_id,
        chunks.source_filename,
        chunks.document_title,
        chunks.document_type,
        chunks.section_path,
        chunks.heading,
        chunks.content,
        chunks.priority,
        pg_catalog.ts_rank_cd(chunks.search_vector, prepared.query)::double precision,
        chunks.metadata
    from public.knowledge_chunks as chunks
    join public.knowledge_documents as documents
      on documents.document_id = chunks.document_id
    cross join prepared
    where documents.status = filter_status
      and (
          filter_document_types is null
          or chunks.document_type = any(filter_document_types)
      )
      and pg_catalog.numnode(prepared.query) > 0
      and chunks.search_vector @@ prepared.query
      and pg_catalog.ts_rank_cd(chunks.search_vector, prepared.query) > 0
    order by
        pg_catalog.ts_rank_cd(chunks.search_vector, prepared.query) desc,
        chunks.priority asc,
        chunks.chunk_index asc
    limit greatest(least(coalesce(match_count, 20), 100), 0);
$$;

create or replace function public.match_knowledge_chunks_hybrid(
    query_text text,
    query_embedding vector(1536),
    final_match_count integer default 5,
    semantic_candidate_count integer default 20,
    lexical_candidate_count integer default 20,
    rrf_k integer default 60,
    semantic_weight double precision default 1.0,
    lexical_weight double precision default 1.0,
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
    lexical_score double precision,
    semantic_rank integer,
    lexical_rank integer,
    hybrid_score double precision,
    retrieval_method text,
    metadata jsonb
)
language sql
stable
security invoker
set search_path = public, extensions, pg_catalog
as $$
    with semantic_candidates as (
        select
            chunks.id,
            (1 - (chunks.embedding <=> query_embedding))::double precision
                as similarity,
            row_number() over (
                order by
                    (1 - (chunks.embedding <=> query_embedding)) desc,
                    chunks.priority asc,
                    chunks.chunk_index asc
            )::integer as rank_position
        from public.knowledge_chunks as chunks
        join public.knowledge_documents as documents
          on documents.document_id = chunks.document_id
        where chunks.embedding is not null
          and documents.status = filter_status
          and (
              filter_document_types is null
              or chunks.document_type = any(filter_document_types)
          )
        order by
            similarity desc,
            chunks.priority asc,
            chunks.chunk_index asc
        limit greatest(least(coalesce(semantic_candidate_count, 20), 100), 0)
    ),
    lexical_candidates as (
        select
            lexical.chunk_id as id,
            lexical.lexical_rank as score,
            row_number() over (
                order by
                    lexical.lexical_rank desc,
                    lexical.priority asc,
                    chunks.chunk_index asc
            )::integer as rank_position
        from public.match_knowledge_chunks_lexical(
            query_text,
            greatest(least(coalesce(lexical_candidate_count, 20), 100), 0),
            filter_document_types,
            filter_status
        ) as lexical
        join public.knowledge_chunks as chunks
          on chunks.id = lexical.chunk_id
    ),
    fused as (
        select
            coalesce(semantic.id, lexical.id) as id,
            semantic.similarity,
            lexical.score as lexical_score,
            semantic.rank_position as semantic_position,
            lexical.rank_position as lexical_position,
            (
                case
                    when semantic.rank_position is null then 0
                    else semantic_weight / (greatest(rrf_k, 1) + semantic.rank_position)
                end
                +
                case
                    when lexical.rank_position is null then 0
                    else lexical_weight / (greatest(rrf_k, 1) + lexical.rank_position)
                end
            )::double precision as hybrid_score
        from semantic_candidates as semantic
        full outer join lexical_candidates as lexical
          on lexical.id = semantic.id
    )
    select
        chunks.id,
        chunks.document_id,
        chunks.source_filename,
        chunks.document_title,
        chunks.document_type,
        chunks.section_path,
        chunks.heading,
        chunks.content,
        chunks.priority,
        fused.similarity,
        fused.lexical_score,
        fused.semantic_position,
        fused.lexical_position,
        fused.hybrid_score,
        'hybrid'::text,
        chunks.metadata
    from fused
    join public.knowledge_chunks as chunks on chunks.id = fused.id
    order by
        fused.hybrid_score desc,
        chunks.priority asc,
        least(
            coalesce(fused.semantic_position, 2147483647),
            coalesce(fused.lexical_position, 2147483647)
        ) asc,
        chunks.chunk_index asc
    limit greatest(least(coalesce(final_match_count, 5), 20), 1);
$$;

revoke execute on function public.normalize_knowledge_search_text(text)
from public, anon, authenticated;

revoke execute on function public.knowledge_search_tsquery(text)
from public, anon, authenticated;

revoke execute on function public.match_knowledge_chunks_lexical(
    text, integer, text[], text
)
from public, anon, authenticated;

revoke execute on function public.match_knowledge_chunks_hybrid(
    text,
    vector,
    integer,
    integer,
    integer,
    integer,
    double precision,
    double precision,
    text[],
    text
)
from public, anon, authenticated;

grant execute on function public.normalize_knowledge_search_text(text)
to service_role;

grant execute on function public.knowledge_search_tsquery(text)
to service_role;

grant execute on function public.match_knowledge_chunks_lexical(
    text, integer, text[], text
)
to service_role;

grant execute on function public.match_knowledge_chunks_hybrid(
    text,
    vector,
    integer,
    integer,
    integer,
    integer,
    double precision,
    double precision,
    text[],
    text
)
to service_role;