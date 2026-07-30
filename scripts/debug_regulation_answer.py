import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai import OpenAI  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.rag.answering import (  # noqa: E402
    OpenAIGroundedAnswerProvider,
    RegulationQuestionAnsweringService,
    _concrete_values,
)
from app.rag.embeddings import OpenAIEmbeddingProvider  # noqa: E402
from app.rag.regulation_queries import build_regulation_query_plan  # noqa: E402
from app.rag.repository import SupabaseKnowledgeRepository  # noqa: E402
from app.rag.retrieval_service import KnowledgeRetrievalService  # noqa: E402
from supabase import create_client  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only local diagnostics for regulation answer validation"
    )
    parser.add_argument("question", help="Regulation question to diagnose")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if not (
        settings.supabase_configured
        and settings.openai_configured
        and settings.rag_answer_model
    ):
        print(
            "ERROR: Supabase, OpenAI, and RAG answer configuration are required",
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
    retrieval = KnowledgeRetrievalService(
        SupabaseKnowledgeRepository(client),
        OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
            client=openai_client,
        ),
        default_top_k=settings.rag_top_k,
        default_threshold=settings.rag_similarity_threshold,
        default_mode=settings.rag_retrieval_mode,
        default_semantic_candidate_count=settings.rag_semantic_candidate_count,
        default_lexical_candidate_count=settings.rag_lexical_candidate_count,
        default_rrf_k=settings.rag_rrf_k,
        default_semantic_weight=settings.rag_semantic_weight,
        default_lexical_weight=settings.rag_lexical_weight,
    )
    provider = OpenAIGroundedAnswerProvider(
        api_key=settings.openai_api_key,
        model=settings.rag_answer_model,
        timeout_seconds=settings.rag_answer_timeout_seconds,
        client=openai_client,
    )
    service = RegulationQuestionAnsweringService(retrieval, provider)
    plan = build_regulation_query_plan(args.question)
    outcome = service.retrieve(plan)
    payload = provider.generate(args.question, outcome.chunks)
    report = {
        "read_only": True,
        "question": args.question,
        "query_variants": list(plan.variants),
        "chunks": [
            {
                "chunk_id": str(chunk.chunk_id),
                "document_id": chunk.document_id,
                "document_type": chunk.document_type,
                "section": chunk.section_path,
            }
            for chunk in outcome.chunks
        ],
        "structured_output_summary": {
            "claim_count": len(payload.claims),
            "insufficient_context": payload.insufficient_context,
            "source_conflict": payload.source_conflict,
            "cited_chunk_ids": sorted(
                {
                    chunk_id
                    for claim in payload.claims
                    for chunk_id in claim.cited_chunk_ids
                }
            ),
        },
        "question_concrete_values": sorted(_concrete_values(args.question)),
        "claim_concrete_values": {
            str(index): sorted(_concrete_values(claim.text))
            for index, claim in enumerate(payload.claims, start=1)
        },
        "cited_normative_concrete_values": {
            str(chunk.chunk_id): sorted(_concrete_values(chunk.content))
            for chunk in outcome.chunks
            if chunk.document_type not in {"examples", "template"}
        },
        "validation_rule": None,
        "result": None,
    }
    try:
        result = service._validate(
            payload,
            outcome.chunks,
            plan,
            time.perf_counter(),
        )
    except ValueError as exc:
        report["validation_rule"] = str(exc).split(":", maxsplit=1)[0]
    else:
        report["result"] = result.model_dump(mode="json")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
