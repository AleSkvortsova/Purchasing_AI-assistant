import json

import pytest

from app.rag.models import HybridRetrievalResult
from scripts.evaluate_regulation_qa import (
    DEFAULT_CASES,
    PRODUCTION_CASES,
    evaluate_cases,
    load_cases,
    main,
)


def test_regulation_dataset_has_required_categories_and_outside_case() -> None:
    cases = load_cases(DEFAULT_CASES)
    categories = {case["category"] for case in cases}
    assert len(cases) >= 15
    assert {"threshold", "required_fields", "status", "outside_kb"} <= categories
    assert any(case["should_refuse"] for case in cases)


def test_production_dataset_contains_exactly_twenty_typed_cases() -> None:
    cases = load_cases(PRODUCTION_CASES)
    assert len(cases) == 20
    assert sum(case["expected_status"] == "answered" for case in cases) == 18
    assert sum(
        case["expected_status"] == "clarification_required" for case in cases
    ) == 1
    assert sum(case["expected_status"] == "insufficient_context" for case in cases) == 1
    assert all("expected_intents" in case for case in cases)
    assert all("required_answer_terms" in case for case in cases)
    assert all("forbidden_answer_terms" in case for case in cases)


def test_evaluation_metrics_are_explicit_contract_proxies() -> None:
    cases = [
        {
            "case_id": "answer",
            "question": "Вопрос",
            "expected_document_ids": ["kb-1"],
            "preferred_document_id": "kb-1",
            "should_refuse": False,
        },
        {
            "case_id": "outside",
            "question": "Цена поставщика",
            "expected_document_ids": [],
            "preferred_document_id": None,
            "should_refuse": True,
        },
    ]
    result = HybridRetrievalResult(
        chunk_id="11111111-1111-4111-8111-111111111111",
        document_id="kb-1",
        source_filename="01.md",
        document_title="Документ",
        document_type="regulation",
        section_path="Раздел",
        content="Правило",
        priority=1,
        hybrid_score=0.1,
    )
    metrics, failures = evaluate_cases(cases, [[result], [result]], 5)
    assert metrics["groundedness_contract"] == 1
    assert metrics["answer_correctness_proxy"] == 1
    assert metrics["refusal_correctness"] == 1
    assert metrics["hallucination_rate"] == 0
    assert metrics["answer_relevance"] == 1
    assert metrics["unsupported_concrete_value_rate"] == 0
    assert metrics["example_leakage_rate"] == 0
    assert metrics["normative_source_accuracy"] == 1
    assert "end_to_end_success_rate" in metrics
    assert "false_refusal_rate" in metrics
    assert "clarification_accuracy" in metrics
    assert "general_policy_question_accuracy" in metrics
    assert "word_number_normalization_accuracy" in metrics
    assert "multi_intent_answer_accuracy" in metrics
    assert failures == []


def test_general_help_is_evaluated_as_deterministic_clarification() -> None:
    case = {
        "case_id": "general-help",
        "question": "Расскажи всё про закупки.",
        "expected_document_ids": [],
        "expected_status": "clarification_required",
        "expected_intents": ["general_help"],
    }

    metrics, failures = evaluate_cases([case], [[]], 5)

    assert metrics["end_to_end_success_rate"] == 1
    assert failures == []


def test_uncited_template_candidate_is_not_example_leakage() -> None:
    case = {
        "case_id": "approval-with-template-candidate",
        "question": (
            "Закупка на 100 тысяч рублей предусмотрена бюджетом. Кто согласует?"
        ),
        "expected_document_ids": ["kb-009"],
        "expected_status": "answered",
    }
    normative = HybridRetrievalResult(
        chunk_id="11111111-1111-4111-8111-111111111111",
        document_id="kb-009",
        source_filename="09.md",
        document_title="Правила согласования",
        document_type="approval_rules",
        section_path="Матрица согласования",
        content="Бюджетная закупка до 100 000 руб.",
        priority=1,
        hybrid_score=0.2,
    )
    template = HybridRetrievalResult(
        chunk_id="22222222-2222-4222-8222-222222222222",
        document_id="kb-003",
        source_filename="03.md",
        document_title="Шаблон заявки",
        document_type="template",
        section_path="Пример",
        content="Бюджет: 200 000 руб.",
        priority=2,
        hybrid_score=0.1,
    )

    metrics, failures = evaluate_cases(
        [case],
        [[normative, template]],
        5,
    )

    assert metrics["example_leakage_rate"] == 0
    assert metrics["unsupported_concrete_value_rate"] == 0
    assert failures == []


def test_invalid_small_dataset_is_rejected(tmp_path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError, match="at least 15"):
        load_cases(path)


def test_offline_cli_does_not_require_external_configuration(capsys) -> None:
    assert main(["--offline", "--top-k", "3"]) == 0
    output = capsys.readouterr().out
    assert "mode: offline hybrid retrieval" in output
    assert "semantic correctness requires manual" in output
