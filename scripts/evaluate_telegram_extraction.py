import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.bot.categories import DeterministicCategoryClassifier  # noqa: E402
from app.bot.normalization import NaturalDateParser  # noqa: E402
from app.bot.parser import DeterministicIntakeParser  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.extraction.intake import (  # noqa: E402
    TelegramIntakeExtractionService,
)
from app.extraction.normalization import evidence_supports_field  # noqa: E402
from app.extraction.provider import (  # noqa: E402
    FakeApprovalExtractionProvider,
    OpenAIApprovalExtractionProvider,
)
from app.intake.field_registry import RequestFieldRegistry  # noqa: E402
from app.intake.models import (  # noqa: E402
    IntakeFieldUpdate,
    NextQuestion,
    RequestDraftData,
)
from app.intake.service import RequestIntakeService  # noqa: E402

DEFAULT_CASES = (
    PROJECT_ROOT / "data" / "evaluation" / "telegram_intake_holdout.json"
)
REFERENCE_DATE = date(2026, 7, 29)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate Telegram intake extraction on a holdout set."
    )
    parser.add_argument(
        "--mode",
        choices=("rule", "openai", "hybrid", "fake"),
        default="rule",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    return parser


def evaluate_cases(
    cases: list[dict[str, Any]],
    *,
    mode: str,
    structured: TelegramIntakeExtractionService | None = None,
) -> tuple[dict[str, float | int], list[dict[str, Any]]]:
    dates = NaturalDateParser(today_provider=lambda: REFERENCE_DATE)
    parser = DeterministicIntakeParser(
        category_classifier=DeterministicCategoryClassifier(),
        date_parser=dates,
    )
    intake = RequestIntakeService()
    total_expected = correct_expected = 0
    total_null = correct_null = 0
    total_critical = correct_critical = 0
    type_total = type_correct = category_total = category_correct = 0
    amount_total = amount_correct = date_total = date_correct = 0
    quantity_total = quantity_correct = unit_total = unit_correct = 0
    quantity_null_total = quantity_hallucinated = 0
    unit_null_total = unit_hallucinated = 0
    date_null_total = date_hallucinated = 0
    budget_total = budget_correct = 0
    decision_total = decision_correct = unnecessary = missed_question = 0
    missing_total = missing_correct = 0
    question_valid_total = question_valid_correct = 0
    order_total = order_correct = 0
    provider_calls = provider_successes = provider_failures = 0
    evidence_failures = fallback_count = 0
    failures: list[dict[str, Any]] = []

    for case in cases:
        draft = RequestDraftData.model_validate(case.get("context", {}))
        question = _question(case.get("awaiting_field"))
        case_failure: dict[str, Any] | None = None
        try:
            deterministic = parser.parse(
                case["input"],
                question,
                case.get("awaiting_field"),
            )
        except Exception as exc:
            deterministic = IntakeFieldUpdate()
            case_failure = {
                "case_id": case["case_id"],
                "mode": mode,
                "stage": "deterministic_parser",
                "error": type(exc).__name__,
                "reason": "Deterministic parser could not process the case",
                "provider_failed": False,
                "fallback_used": False,
            }
        update = deterministic
        if mode in {"openai", "hybrid", "fake"}:
            if structured is None:
                raise RuntimeError("Structured extractor is not configured")
            resolution = structured.resolve_message(
                case["input"],
                draft,
                question,
                deterministic,
                source_kind=(
                    "clarification_answer" if question else "initial_description"
                ),
                merge_deterministic=mode == "hybrid",
                fallback_on_error=mode == "hybrid",
            )
            provider_calls += int(resolution.provider_called)
            provider_successes += int(resolution.provider_succeeded)
            provider_failures += int(resolution.provider_failed)
            fallback_count += int(resolution.fallback_used)
            update = resolution.update or IntakeFieldUpdate()
            if resolution.failure is not None:
                failure = resolution.failure
                evidence_failures += int(
                    failure.error_type == "ApprovalEvidenceValidationError"
                    or failure.diagnostic_code == "evidence_validation_failed"
                )
                case_failure = {
                    "case_id": case["case_id"],
                    "mode": mode,
                    "stage": "openai_provider",
                    "error": failure.error,
                    "reason": failure.reason,
                    "error_type": failure.error_type,
                    "error_code": failure.diagnostic_code,
                    "validation_error_codes": failure.validation_error_codes,
                    "provider_failed": True,
                    "fallback_used": resolution.fallback_used,
                }
        result = intake.process_step(draft, update)
        actual = result.draft.model_dump(mode="json")
        mismatches: list[str] = []
        mismatch_types: list[str] = []
        case_correct: dict[str, bool] = {}

        for field_name, expected in case["expected_fields"].items():
            matched = _equal(actual.get(field_name), expected)
            total_expected += 1
            correct_expected += int(matched)
            case_correct[field_name] = matched
            if not matched:
                mismatch_types.append(
                    "missing_field"
                    if actual.get(field_name) is None
                    else "semantic_error"
                )
                mismatches.append(
                    f"{field_name}: expected {expected!r}, got "
                    f"{actual.get(field_name)!r}"
                )
        for field_name, alternatives in case.get(
            "acceptable_text_fields", {}
        ).items():
            matched = _text_equal(actual.get(field_name), alternatives)
            total_expected += 1
            correct_expected += int(matched)
            case_correct[field_name] = matched
            if not matched:
                mismatch_types.append(
                    "missing_field"
                    if actual.get(field_name) is None
                    else "semantic_error"
                )
                mismatches.append(
                    f"{field_name}: expected one of {alternatives!r}, got "
                    f"{actual.get(field_name)!r}"
                )
        for field_name in case.get("expected_null_fields", []):
            matched = actual.get(field_name) is None
            total_null += 1
            correct_null += int(matched)
            case_correct[field_name] = matched
            if not matched:
                mismatch_types.append("hallucination")
                mismatches.append(
                    f"{field_name}: expected null, got {actual.get(field_name)!r}"
                )
            if field_name == "quantity":
                quantity_null_total += 1
                quantity_hallucinated += int(not matched)
            elif field_name == "unit":
                unit_null_total += 1
                unit_hallucinated += int(not matched)
            elif field_name == "desired_delivery_date":
                date_null_total += 1
                date_hallucinated += int(not matched)

        for field_name in case.get("critical_fields", []):
            total_critical += 1
            matched = case_correct.get(field_name, actual.get(field_name) is None)
            correct_critical += int(matched)
        type_total, type_correct = _field_counter(
            "procurement_type", case_correct, type_total, type_correct
        )
        category_total, category_correct = _field_counter(
            "category_code", case_correct, category_total, category_correct
        )
        amount_total, amount_correct = _field_counter(
            "amount", case_correct, amount_total, amount_correct
        )
        quantity_total, quantity_correct = _field_counter(
            "quantity", case_correct, quantity_total, quantity_correct
        )
        unit_total, unit_correct = _field_counter(
            "unit", case_correct, unit_total, unit_correct
        )
        date_total, date_correct = _field_counter(
            "desired_delivery_date", case_correct, date_total, date_correct
        )
        budget_total, budget_correct = _field_counter(
            "budget_status", case_correct, budget_total, budget_correct
        )

        expected_next = case.get("expected_next_question")
        actual_next = (
            result.next_question.field_code if result.next_question else "none"
        )
        expected_missing = set(case.get("expected_missing_fields", []))
        actual_missing = set(result.completeness.missing_fields)
        known_present = set(case["expected_fields"]) | set(
            case.get("acceptable_text_fields", {})
        )
        evaluate_missing = bool(expected_missing) or expected_next == "none"
        if evaluate_missing:
            missing_matched = expected_missing <= actual_missing and not (
                known_present & actual_missing
            )
            missing_total += 1
            missing_correct += int(missing_matched)
            if not missing_matched:
                mismatch_types.append("workflow_error")
                mismatches.append(
                    "missing_fields: expected required subset "
                    f"{sorted(expected_missing)!r}, got "
                    f"{sorted(actual_missing)!r}"
                )
        if expected_next is not None:
            decision_total += 1
            expected_has_question = expected_next != "none"
            actual_has_question = actual_next != "none"
            decision_matched = actual_has_question == expected_has_question
            decision_correct += int(decision_matched)

            order_total += 1
            order_matched = actual_next == expected_next
            order_correct += int(order_matched)

            valid_candidates = actual_missing | set(
                result.completeness.invalid_fields
            )
            question_valid = (
                actual_next in valid_candidates
                if actual_has_question
                else not valid_candidates
            )
            question_valid_total += 1
            question_valid_correct += int(question_valid)
            unnecessary += int(actual_has_question and not question_valid)
            missed_question += int(
                not actual_has_question and bool(valid_candidates)
            )
            if not decision_matched:
                mismatch_types.append("workflow_error")
                mismatches.append(
                    "next_question decision: expected "
                    f"{'a question' if expected_has_question else 'no question'}, "
                    f"got {actual_next!r}"
                )
            elif not question_valid:
                mismatch_types.append("workflow_error")
                mismatches.append(
                    f"next_question: {actual_next!r} is not an unresolved field"
                )
            elif not order_matched:
                mismatch_types.append("question_order_difference")
                mismatches.append(
                    f"next_question order: expected {expected_next!r}, got "
                    f"{actual_next!r}"
                )
        if case_failure is not None:
            if mismatches:
                case_failure["mismatches"] = mismatches
                case_failure["mismatch_types"] = sorted(set(mismatch_types))
            failures.append(case_failure)
        elif mismatches:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "mismatches": mismatches,
                    "mismatch_types": sorted(set(mismatch_types)),
                }
            )

    metrics: dict[str, float | int] = {
        "cases": len(cases),
        "provider_call_count": provider_calls,
        "provider_success_count": provider_successes,
        "provider_failure_count": provider_failures,
        "evidence_validation_failure_count": evidence_failures,
        "fallback_count": fallback_count,
        "fallback_rate": _ratio(fallback_count, provider_calls),
        "procurement_type_accuracy": _ratio(type_correct, type_total),
        "category_accuracy": _ratio(category_correct, category_total),
        "field_level_precision": _ratio(
            correct_expected + correct_null,
            total_expected + total_null,
        ),
        "field_level_recall": _ratio(correct_expected, total_expected),
        "critical_field_exact_match": _ratio(correct_critical, total_critical),
        "amount_exact_match": _ratio(amount_correct, amount_total),
        "quantity_exact_match": _ratio(quantity_correct, quantity_total),
        "unit_exact_match": _ratio(unit_correct, unit_total),
        "date_exact_match": _ratio(date_correct, date_total),
        "budget_status_accuracy": _ratio(budget_correct, budget_total),
        "completeness_decision_accuracy": _ratio(
            decision_correct, decision_total
        ),
        "missing_fields_correctness": _ratio(missing_correct, missing_total),
        "next_question_validity_accuracy": _ratio(
            question_valid_correct, question_valid_total
        ),
        "next_question_order_exact_match": _ratio(
            order_correct, order_total
        ),
        "unnecessary_question_rate": _ratio(unnecessary, decision_total),
        "missed_question_rate": _ratio(missed_question, decision_total),
        "missed_field_rate": _ratio(
            total_expected - correct_expected, total_expected
        ),
        "hallucinated_field_rate": _ratio(
            total_null - correct_null, total_null
        ),
        "hallucinated_quantity_rate": _ratio(
            quantity_hallucinated, quantity_null_total
        ),
        "hallucinated_unit_rate": _ratio(unit_hallucinated, unit_null_total),
        "hallucinated_date_rate": _ratio(date_hallucinated, date_null_total),
    }
    return metrics, failures


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if len(cases) < 30:
        print("ERROR: Telegram holdout must contain at least 30 cases")
        return 2
    structured = (
        _build_structured(args.mode)
        if args.mode in {"openai", "hybrid", "fake"}
        else None
    )
    metrics, failures = evaluate_cases(cases, mode=args.mode, structured=structured)
    _print_report(
        args.mode,
        metrics,
        failures,
        show_failures=args.show_failures,
        json_output=args.json_output,
    )
    return 0


def _print_report(
    mode: str,
    metrics: dict[str, float | int],
    failures: list[dict[str, Any]],
    *,
    show_failures: bool,
    json_output: bool,
) -> None:
    if json_output:
        print(
            json.dumps(
                {"mode": mode, "metrics": metrics, "failures": failures},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"mode: {mode}")
        for name, value in metrics.items():
            formatted = f"{value:.3f}" if isinstance(value, float) else value
            print(f"{name}: {formatted}")
        if show_failures:
            for failure in failures:
                print(json.dumps(failure, ensure_ascii=False))


def _build_structured(mode: str) -> TelegramIntakeExtractionService:
    settings = get_settings()
    if mode == "fake":
        return TelegramIntakeExtractionService(FakeApprovalExtractionProvider())
    if not settings.openai_configured:
        raise RuntimeError("OPENAI_API_KEY is required for openai/hybrid evaluation")
    return TelegramIntakeExtractionService(
        OpenAIApprovalExtractionProvider(
            api_key=settings.openai_api_key,
            model=settings.approval_extraction_model,
            timeout_seconds=settings.approval_extraction_timeout_seconds,
            max_retries=settings.approval_extraction_max_retries,
        ),
        date_parser=NaturalDateParser(today_provider=lambda: REFERENCE_DATE),
        min_confidence=settings.approval_extraction_min_confidence,
    )


def _question(field_code: str | None) -> NextQuestion | None:
    if field_code is None:
        return None
    definition = RequestFieldRegistry().get(field_code)
    if definition is None:
        raise ValueError(f"Unknown awaiting field: {field_code}")
    return NextQuestion(
        field_code=field_code,
        text=definition.question,
        question_type=definition.question_type,
        options=list(definition.options),
        reason="holdout context",
        priority=definition.priority,
    )


def _equal(actual: Any, expected: Any) -> bool:
    if actual is None:
        return expected is None
    if isinstance(actual, str):
        if _looks_decimal(expected):
            try:
                return Decimal(actual) == Decimal(str(expected))
            except ValueError:
                pass
        return actual.casefold().strip() == str(expected).casefold().strip()
    if isinstance(actual, (int, float, Decimal)) and _looks_decimal(expected):
        return Decimal(str(actual)) == Decimal(str(expected))
    return actual == expected


def _text_equal(actual: Any, alternatives: list[str]) -> bool:
    if not isinstance(actual, str):
        return False
    normalized = " ".join(actual.casefold().replace("ё", "е").split())
    normalized_alternatives = {
        " ".join(value.casefold().replace("ё", "е").split())
        for value in alternatives
    }
    if normalized in normalized_alternatives:
        return True
    return any(
        evidence_supports_field("item_name", alternative, actual)
        and evidence_supports_field("item_name", actual, alternative)
        for alternative in alternatives
    )


def _looks_decimal(value: Any) -> bool:
    try:
        Decimal(str(value))
    except Exception:
        return False
    return True


def _field_counter(
    field_name: str,
    results: dict[str, bool],
    total: int,
    correct: int,
) -> tuple[int, int]:
    if field_name not in results:
        return total, correct
    return total + 1, correct + int(results[field_name])


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
