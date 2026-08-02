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
    clarifying_question_for,
)
from app.rag.models import SearchResult  # noqa: E402
from app.rag.regulation_queries import build_regulation_query_plan  # noqa: E402
from app.rag.value_normalization import normalize_regulation_text  # noqa: E402
from scripts.evaluate_retrieval import build_offline_service  # noqa: E402

DEFAULT_CASES = PROJECT_ROOT / "data" / "evaluation" / "regulation_qa_cases.json"
PRODUCTION_CASES = (
    PROJECT_ROOT / "data" / "evaluation" / "regulation_qa_production_cases.json"
)


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
    }
    if not isinstance(values, list) or len(values) < 15:
        raise ValueError("Regulation Q&A evaluation requires at least 15 cases")
    if any(
        not isinstance(item, dict)
        or not required <= item.keys()
        or not ({"should_refuse", "expected_status"} & item.keys())
        for item in values
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
    source_accuracy_cases = 0
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
    end_to_end_successes = 0
    false_refusals = 0
    clarification_matches = 0
    clarification_cases = 0
    general_policy_matches = 0
    general_policy_cases = 0
    word_number_matches = 0
    word_number_cases = 0
    multi_intent_matches = 0
    multi_intent_cases = 0
    outside_refusal_matches = 0
    outside_cases = 0
    failures: list[dict[str, Any]] = []
    for case, results in zip(cases, ranked_results, strict=True):
        enriched = "expected_status" in case
        should_refuse = bool(
            case.get(
                "should_refuse",
                case.get("expected_status") == "insufficient_context",
            )
        )
        expected_status = case.get("expected_status") or (
            "insufficient_context" if should_refuse else "answered"
        )
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
        selected_ids = {result.document_id for result in results}
        preferred = case.get("preferred_document_id")
        if preferred:
            source_accuracy_cases += 1
            if any(result.document_id == preferred for result in results):
                preferred_matches += 1
        elif enriched and expected:
            source_accuracy_cases += 1
            if selected_ids & expected:
                preferred_matches += 1
        normative_expected = expected - {"kb-002", "kb-003", "kb-011"}
        if (
            expected_status != "insufficient_context"
            and selected_ids & normative_expected
        ):
            relevance_matches += 1
            normative_source_matches += 1
        forbidden_values = case.get("forbidden_answer_values", [])
        selected_text = " ".join(result.content for result in results).casefold()
        if any(value.casefold() in selected_text for value in forbidden_values):
            unsupported_concrete_exposures += 1

        plan = build_regulation_query_plan(case["question"])
        if enriched and plan.understanding.primary_intent == "general_help":
            status = "clarification_required"
            cited = []
        elif enriched and clarifying_question_for(plan) is not None:
            status = "clarification_required"
            cited = []
        elif enriched and (
            plan.intent == "outside_kb"
            or not results
            or (bool(expected) and not selected_ids & expected)
        ):
            status = "insufficient_context"
            cited = []
            if plan.intent == "outside_kb":
                refusal_correct += 1
        elif enriched:
            status = "answered"
            cited = [result for result in results if result.document_id in expected]
            answered += 1
            if cited:
                correct_proxy += 1
                citation_correct += 1
                grounded += 1
        elif should_refuse:
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

        if any(
            result.document_type in {"examples", "template"}
            for result in cited
        ) and not case.get("asks_for_example", False):
            example_leakage += 1

        unsupported = any(item not in results for item in cited)
        if unsupported or (status == "answered" and not cited):
            hallucinations += 1
        if status == expected_status:
            end_to_end_successes += 1
        if expected_status == "answered" and status == "insufficient_context":
            false_refusals += 1
        expected_clarification = bool(case.get("clarification_required"))
        if expected_clarification:
            clarification_cases += 1
            if status == "clarification_required":
                clarification_matches += 1
        if case.get("category") == "general_policy":
            general_policy_cases += 1
            if status == expected_status and (not expected or selected_ids & expected):
                general_policy_matches += 1
        normalized_fragments = case.get("normalized_fragments", [])
        if normalized_fragments:
            word_number_cases += 1
            normalized_question = normalize_regulation_text(case["question"])
            if all(item in normalized_question for item in normalized_fragments):
                word_number_matches += 1
        expected_intents = set(case.get("expected_intents", []))
        if len(expected_intents) > 1:
            multi_intent_cases += 1
            if expected_intents <= set(plan.intents) and status == expected_status:
                multi_intent_matches += 1
        if expected_status == "insufficient_context":
            outside_cases += 1
            if status == "insufficient_context":
                outside_refusal_matches += 1
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
    expected_statuses = [
        case.get("expected_status")
        or (
            "insufficient_context"
            if case.get("should_refuse", False)
            else "answered"
        )
        for case in cases
    ]
    answerable = sum(status == "answered" for status in expected_statuses)
    refusal_cases = sum(
        status == "insufficient_context" for status in expected_statuses
    )
    in_scope_cases = count - refusal_cases
    return (
        {
            "cases": count,
            "hit_at_k": sum(rank is not None and rank <= top_k for rank in ranks)
            / count,
            "mrr": sum(1 / rank for rank in ranks if rank is not None) / count,
            "source_document_accuracy": preferred_matches
            / max(1, source_accuracy_cases),
            "groundedness_contract": grounded / max(1, answered),
            "answer_correctness_proxy": correct_proxy / max(1, answerable),
            "refusal_correctness": refusal_correct / max(1, refusal_cases),
            "citation_correctness": citation_correct / max(1, answered),
            "hallucination_rate": hallucinations / count,
            "answer_relevance": relevance_matches / max(1, in_scope_cases),
            "unsupported_concrete_value_rate": (
                unsupported_concrete_exposures / count
            ),
            "example_leakage_rate": example_leakage / count,
            "normative_source_accuracy": (
                normative_source_matches / max(1, in_scope_cases)
            ),
            "end_to_end_success_rate": end_to_end_successes / count,
            "false_refusal_rate": false_refusals / max(1, answerable),
            "clarification_accuracy": (
                clarification_matches / max(1, clarification_cases)
            ),
            "general_policy_question_accuracy": (
                general_policy_matches / max(1, general_policy_cases)
            ),
            "word_number_normalization_accuracy": (
                word_number_matches / max(1, word_number_cases)
            ),
            "multi_intent_answer_accuracy": (
                multi_intent_matches / max(1, multi_intent_cases)
            ),
            "outside_kb_refusal": outside_refusal_matches / max(1, outside_cases),
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
