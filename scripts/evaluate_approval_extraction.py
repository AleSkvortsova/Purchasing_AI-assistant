import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.extraction.provider import (  # noqa: E402
    OpenAIApprovalExtractionProvider,
    RuleBasedApprovalExtractionProvider,
)
from app.extraction.service import ApprovalContextExtractionService  # noqa: E402

DEFAULT_CASES = (
    PROJECT_ROOT / "data" / "evaluation" / "approval_extraction_cases.json"
)
BOOLEAN_FIELDS = (
    "single_supplier",
    "has_data_access",
    "work_on_site",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate structured approval extraction."
    )
    parser.add_argument(
        "--provider",
        choices=("rule-based", "openai"),
        default="rule-based",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    return parser


def build_service(
    provider_name: str,
    *,
    offline: bool,
) -> ApprovalContextExtractionService:
    settings = get_settings()
    if offline or provider_name == "rule-based":
        provider = RuleBasedApprovalExtractionProvider()
    else:
        provider = OpenAIApprovalExtractionProvider(
            api_key=settings.openai_api_key,
            model=settings.approval_extraction_model,
            timeout_seconds=settings.approval_extraction_timeout_seconds,
            max_retries=settings.approval_extraction_max_retries,
        )
    return ApprovalContextExtractionService(
        provider,
        min_confidence=settings.approval_extraction_min_confidence,
    )


def evaluate_cases(
    cases: list[dict[str, Any]],
    service: ApprovalContextExtractionService,
) -> tuple[dict[str, float | int], list[dict[str, Any]]]:
    field_scores = Counter()
    field_totals = Counter()
    boolean_counts = Counter()
    complete = 0
    clarification_correct = 0
    contradiction_correct = 0
    failures: list[dict[str, Any]] = []

    for case in cases:
        result = service.extract(case["text"])
        extraction = result.extraction
        mismatches: list[str] = []
        for field, expected in case["expected_fields"].items():
            actual = getattr(extraction, field)
            correct = _equal(field, actual, expected)
            field_totals[field] += 1
            field_scores[field] += int(correct)
            if field in BOOLEAN_FIELDS:
                _count_boolean(boolean_counts, bool(actual), bool(expected))
            if not correct:
                mismatches.append(
                    f"{field}: expected {expected!r}, got {actual!r}"
                )
        for field in case["expected_null_fields"]:
            actual = getattr(extraction, field)
            field_totals[field] += 1
            field_scores[field] += int(actual is None)
            if actual is not None:
                mismatches.append(
                    f"{field}: expected null, got {actual!r}"
                )

        expected_clarification = set(
            case["expected_clarification_fields"]
        )
        actual_clarification = set(extraction.missing_fields)
        clarification_match = expected_clarification.issubset(
            actual_clarification
        )
        clarification_correct += int(clarification_match)
        contradiction_match = (
            (case["expected_status"] == "conflict")
            == bool(extraction.contradictions)
        )
        contradiction_correct += int(contradiction_match)
        warnings_match = all(
            any(expected in actual for actual in result.warnings)
            for expected in case["expected_warnings"]
        )
        status_match = result.status == case["expected_status"]
        case_correct = (
            not mismatches
            and status_match
            and warnings_match
            and clarification_match
            and contradiction_match
        )
        complete += int(case_correct)
        if not case_correct:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "mismatches": mismatches,
                    "expected_status": case["expected_status"],
                    "actual_status": result.status,
                    "expected_clarification": sorted(
                        expected_clarification
                    ),
                    "actual_clarification": sorted(
                        actual_clarification
                    ),
                    "warnings": result.warnings,
                }
            )

    total = len(cases)
    metrics: dict[str, float | int] = {
        "cases": total,
        "exact_match_amount": _accuracy(
            field_scores,
            field_totals,
            "amount",
        ),
        "budget_status_accuracy": _accuracy(
            field_scores,
            field_totals,
            "budget_status",
        ),
        "urgency_accuracy": _accuracy(
            field_scores,
            field_totals,
            "urgency",
        ),
        "category_accuracy": _accuracy(
            field_scores,
            field_totals,
            "category_code",
        ),
        "boolean_f1": _boolean_f1(boolean_counts),
        "complete_context_accuracy": complete / total,
        "clarification_accuracy": clarification_correct / total,
        "contradiction_detection_accuracy": (
            contradiction_correct / total
        ),
    }
    return metrics, failures


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    service = build_service(args.provider, offline=args.offline)
    metrics, failures = evaluate_cases(cases, service)
    payload = {"metrics": metrics, "failures": failures}
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, value in metrics.items():
            formatted = f"{value:.3f}" if isinstance(value, float) else value
            print(f"{name}: {formatted}")
        if args.show_failures:
            for failure in failures:
                print(json.dumps(failure, ensure_ascii=False))
    return 0 if not failures else 1


def _equal(field: str, actual: Any, expected: Any) -> bool:
    if field == "amount":
        return actual == Decimal(str(expected))
    return actual == expected


def _count_boolean(
    counts: Counter,
    actual: bool,
    expected: bool,
) -> None:
    if actual and expected:
        counts["tp"] += 1
    elif actual and not expected:
        counts["fp"] += 1
    elif not actual and expected:
        counts["fn"] += 1


def _boolean_f1(counts: Counter) -> float:
    denominator = 2 * counts["tp"] + counts["fp"] + counts["fn"]
    return (2 * counts["tp"] / denominator) if denominator else 1.0


def _accuracy(
    scores: Counter,
    totals: Counter,
    field: str,
) -> float:
    return scores[field] / totals[field] if totals[field] else 1.0


if __name__ == "__main__":
    raise SystemExit(main())
