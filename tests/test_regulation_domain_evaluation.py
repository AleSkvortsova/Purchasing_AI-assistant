import json
from pathlib import Path

from scripts.evaluate_regulation_domain import evaluate_cases

CASES = Path("data/evaluation/regulation_domain_cases.json")


def test_domain_dataset_is_independent_and_covers_decisions() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))

    assert len(cases) >= 15
    assert {case["expected_domain"] for case in cases} == {
        "known_domain_intent",
        "ambiguous_domain",
        "outside_domain",
    }
    assert sum(case["expected_domain"] == "outside_domain" for case in cases) >= 6


def test_domain_evaluation_metrics_are_separate_and_green() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    metrics, failures = evaluate_cases(cases)

    assert failures == []
    assert metrics["domain_classification_accuracy"] == 1.0
    assert metrics["ood_rejection"] == 1.0
    assert metrics["primary_intent_accuracy"] == 1.0
    assert metrics["final_answer_relevance"] == 1.0
    assert metrics["source_support"] == 1.0
    assert metrics["unsupported_concrete_values"] == 0.0
    assert metrics["example_leakage"] == 0.0
