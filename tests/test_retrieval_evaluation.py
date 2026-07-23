from pathlib import Path
from uuid import UUID

import pytest

from app.rag.models import RetrievalResult
from scripts.evaluate_retrieval import (
    build_offline_service,
    calculate_metrics,
    load_cases,
)


def _result(document_id: str, section_path: str) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=UUID("11111111-1111-4111-8111-111111111111"),
        document_id=document_id,
        source_filename=f"{document_id}.md",
        document_title=document_id,
        document_type="test",
        section_path=section_path,
        content="content",
        priority=1,
        similarity=1.0,
    )


def test_evaluation_calculates_hits_mrr_and_preferred_metrics() -> None:
    cases = [
        {
            "case_id": "one",
            "query": "q1",
            "expected_document_ids": ["kb-a", "kb-b"],
            "preferred_document_id": "kb-a",
            "expected_section_contains": ["Target"],
        },
        {
            "case_id": "two",
            "query": "q2",
            "expected_document_ids": ["kb-c"],
            "preferred_document_id": "kb-c",
        },
    ]
    results = [
        [
            _result("kb-b", "Target"),
            _result("kb-a", "Target"),
        ],
        [
            _result("kb-x", "Other"),
            _result("kb-y", "Other"),
            _result("kb-c", "Found"),
        ],
    ]

    metrics = calculate_metrics(cases, results)

    assert metrics["hit_at_1"] == pytest.approx(0.5)
    assert metrics["hit_at_3"] == pytest.approx(1.0)
    assert metrics["hit_at_5"] == pytest.approx(1.0)
    assert metrics["mrr"] == pytest.approx((1 + 1 / 3) / 2)
    assert metrics["preferred_hit_at_1"] == 0
    assert metrics["preferred_hit_at_3"] == pytest.approx(1.0)
    assert metrics["preferred_hit_at_5"] == pytest.approx(1.0)


def test_dataset_contains_fifteen_real_cases() -> None:
    cases = load_cases(
        Path("data/evaluation/retrieval_cases.json")
    )

    assert len(cases) >= 15
    assert {case["case_id"] for case in cases} >= {
        "approval-180k",
        "status-needs-rework",
        "delivery-date-past",
    }


def test_offline_service_does_not_create_external_clients(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.rag.repository.create_client",
        lambda *_: pytest.fail("Supabase client must not be created"),
    )
    monkeypatch.setattr(
        "app.rag.embeddings.OpenAI",
        lambda *_args, **_kwargs: pytest.fail(
            "OpenAI client must not be created"
        ),
    )

    service = build_offline_service()
    results = service.search(
        "Матрица согласования",
        mode="hybrid",
        top_k=5,
    )

    assert results
