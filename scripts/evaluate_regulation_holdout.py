import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.answering import (  # noqa: E402
    FakeGroundedAnswerProvider,
    GroundedAnswerPayload,
    RegulationQuestionAnsweringService,
)
from app.rag.question_understanding import (  # noqa: E402
    understand_regulation_question,
)
from app.rag.regulation_queries import source_kind  # noqa: E402
from app.rag.value_normalization import normalize_regulation_text  # noqa: E402
from scripts.evaluate_retrieval import build_offline_service  # noqa: E402

DEFAULT_CASES = (
    PROJECT_ROOT / "data" / "evaluation" / "regulation_qa_holdout_cases.json"
)
KNOWLEDGE_DOCUMENTS = (
    PROJECT_ROOT / "data" / "processed" / "knowledge_documents.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline evaluation of typed regulation question understanding"
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--show-failures", action="store_true")
    return parser


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Holdout dataset must contain cases")
    return cases


def evaluate_cases(
    cases: list[dict[str, Any]],
) -> tuple[dict[str, float | int], list[dict[str, Any]]]:
    provider = FakeGroundedAnswerProvider(
        GroundedAnswerPayload(
            answer="",
            claims=[],
            insufficient_context=True,
            source_conflict=False,
        )
    )
    service = RegulationQuestionAnsweringService(build_offline_service(), provider)
    successes = 0
    user_answer_successes = 0
    understanding_successes = 0
    primary_intent_matches = 0
    secondary_intent_matches = 0
    slot_matches = 0
    false_refusals = 0
    false_answers = 0
    clarification_total = 0
    clarification_matches = 0
    source_total = 0
    exact_source_matches = 0
    source_claim_support_matches = 0
    normative_source_matches = 0
    relevant_answers = 0
    answered = 0
    outside_total = 0
    outside_matches = 0
    example_leaks = 0
    unsupported_values = 0
    validation_errors = 0
    safe_failures = 0
    non_answered_results = 0
    failures: list[dict[str, Any]] = []
    document_catalog = _load_document_catalog()
    for case in cases:
        understanding = understand_regulation_question(case["question"])
        result = service.answer(case["question"])
        expected_status = case["expected_status"]
        expected_intents = case["expected_intents"]
        primary_intent_ok = understanding.primary_intent == expected_intents[0]
        secondary_intent_ok = set(understanding.secondary_intents) == set(
            expected_intents[1:]
        )
        intent_ok = primary_intent_ok and secondary_intent_ok
        slots_ok = _slots_match(case["expected_slots"], understanding.model_dump())
        status_ok = result.status == expected_status
        answer = normalize_regulation_text(result.answer)
        claims_ok = all(
            normalize_regulation_text(claim) in answer
            for claim in case["required_claims"]
        )
        forbidden_ok = not any(
            normalize_regulation_text(claim) in answer
            for claim in case["forbidden_claims"]
        )
        actual_sources = {source.document_id for source in result.sources}
        expected_sources = set(case["expected_document_ids"])
        exact_source_ok = not expected_sources or bool(
            actual_sources & expected_sources
        )
        normative_source_ok = _normative_sources_ok(
            actual_sources,
            expected_sources,
            document_catalog,
        )
        source_claim_support_ok = _source_claim_support_ok(
            case,
            result.status,
            answer,
            actual_sources,
            document_catalog,
        )
        clarification_pattern = case.get("clarification_text_pattern")
        clarification_ok = not clarification_pattern or bool(
            result.clarifying_question
            and re.search(
                clarification_pattern,
                result.clarifying_question,
                re.IGNORECASE,
            )
        )
        outside_ok = not case["outside_kb"] or (
            result.status == "insufficient_context"
            and result.refusal_reason == "outside_kb"
        )
        user_answer_success = all(
            (
                status_ok,
                claims_ok,
                forbidden_ok,
                source_claim_support_ok,
                normative_source_ok,
                clarification_ok,
                outside_ok,
            )
        )
        understanding_success = all(
            (primary_intent_ok, secondary_intent_ok, slots_ok)
        )
        success = all(
            (
                user_answer_success,
                understanding_success,
            )
        )
        successes += int(success)
        user_answer_successes += int(user_answer_success)
        understanding_successes += int(understanding_success)
        primary_intent_matches += int(primary_intent_ok)
        secondary_intent_matches += int(secondary_intent_ok)
        slot_matches += int(slots_ok)
        if expected_status == "answered" and result.status != "answered":
            false_refusals += 1
        if expected_status != "answered" and result.status == "answered":
            false_answers += 1
        if expected_status == "clarification_required":
            clarification_total += 1
            clarification_matches += int(status_ok and clarification_ok)
        if expected_sources:
            source_total += 1
            exact_source_matches += int(exact_source_ok)
            source_claim_support_matches += int(source_claim_support_ok)
            normative_source_matches += int(normative_source_ok)
        if result.status == "answered":
            answered += 1
            relevant_answers += int(claims_ok)
        if case["outside_kb"]:
            outside_total += 1
            outside_matches += int(
                result.status == "insufficient_context"
                and result.refusal_reason == "outside_kb"
            )
        example_leaks += int(bool(actual_sources & {"kb-002", "kb-003", "kb-011"}))
        unsupported_values += int(
            result.diagnostics.get("validation_rule") == "unsupported_concrete_value"
        )
        validation_errors += int(
            result.refusal_reason in {"unsupported_answer", "malformed_output"}
        )
        if result.status != "answered":
            non_answered_results += 1
            safe_failures += int(
                result.refusal_reason
                not in {
                    "unsupported_answer",
                    "malformed_output",
                    "provider_unavailable",
                }
            )
        if not success:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "expected_status": expected_status,
                    "actual_status": result.status,
                    "intent": understanding.primary_intent,
                    "intents": list(understanding.intents),
                    "user_answer_success": user_answer_success,
                    "understanding_structure_ok": understanding_success,
                    "primary_intent_ok": primary_intent_ok,
                    "secondary_intent_ok": secondary_intent_ok,
                    "intent_ok": intent_ok,
                    "slots_ok": slots_ok,
                    "claims_ok": claims_ok,
                    "exact_source_match": exact_source_ok,
                    "source_claim_support_ok": source_claim_support_ok,
                    "normative_source_ok": normative_source_ok,
                    "clarification_ok": clarification_ok,
                    "reason_code": result.refusal_reason,
                }
            )
    count = len(cases)
    answerable = sum(case["expected_status"] == "answered" for case in cases)
    non_answerable = count - answerable
    return (
        {
            "cases": count,
            "end_to_end_success_rate": successes / count,
            "user_answer_success_rate": user_answer_successes / count,
            "understanding_structure_accuracy": understanding_successes / count,
            "primary_intent_accuracy": primary_intent_matches / count,
            "secondary_intent_accuracy": secondary_intent_matches / count,
            "slot_accuracy": slot_matches / count,
            "safe_failure_rate": safe_failures / max(1, non_answered_results),
            "false_refusal_rate": false_refusals / max(1, answerable),
            "false_answer_rate": false_answers / max(1, non_answerable),
            "clarification_accuracy": clarification_matches
            / max(1, clarification_total),
            "exact_source_match": exact_source_matches / max(1, source_total),
            "source_claim_support_accuracy": source_claim_support_matches
            / max(1, source_total),
            "normative_source_accuracy": normative_source_matches
            / max(1, source_total),
            # Backward-compatible diagnostic name. It intentionally keeps the
            # historical exact-ID meaning and no longer gates answer success.
            "source_document_accuracy": exact_source_matches
            / max(1, source_total),
            "answer_relevance": relevant_answers / max(1, answered),
            "outside_kb_refusal": outside_matches / max(1, outside_total),
            "example_leakage_rate": example_leaks / count,
            "unsupported_concrete_value_rate": unsupported_values / count,
            "validation_error_count": validation_errors,
        },
        failures,
    )


def _load_document_catalog() -> dict[str, dict[str, str]]:
    documents = json.loads(KNOWLEDGE_DOCUMENTS.read_text(encoding="utf-8"))
    return {
        item["document_id"]: {
            "document_type": item["document_type"],
            "content": normalize_regulation_text(item["raw_content"]),
        }
        for item in documents
    }


def _normative_sources_ok(
    actual_sources: set[str],
    expected_sources: set[str],
    catalog: dict[str, dict[str, str]],
) -> bool:
    if not expected_sources:
        return True
    if not actual_sources:
        return False
    return all(
        document_id in catalog
        and source_kind(catalog[document_id]["document_type"])
        not in {"example", "template"}
        for document_id in actual_sources
    )


def _source_claim_support_ok(
    case: dict[str, Any],
    result_status: str,
    normalized_answer: str,
    actual_sources: set[str],
    catalog: dict[str, dict[str, str]],
) -> bool:
    if not case["expected_document_ids"]:
        return True
    if result_status != "answered" or not actual_sources:
        return False
    required_claims = [
        normalize_regulation_text(claim) for claim in case["required_claims"]
    ]
    if not all(claim in normalized_answer for claim in required_claims):
        return False
    combined = " ".join(
        catalog.get(document_id, {}).get("content", "")
        for document_id in actual_sources
    )
    intent = case["expected_intents"][0]
    relevance_patterns = {
        "approval_route": r"согласован|согласующ|матриц",
        "urgency_policy": r"сроч|приоритет|нормативн.*срок",
        "status_explanation": r"статус|на согласовании|переход",
        "request_cancellation": r"отмен|снять.*заяв|начала закупки",
        "required_fields": r"обязательн.*пол|укажите|заполн",
        "category_classification": r"категор|классифик|s0[1-9]",
        "brand_equivalent_policy": r"бренд|марк|эквивалент",
        "responsibility_policy": r"ответствен|внутренн.*заказчик",
        "draft_and_history": r"черновик|мои заявки|последн.*заяв",
    }
    pattern = relevance_patterns.get(intent)
    return pattern is None or bool(re.search(pattern, combined))


def _slots_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for name, value in expected.items():
        actual_value = actual.get(name)
        if isinstance(actual_value, Decimal):
            actual_value = str(actual_value)
        if actual_value != value:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    metrics, failures = evaluate_cases(load_cases(args.cases))
    for name, value in metrics.items():
        rendered = f"{value:.3f}" if isinstance(value, float) else str(value)
        print(f"{name}: {rendered}")
    if args.show_failures and failures:
        print(json.dumps(failures, ensure_ascii=False, indent=2))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
