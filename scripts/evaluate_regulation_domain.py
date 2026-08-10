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
    GroundedAnswerPayload,
    RegulationQuestionAnsweringService,
)
from app.rag.question_understanding import (  # noqa: E402
    understand_regulation_question,
)
from app.rag.value_normalization import normalize_regulation_text  # noqa: E402
from scripts.evaluate_retrieval import build_offline_service  # noqa: E402

DEFAULT_CASES = PROJECT_ROOT / "data" / "evaluation" / "regulation_domain_cases.json"


class CountingRetrieval:
    def __init__(self) -> None:
        self._delegate = build_offline_service()
        self.default_top_k = self._delegate.default_top_k
        self.default_rrf_k = self._delegate.default_rrf_k
        self.calls: list[str] = []

    def search(self, query: str):
        self.calls.append(query)
        return self._delegate.search(query)


def evaluate_cases(
    cases: list[dict[str, Any]],
) -> tuple[dict[str, float | int], list[dict[str, Any]]]:
    counters = {
        "domain": 0,
        "ood": 0,
        "primary": 0,
        "relevance": 0,
        "source": 0,
        "unsupported": 0,
        "leakage": 0,
    }
    answered = 0
    ood_total = 0
    failures: list[dict[str, Any]] = []
    for case in cases:
        retrieval = CountingRetrieval()
        provider = FakeGroundedAnswerProvider(
            GroundedAnswerPayload(
                answer="",
                claims=[],
                insufficient_context=True,
                source_conflict=False,
            )
        )
        service = RegulationQuestionAnsweringService(retrieval, provider)
        understanding = understand_regulation_question(case["question"])
        result = service.answer(case["question"])
        normalized_answer = normalize_regulation_text(result.answer)
        domain_ok = understanding.domain_decision == case["expected_domain"]
        primary_ok = (
            understanding.primary_intent == case["expected_primary_intent"]
        )
        retrieval_ok = bool(retrieval.calls) is case["expected_retrieval"]
        status_ok = result.status == case["expected_status"]
        relevance_ok = all(
            normalize_regulation_text(term) in normalized_answer
            for term in case["required_answer_terms"]
        )
        source_ok = result.status != "answered" or bool(result.sources)
        unsupported_ok = result.diagnostics.get("validation_rule") != (
            "unsupported_concrete_value"
        )
        leakage_ok = not any(
            source.document_id in {"kb-002", "kb-003", "kb-011"}
            for source in result.sources
        )
        is_ood = case["expected_domain"] == "outside_domain"
        ood_ok = not is_ood or (
            result.refusal_reason == "outside_domain"
            and not retrieval.calls
            and not result.sources
        )
        counters["domain"] += int(domain_ok)
        counters["primary"] += int(primary_ok)
        counters["relevance"] += int(relevance_ok)
        if result.status == "answered":
            counters["source"] += int(source_ok)
        counters["unsupported"] += int(unsupported_ok)
        counters["leakage"] += int(leakage_ok)
        answered += int(result.status == "answered")
        if is_ood:
            ood_total += 1
            counters["ood"] += int(ood_ok)
        success = all(
            (
                domain_ok,
                primary_ok,
                retrieval_ok,
                status_ok,
                relevance_ok,
                source_ok,
                unsupported_ok,
                leakage_ok,
                ood_ok,
            )
        )
        if not success:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "domain": understanding.domain_decision,
                    "primary_intent": understanding.primary_intent,
                    "status": result.status,
                    "reason_code": result.refusal_reason,
                    "retrieval_called": bool(retrieval.calls),
                    "sources": [item.document_id for item in result.sources],
                }
            )
    total = len(cases)
    return (
        {
            "cases": total,
            "domain_classification_accuracy": counters["domain"] / total,
            "ood_rejection": counters["ood"] / max(1, ood_total),
            "primary_intent_accuracy": counters["primary"] / total,
            "final_answer_relevance": counters["relevance"] / total,
            "source_support": counters["source"] / max(1, answered),
            "unsupported_concrete_values": 1 - counters["unsupported"] / total,
            "example_leakage": 1 - counters["leakage"] / total,
            "failures": len(failures),
        },
        failures,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline regulation domain evaluation")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    metrics, failures = evaluate_cases(cases)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if args.show_failures and failures:
        print(json.dumps(failures, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
