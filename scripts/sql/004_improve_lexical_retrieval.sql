-- Improve lexical candidates without changing tables, search_vector, or embeddings.

create or replace function public.normalize_knowledge_text_terms(query_text text)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
    with normalized as (
        select pg_catalog.regexp_replace(
            pg_catalog.lower(
                pg_catalog.replace(
                    pg_catalog.replace(
                        pg_catalog.replace(
                            pg_catalog.replace(
                                coalesce(query_text, ''),
                                'ё',
                                'е'
                            ),
                            chr(160),
                            ' '
                        ),
                        '–',
                        '-'
                    ),
                    '—',
                    '-'
                )
            ),
            '[^a-zа-я0-9]+',
            ' ',
            'g'
        ) as value
    ),
    terms as (
        select term, position
        from normalized
        cross join lateral pg_catalog.regexp_split_to_table(
            normalized.value,
            '[[:space:]]+'
        ) with ordinality as parts(term, position)
        where term <> ''
          and term !~ '^[0-9]+$'
          and term not in (
              'руб',
              'рубль',
              'рубля',
              'рублей',
              'рубли',
              'р',
              'кто',
              'что',
              'какой',
              'какая',
              'какие',
              'можно',
              'ли',
              'на'
          )
    )
    select coalesce(
        pg_catalog.string_agg(term, ' ' order by position),
        ''
    )
    from terms;
$$;

create or replace function public.knowledge_search_query_variants(
    query_text text
)
returns table (
    strict_query tsquery,
    text_query tsquery,
    broad_query tsquery
)
language sql
stable
parallel safe
set search_path = ''
as $$
    with selected_config as (
        select case
            when exists (
                select 1
                from pg_catalog.pg_ts_config
                where cfgname = 'russian'
            )
            then 'pg_catalog.russian'::regconfig
            else 'pg_catalog.simple'::regconfig
        end as config
    ),
    normalized as (
        select
            config,
            public.normalize_knowledge_search_text(query_text) as strict_text,
            public.normalize_knowledge_text_terms(query_text) as text_terms
        from selected_config
    ),
    prepared as (
        select
            config,
            pg_catalog.websearch_to_tsquery(config, strict_text)
                as strict_query,
            pg_catalog.plainto_tsquery(config, text_terms)
                as text_query,
            pg_catalog.to_tsvector(config, text_terms)
                as text_vector
        from normalized
    ),
    broad_expression as (
        select
            prepared.config,
            prepared.strict_query,
            prepared.text_query,
            pg_catalog.string_agg(
                pg_catalog.quote_literal(lexeme) || ':*',
                ' | '
                order by lexeme
            ) as expression
        from prepared
        left join lateral unnest(
            pg_catalog.tsvector_to_array(prepared.text_vector)
        ) as terms(lexeme) on true
        group by
            prepared.config,
            prepared.strict_query,
            prepared.text_query
    )
    select
        strict_query,
        text_query,
        case
            when expression is null
                then pg_catalog.plainto_tsquery(config, '')
            else pg_catalog.to_tsquery(config, expression)
        end as broad_query
    from broad_expression;
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
    with queries as (
        select *
        from public.knowledge_search_query_variants(query_text)
    ),
    scored as (
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
            chunks.chunk_index,
            chunks.metadata,
            (
                case
                    when pg_catalog.numnode(queries.strict_query) > 0
                     and chunks.search_vector @@ queries.strict_query
                    then 3 * pg_catalog.ts_rank_cd(
                        chunks.search_vector,
                        queries.strict_query
                    )
                    else 0
                end
                +
                case
                    when pg_catalog.numnode(queries.text_query) > 0
                     and chunks.search_vector @@ queries.text_query
                    then 2 * pg_catalog.ts_rank_cd(
                        chunks.search_vector,
                        queries.text_query
                    )
                    else 0
                end
                +
                case
                    when pg_catalog.numnode(queries.broad_query) > 0
                     and chunks.search_vector @@ queries.broad_query
                    then pg_catalog.ts_rank_cd(
                        chunks.search_vector,
                        queries.broad_query
                    )
                    else 0
                end
            )::double precision as lexical_score
        from public.knowledge_chunks as chunks
        join public.knowledge_documents as documents
          on documents.document_id = chunks.document_id
        cross join queries
        where documents.status = filter_status
          and (
              filter_document_types is null
              or chunks.document_type = any(filter_document_types)
          )
          and (
              (
                  pg_catalog.numnode(queries.strict_query) > 0
                  and chunks.search_vector @@ queries.strict_query
              )
              or (
                  pg_catalog.numnode(queries.text_query) > 0
                  and chunks.search_vector @@ queries.text_query
              )
              or (
                  pg_catalog.numnode(queries.broad_query) > 0
                  and chunks.search_vector @@ queries.broad_query
              )
          )
    )
    select
        scored.id,
        scored.document_id,
        scored.source_filename,
        scored.document_title,
        scored.document_type,
        scored.section_path,
        scored.heading,
        scored.content,
        scored.priority,
        scored.lexical_score,
        scored.metadata
    from scored
    where scored.lexical_score > 0
    order by
        scored.lexical_score desc,
        scored.priority asc,
        scored.chunk_index asc
    limit greatest(least(coalesce(match_count, 20), 100), 0);
$$;

-- The existing hybrid RPC calls match_knowledge_chunks_lexical and therefore
-- automatically consumes the improved lexical candidate ordering. Its RRF
-- formula remains position-based and does not add raw lexical scores.

revoke execute on function public.normalize_knowledge_text_terms(text)
from public, anon, authenticated;

revoke execute on function public.knowledge_search_query_variants(text)
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

grant execute on function public.normalize_knowledge_text_terms(text)
to service_role;

grant execute on function public.knowledge_search_query_variants(text)
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
