import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.rag.embeddings import (  # noqa: E402
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from app.rag.exceptions import RagError  # noqa: E402
from app.rag.indexing_service import load_processed_knowledge_data  # noqa: E402
from app.rag.models import EmbeddingItem, RetrievalMode, SearchResult  # noqa: E402
from app.rag.repository import (  # noqa: E402
    InMemoryKnowledgeRepository,
    SupabaseKnowledgeRepository,
)
from app.rag.retrieval_service import KnowledgeRetrievalService  # noqa: E402

DEFAULT_CASES = PROJECT_ROOT / "data" / "evaluation" / "retrieval_cases.json"
DOCUMENTS = PROJECT_ROOT / "data" / "processed" / "knowledge_documents.json"
CHUNKS = PROJECT_ROOT / "data" / "processed" / "knowledge_chunks.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate knowledge retrieval")
    parser.add_argument(
        "--mode",
        choices=("semantic", "lexical", "hybrid", "all"),
        default="all",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--offline", action="store_true")
    return parser


def load_cases(path: Path) -> list[dict[str, Any]]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RagError(f"Cannot read evaluation cases: {path}") from exc
    if not isinstance(values, list) or not values:
        raise RagError("Evaluation cases must be a non-empty JSON array")
    required = {"case_id", "query", "expected_document_ids"}
    for value in values:
        if not isinstance(value, dict) or not required <= value.keys():
            raise RagError("Evaluation case has an invalid structure")
    return values


def calculate_metrics(
    cases: list[dict[str, Any]],
    ranked_results: list[list[SearchResult]],
    latencies_ms: list[float] | None = None,
) -> dict[str, float | int]:
    if len(cases) != len(ranked_results):
        raise ValueError("Cases and result lists must have equal length")
    ranks = [
        _first_matching_rank(case, results)
        for case, results in zip(cases, ranked_results, strict=True)
    ]
    preferred_ranks = [
        _first_matching_rank(case, results, preferred=True)
        for case, results in zip(cases, ranked_results, strict=True)
    ]
    count = len(cases)
    metrics: dict[str, float | int] = {
        "cases": count,
        "hit_at_1": _hit_rate(ranks, 1),
        "hit_at_3": _hit_rate(ranks, 3),
        "hit_at_5": _hit_rate(ranks, 5),
        "mrr": (
            sum(1 / rank for rank in ranks if rank is not None) / count
            if count
            else 0.0
        ),
        "preferred_hit_at_1": _hit_rate(preferred_ranks, 1),
        "preferred_hit_at_3": _hit_rate(preferred_ranks, 3),
        "preferred_hit_at_5": _hit_rate(preferred_ranks, 5),
    }
    if latencies_ms:
        metrics["average_latency_ms"] = sum(latencies_ms) / len(latencies_ms)
    return metrics


def evaluate_mode(
    service: KnowledgeRetrievalService,
    cases: list[dict[str, Any]],
    mode: RetrievalMode,
    top_k: int,
) -> tuple[dict[str, float | int], list[dict[str, Any]]]:
    ranked_results: list[list[SearchResult]] = []
    latencies: list[float] = []
    failures: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        results = service.search(case["query"], mode=mode, top_k=top_k)
        latencies.append((time.perf_counter() - started) * 1000)
        ranked_results.append(results)
        if _first_matching_rank(case, results) is None:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "query": case["query"],
                    "expected_document_ids": case["expected_document_ids"],
                    "expected_section_contains": case.get(
                        "expected_section_contains"
                    ),
                    "actual": [
                        {
                            "rank": rank,
                            "document_id": result.document_id,
                            "section_path": result.section_path,
                            "scores": _scores(result),
                        }
                        for rank, result in enumerate(results, start=1)
                    ],
                }
            )
    return calculate_metrics(cases, ranked_results, latencies), failures


def build_offline_service() -> KnowledgeRetrievalService:
    documents, chunks = load_processed_knowledge_data(DOCUMENTS, CHUNKS)
    repository = InMemoryKnowledgeRepository()
    provider = FakeEmbeddingProvider(dimensions=32, model="fake-evaluation")
    repository.upsert_documents(documents)
    repository.upsert_chunks(chunks)
    vectors = provider.embed_texts([chunk.content for chunk in chunks])
    repository.update_chunk_embeddings(
        [
            EmbeddingItem(
                chunk_id=UUID(str(chunk.chunk_id)),
                embedding=vector,
                embedding_model=provider.model,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
    )
    return KnowledgeRetrievalService(
        repository,
        provider,
        default_mode="hybrid",
        default_threshold=-1.0,
    )


def build_real_service(mode: str) -> KnowledgeRetrievalService:
    settings = get_settings()
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
    return KnowledgeRetrievalService(
        repository,
        provider,
        default_top_k=settings.rag_top_k,
        default_threshold=settings.rag_similarity_threshold,
        default_mode=settings.rag_retrieval_mode,
        default_semantic_candidate_count=settings.rag_semantic_candidate_count,
        default_lexical_candidate_count=settings.rag_lexical_candidate_count,
        default_rrf_k=settings.rag_rrf_k,
        default_semantic_weight=settings.rag_semantic_weight,
        default_lexical_weight=settings.rag_lexical_weight,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.top_k <= 20:
        print("--top-k must be between 1 and 20", file=sys.stderr)
        return 2
    try:
        cases = load_cases(args.cases)
        service = (
            build_offline_service()
            if args.offline
            else build_real_service(args.mode)
        )
        modes: list[RetrievalMode] = (
            ["semantic", "lexical", "hybrid"]
            if args.mode == "all"
            else [args.mode]
        )
        report: dict[str, Any] = {
            "offline": args.offline,
            "top_k": args.top_k,
            "modes": {},
        }
        for mode in modes:
            metrics, failures = evaluate_mode(
                service,
                cases,
                mode,
                args.top_k,
            )
            report["modes"][mode] = {
                "metrics": metrics,
                "failures": failures if args.show_failures else [],
            }
    except RagError as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1

    _print_table(report)
    if args.show_failures:
        _print_failures(report)
    if args.json_output:
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.offline:
        print("Offline metrics validate architecture, not production quality.")
    return 0


def _first_matching_rank(
    case: dict[str, Any],
    results: list[SearchResult],
    *,
    preferred: bool = False,
) -> int | None:
    allowed = (
        {case["preferred_document_id"]}
        if preferred and case.get("preferred_document_id")
        else set(case["expected_document_ids"])
    )
    sections = case.get("expected_section_contains") or []
    for rank, result in enumerate(results, start=1):
        section_matches = not sections or any(
            expected.casefold() in result.section_path.casefold()
            for expected in sections
        )
        if result.document_id in allowed and section_matches:
            return rank
    return None


def _hit_rate(ranks: list[int | None], cutoff: int) -> float:
    return (
        sum(rank is not None and rank <= cutoff for rank in ranks) / len(ranks)
        if ranks
        else 0.0
    )


def _scores(result: SearchResult) -> dict[str, float | int | None]:
    return {
        name: getattr(result, name, None)
        for name in (
            "similarity",
            "lexical_score",
            "semantic_rank",
            "lexical_rank",
            "hybrid_score",
        )
    }


def _print_table(report: dict[str, Any]) -> None:
    print(
        "mode      cases  Hit@1  Hit@3  Hit@5  MRR    "
        "Pref@1 Pref@3 Pref@5 latency_ms"
    )
    for mode, values in report["modes"].items():
        metrics = values["metrics"]
        print(
            f"{mode:<9} {metrics['cases']:>5}  "
            f"{metrics['hit_at_1']:.3f}  {metrics['hit_at_3']:.3f}  "
            f"{metrics['hit_at_5']:.3f}  {metrics['mrr']:.3f}  "
            f"{metrics['preferred_hit_at_1']:.3f}  "
            f"{metrics['preferred_hit_at_3']:.3f}  "
            f"{metrics['preferred_hit_at_5']:.3f}  "
            f"{metrics.get('average_latency_ms', 0):.2f}"
        )


def _print_failures(report: dict[str, Any]) -> None:
    for mode, values in report["modes"].items():
        for failure in values["failures"]:
            print(f"\n[{mode}] {failure['case_id']}: {failure['query']}")
            print(
                "  expected: "
                f"{failure['expected_document_ids']} / "
                f"{failure['expected_section_contains']}"
            )
            for actual in failure["actual"]:
                print(
                    f"  {actual['rank']}. {actual['document_id']} | "
                    f"{actual['section_path']} | {actual['scores']}"
                )


if __name__ == "__main__":
    raise SystemExit(main())
