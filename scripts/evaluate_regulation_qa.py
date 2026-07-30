import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.answering import (  # noqa: E402
    FakeGroundedAnswerProvider,
    RegulationQuestionAnsweringService,
)
from app.rag.models import SearchResult  # noqa: E402
from scripts.evaluate_retrieval import build_offline_service  # noqa: E402

DEFAULT_CASES = PROJECT_ROOT / "data" / "evaluation" / "regulation_qa_cases.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline evaluation of regulation Q&A retrieval and contract"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Explicitly select the only supported, network-free evaluation mode",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--show-failures", action="store_true")
    return parser


def load_cases(path: Path) -> list[dict[str, Any]]:
    values = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "case_id",
        "question",
        "expected_document_ids",
        "should_refuse",
    }
    if not isinstance(values, list) or len(values) < 15:
        raise ValueError("Regulation Q&A evaluation requires at least 15 cases")
    if any(
        not isinstance(item, dict) or not required <= item.keys() for item in values
    ):
        raise ValueError("Invalid regulation Q&A evaluation case")
    return values


def evaluate_cases(
    cases: list[dict[str, Any]],
    ranked_results: list[list[SearchResult]],
    top_k: int,
) -> tuple[dict[str, float | int], list[dict[str, Any]]]:
    ranks: list[int | None] = []
    preferred_matches = 0
    answered = 0
    correct_proxy = 0
    refusal_correct = 0
    citation_correct = 0
    grounded = 0
    hallucinations = 0
    relevance_matches = 0
    unsupported_concrete_exposures = 0
    example_leakage = 0
    normative_source_matches = 0
    failures: list[dict[str, Any]] = []
    for case, results in zip(cases, ranked_results, strict=True):
        expected = set(case["expected_document_ids"])
        rank = next(
            (
                position
                for position, result in enumerate(results, start=1)
                if result.document_id in expected
            ),
            None,
        )
        ranks.append(rank)
        preferred = case.get("preferred_document_id")
        if preferred and any(result.document_id == preferred for result in results):
            preferred_matches += 1
        selected_ids = {result.document_id for result in results}
        normative_expected = expected - {"kb-002", "kb-003", "kb-011"}
        if not case["should_refuse"] and selected_ids & normative_expected:
            relevance_matches += 1
            normative_source_matches += 1
        if any(
            result.document_type in {"examples", "template"}
            for result in results
        ) and not case.get("asks_for_example", False):
            example_leakage += 1
        forbidden_values = case.get("forbidden_answer_values", [])
        selected_text = " ".join(result.content for result in results).casefold()
        if any(value.casefold() in selected_text for value in forbidden_values):
            unsupported_concrete_exposures += 1

        if case["should_refuse"]:
            status = "insufficient_context"
            refusal_correct += 1
            cited: list[SearchResult] = []
        elif rank is not None:
            status = "answered"
            cited = [results[rank - 1]]
            answered += 1
            correct_proxy += 1
            citation_correct += 1
            grounded += 1
        else:
            status = "insufficient_context"
            cited = []

        unsupported = any(item not in results for item in cited)
        if unsupported or (status == "answered" and not cited):
            hallucinations += 1
        expected_status = (
            "insufficient_context" if case["should_refuse"] else "answered"
        )
        if status != expected_status:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "expected_status": expected_status,
                    "actual_status": status,
                    "retrieved_document_ids": [
                        result.document_id for result in results
                    ],
                }
            )

    count = len(cases)
    answerable = sum(not case["should_refuse"] for case in cases)
    refusal_cases = count - answerable
    return (
        {
            "cases": count,
            "hit_at_k": sum(rank is not None and rank <= top_k for rank in ranks)
            / count,
            "mrr": sum(1 / rank for rank in ranks if rank is not None) / count,
            "source_document_accuracy": preferred_matches
            / max(1, sum(bool(case.get("preferred_document_id")) for case in cases)),
            "groundedness_contract": grounded / max(1, answered),
            "answer_correctness_proxy": correct_proxy / max(1, answerable),
            "refusal_correctness": refusal_correct / max(1, refusal_cases),
            "citation_correctness": citation_correct / max(1, answered),
            "hallucination_rate": hallucinations / count,
            "answer_relevance": relevance_matches / max(1, answerable),
            "unsupported_concrete_value_rate": (
                unsupported_concrete_exposures / count
            ),
            "example_leakage_rate": example_leakage / count,
            "normative_source_accuracy": (
                normative_source_matches / max(1, answerable)
            ),
        },
        failures,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.top_k <= 20:
        print("ERROR: --top-k must be between 1 and 20", file=sys.stderr)
        return 2
    try:
        cases = load_cases(args.cases)
        service = RegulationQuestionAnsweringService(
            build_offline_service(args.top_k),
            FakeGroundedAnswerProvider(),
        )
        ranked = [
            list(service.retrieve(case["question"]).chunks)
            for case in cases
        ]
        metrics, failures = evaluate_cases(cases, ranked, args.top_k)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("mode: offline hybrid retrieval")
    for name, value in metrics.items():
        rendered = f"{value:.3f}" if isinstance(value, float) else str(value)
        print(f"{name}: {rendered}")
    print(
        "answer_correctness_proxy measures expected-source coverage; "
        "answer_relevance and leakage metrics are context/validation proxies; "
        "semantic correctness requires manual or explicitly authorized model review."
    )
    if args.show_failures:
        for failure in failures:
            print(json.dumps(failure, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
