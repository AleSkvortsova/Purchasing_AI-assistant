from scripts.evaluate_regulation_holdout import evaluate_cases


def test_user_answer_and_understanding_metrics_are_independent() -> None:
    case = {
        "case_id": "metric-separation",
        "question": "Какие параметры нужны для перевозки восьми контейнеров?",
        "expected_intents": [
            "category_classification",
            "responsibility_policy",
        ],
        "expected_slots": {"category_hint": "S03"},
        "expected_status": "answered",
        "expected_document_ids": ["kb-005"],
        "required_claims": ["маршрут", "вес"],
        "forbidden_claims": [],
        "clarification_text_pattern": None,
        "outside_kb": False,
    }

    metrics, failures = evaluate_cases([case])

    assert metrics["user_answer_success_rate"] == 1
    assert metrics["understanding_structure_accuracy"] == 0
    assert metrics["primary_intent_accuracy"] == 1
    assert metrics["secondary_intent_accuracy"] == 0
    assert metrics["slot_accuracy"] == 1
    assert metrics["end_to_end_success_rate"] == 0
    assert failures[0]["user_answer_success"] is True
    assert failures[0]["understanding_structure_ok"] is False


def test_supported_normative_source_is_not_rejected_for_different_id() -> None:
    case = {
        "case_id": "semantic-source",
        "question": "Можно указать марку оборудования и не допускать аналоги?",
        "expected_intents": ["brand_equivalent_policy"],
        "expected_slots": {"purchase_type": "goods"},
        "expected_status": "answered",
        "expected_document_ids": ["kb-001"],
        "required_claims": ["эквивалент", "обосновать"],
        "forbidden_claims": [],
        "clarification_text_pattern": None,
        "outside_kb": False,
    }

    metrics, failures = evaluate_cases([case])

    assert metrics["exact_source_match"] == 0
    assert metrics["source_claim_support_accuracy"] == 1
    assert metrics["normative_source_accuracy"] == 1
    assert metrics["user_answer_success_rate"] == 1
    assert failures == []
