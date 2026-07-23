import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.rag.embeddings import OpenAIEmbeddingProvider  # noqa: E402
from app.rag.exceptions import RagError  # noqa: E402
from app.rag.models import HybridRetrievalResult, LexicalRetrievalResult  # noqa: E402
from app.rag.repository import SupabaseKnowledgeRepository  # noqa: E402
from app.rag.retrieval_service import KnowledgeRetrievalService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Knowledge retrieval")
    parser.add_argument("query")
    parser.add_argument(
        "--mode",
        choices=("semantic", "lexical", "hybrid"),
        default=None,
    )
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--semantic-candidates", type=int)
    parser.add_argument("--lexical-candidates", type=int)
    parser.add_argument("--rrf-k", type=int)
    parser.add_argument("--semantic-weight", type=float)
    parser.add_argument("--lexical-weight", type=float)
    parser.add_argument(
        "--document-type",
        action="append",
        dest="document_types",
    )
    parser.add_argument("--debug-scores", action="store_true")
    parser.add_argument("--show-content", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    mode = args.mode or settings.rag_retrieval_mode
    try:
        if not settings.supabase_configured:
            raise RagError("Supabase is not configured")
        assert settings.supabase_url is not None
        assert settings.supabase_service_role_key is not None
        repository = SupabaseKnowledgeRepository.from_credentials(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
        provider = None
        if mode != "lexical":
            provider = OpenAIEmbeddingProvider(
                api_key=settings.openai_api_key,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                batch_size=settings.embedding_batch_size,
            )
        service = KnowledgeRetrievalService(
            repository,
            provider,
            default_top_k=settings.rag_top_k,
            default_threshold=settings.rag_similarity_threshold,
            default_mode=settings.rag_retrieval_mode,
            default_semantic_candidate_count=(
                settings.rag_semantic_candidate_count
            ),
            default_lexical_candidate_count=(
                settings.rag_lexical_candidate_count
            ),
            default_rrf_k=settings.rag_rrf_k,
            default_semantic_weight=settings.rag_semantic_weight,
            default_lexical_weight=settings.rag_lexical_weight,
        )
        results = service.search(
            args.query,
            mode=mode,
            top_k=args.top_k,
            similarity_threshold=args.threshold,
            semantic_candidate_count=args.semantic_candidates,
            lexical_candidate_count=args.lexical_candidates,
            rrf_k=args.rrf_k,
            semantic_weight=args.semantic_weight,
            lexical_weight=args.lexical_weight,
            document_types=args.document_types,
        )
    except RagError as exc:
        print(f"Search failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in results],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    for position, result in enumerate(results, start=1):
        preview = " ".join(result.content.split())
        if not args.show_content:
            preview = preview[:240] + ("…" if len(preview) > 240 else "")
        print(f"{position}. mode={mode} {_primary_score(result)}")
        print(f"   document: {result.document_title}")
        print(f"   section: {result.section_path}")
        print(f"   source: {result.source_filename}")
        if args.debug_scores:
            _print_debug_scores(result)
        print(f"   content: {result.content if args.show_content else preview}")
    return 0


def _primary_score(result) -> str:
    if isinstance(result, HybridRetrievalResult):
        return f"hybrid_score={result.hybrid_score:.6f}"
    if isinstance(result, LexicalRetrievalResult):
        return f"lexical_score={result.lexical_score:.6f}"
    return f"similarity={result.similarity:.6f}"


def _print_debug_scores(result) -> None:
    if isinstance(result, HybridRetrievalResult):
        print(f"   similarity: {result.similarity}")
        print(f"   lexical_score: {result.lexical_score}")
        print(f"   semantic_rank: {result.semantic_rank}")
        print(f"   lexical_rank: {result.lexical_rank}")
        print(f"   hybrid_score: {result.hybrid_score}")
    elif isinstance(result, LexicalRetrievalResult):
        print(f"   lexical_score: {result.lexical_score}")
    else:
        print(f"   similarity: {result.similarity}")


if __name__ == "__main__":
    raise SystemExit(main())
