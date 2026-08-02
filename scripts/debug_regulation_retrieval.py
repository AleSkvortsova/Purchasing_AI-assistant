import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai import OpenAI  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.rag.embeddings import OpenAIEmbeddingProvider  # noqa: E402
from app.rag.models import SearchResult  # noqa: E402
from app.rag.regulation_queries import (  # noqa: E402
    build_regulation_query_plan,
    fuse_regulation_results,
    matching_intents,
    select_relevant_regulation_chunks,
    source_kind,
)
from app.rag.repository import SupabaseKnowledgeRepository  # noqa: E402
from supabase import create_client  # noqa: E402

CHUNKS_PATH = PROJECT_ROOT / "data" / "processed" / "knowledge_chunks.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostics for regulation retrieval"
    )
    parser.add_argument("question", help="Regulation question to diagnose")
    return parser


def _chunk_indexes() -> dict[str, int]:
    values = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    return {str(item["chunk_id"]): int(item["chunk_index"]) for item in values}


def _result_row(
    result: SearchResult,
    *,
    rank: int,
    chunk_indexes: dict[str, int],
    threshold: float,
) -> dict[str, Any]:
    similarity = getattr(result, "similarity", None)
    return {
        "rank": rank,
        "document_id": result.document_id,
        "document_title": result.document_title,
        "document_type": result.document_type,
        "section_title": result.section_path,
        "chunk_index": chunk_indexes.get(str(result.chunk_id)),
        "semantic_score": similarity,
        "lexical_score": getattr(result, "lexical_score", None),
        "semantic_rank": getattr(result, "semantic_rank", None),
        "lexical_rank": getattr(result, "lexical_rank", None),
        "rrf_score": getattr(result, "hybrid_score", None),
        "multi_query_rrf_score": result.metadata.get(
            "regulation_query_rrf_score"
        ),
        "threshold": threshold,
        "threshold_decision": (
            similarity is None or similarity >= threshold
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if not settings.supabase_configured or not settings.openai_configured:
        print(
            "ERROR: Supabase and OpenAI configuration are required for "
            "read-only retrieval diagnostics",
            file=sys.stderr,
        )
        return 2
    client = create_client(
        str(settings.supabase_url),
        str(settings.supabase_service_role_key),
    )
    openai_client = OpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.rag_answer_timeout_seconds,
        max_retries=0,
    )
    embeddings = OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
        client=openai_client,
    )
    repository = SupabaseKnowledgeRepository(client)
    plan = build_regulation_query_plan(args.question)
    chunk_indexes = _chunk_indexes()
    variant_reports: list[dict[str, Any]] = []
    hybrid_lists: list[list[SearchResult]] = []
    semantic_candidates: list[SearchResult] = []
    lexical_candidate_count = 0
    for variant in plan.variants:
        query_embedding = embeddings.embed_query(variant)
        semantic = repository.semantic_search(
            query_embedding,
            settings.rag_semantic_candidate_count,
            -1.0,
        )
        lexical = repository.lexical_search(
            variant,
            settings.rag_lexical_candidate_count,
        )
        hybrid = repository.hybrid_search(
            variant,
            query_embedding,
            settings.rag_top_k,
            settings.rag_semantic_candidate_count,
            settings.rag_lexical_candidate_count,
            settings.rag_similarity_threshold,
            settings.rag_rrf_k,
            settings.rag_semantic_weight,
            settings.rag_lexical_weight,
        )
        hybrid_lists.append(hybrid)
        semantic_candidates.extend(semantic)
        lexical_candidate_count += len(lexical)
        variant_reports.append(
            {
                "query": variant,
                "semantic_candidates": [
                    _result_row(
                        item,
                        rank=rank,
                        chunk_indexes=chunk_indexes,
                        threshold=settings.rag_similarity_threshold,
                    )
                    for rank, item in enumerate(semantic, start=1)
                ],
                "lexical_candidates": [
                    _result_row(
                        item,
                        rank=rank,
                        chunk_indexes=chunk_indexes,
                        threshold=settings.rag_similarity_threshold,
                    )
                    for rank, item in enumerate(lexical, start=1)
                ],
                "hybrid_candidates": [
                    _result_row(
                        item,
                        rank=rank,
                        chunk_indexes=chunk_indexes,
                        threshold=settings.rag_similarity_threshold,
                    )
                    for rank, item in enumerate(hybrid, start=1)
                ],
            }
        )
    fused = fuse_regulation_results(hybrid_lists, rrf_k=settings.rag_rrf_k)
    selected = select_relevant_regulation_chunks(
        plan,
        fused,
        limit=settings.rag_top_k,
    )
    reason = None
    if not fused:
        below_threshold = bool(semantic_candidates) and all(
            (getattr(item, "similarity", None) or 0)
            < settings.rag_similarity_threshold
            for item in semantic_candidates
        )
        reason = (
            "below_threshold"
            if below_threshold and lexical_candidate_count == 0
            else "no_chunks"
        )
    elif not selected:
        reason = "no_relevant_normative_chunks"
    report = {
        "original_query": plan.original_query,
        "normalized_query": plan.normalized_query,
        "strict_query": plan.strict_query,
        "text_query": plan.text_query,
        "broad_query": plan.broad_query,
        "intent": plan.intent,
        "intents": list(plan.intents),
        "understanding": plan.understanding.model_dump(mode="json"),
        "variants": variant_reports,
        "final_candidates": [
            _result_row(
                item,
                rank=rank,
                chunk_indexes=chunk_indexes,
                threshold=settings.rag_similarity_threshold,
            )
            for rank, item in enumerate(fused, start=1)
        ],
        "relevance_decisions": [
            {
                **_result_row(
                    item,
                    rank=rank,
                    chunk_indexes=chunk_indexes,
                    threshold=settings.rag_similarity_threshold,
                ),
                "source_kind": source_kind(item.document_type),
                "matched_intents": list(matching_intents(plan, item)),
                "accepted": item in selected,
                "decision": (
                    "accepted"
                    if item in selected
                    else (
                        "example_or_template_excluded"
                        if source_kind(item.document_type) in {"example", "template"}
                        else "intent_or_context_limit"
                    )
                ),
            }
            for rank, item in enumerate(fused, start=1)
        ],
        "chunks_passed_to_answer_provider": [
            _result_row(
                item,
                rank=rank,
                chunk_indexes=chunk_indexes,
                threshold=settings.rag_similarity_threshold,
            )
            for rank, item in enumerate(selected, start=1)
        ],
        "insufficient_context_reason": reason,
        "read_only": True,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
