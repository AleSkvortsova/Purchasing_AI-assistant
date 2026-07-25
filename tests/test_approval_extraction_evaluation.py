import json
from pathlib import Path

from app.extraction.provider import RuleBasedApprovalExtractionProvider
from app.extraction.service import ApprovalContextExtractionService
from scripts import evaluate_approval_extraction


def test_evaluation_dataset_has_at_least_twenty_cases() -> None:
    cases = json.loads(
        Path(
            "data/evaluation/approval_extraction_cases.json"
        ).read_text(encoding="utf-8")
    )

    assert len(cases) >= 20
    assert len({case["case_id"] for case in cases}) == len(cases)


def test_offline_evaluation_passes() -> None:
    cases = json.loads(
        evaluate_approval_extraction.DEFAULT_CASES.read_text(
            encoding="utf-8"
        )
    )
    service = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    )

    metrics, failures = evaluate_approval_extraction.evaluate_cases(
        cases,
        service,
    )

    assert failures == []
    assert metrics["complete_context_accuracy"] == 1.0
    assert metrics["boolean_f1"] == 1.0


def test_offline_mode_never_constructs_openai(monkeypatch) -> None:
    monkeypatch.setattr(
        evaluate_approval_extraction,
        "OpenAIApprovalExtractionProvider",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("OpenAI must not be constructed")
        ),
    )

    service = evaluate_approval_extraction.build_service(
        "openai",
        offline=True,
    )

    assert isinstance(
        service._provider,  # noqa: SLF001
        RuleBasedApprovalExtractionProvider,
    )


def test_prompt_contains_guardrails_and_few_shots() -> None:
    prompt = Path(
        "app/extraction/prompts/approval_context_extraction.md"
    ).read_text(encoding="utf-8")

    assert prompt.count("→") >= 8
    assert "Не вычисляй маршрут" in prompt
    assert "не означает единственного поставщика" in prompt
    assert "Руководитель подразделения" not in prompt
