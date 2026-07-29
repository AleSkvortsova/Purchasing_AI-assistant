import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_approval_evaluation_orchestrator,
    get_approval_extraction_provider,
    get_approval_extraction_service,
)
from app.core.config import get_settings
from app.extraction.exceptions import (
    ApprovalExtractionConfigurationError,
    ApprovalExtractionProviderError,
)
from app.extraction.models import RawApprovalExtraction
from app.extraction.openai_schema import OpenAIApprovalExtractionPayload
from app.extraction.provider import (
    FakeApprovalExtractionProvider,
    OpenAIApprovalExtractionProvider,
    RuleBasedApprovalExtractionProvider,
)
from app.extraction.service import (
    ApprovalContextExtractionService,
    ApprovalEvaluationOrchestrator,
)
from app.main import app
from app.rules.models import ApprovalRouteResult
from scripts import extract_approval_context


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def extraction_service() -> ApprovalContextExtractionService:
    return ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    )


def test_extract_endpoint(
    extraction_service: ApprovalContextExtractionService,
) -> None:
    app.dependency_overrides[
        get_approval_extraction_service
    ] = lambda: extraction_service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/approval-context/extract",
            json={
                "text": (
                    "Юридические услуги на 600 тысяч, "
                    "закупка бюджетная"
                )
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "extracted"
    assert response.json()["extraction"]["category_code"] == "S11"


def test_extract_endpoint_rejects_blank_text(
    extraction_service: ApprovalContextExtractionService,
) -> None:
    app.dependency_overrides[
        get_approval_extraction_service
    ] = lambda: extraction_service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/approval-context/extract",
            json={"text": "   "},
        )

    assert response.status_code == 422


def test_provider_failure_is_controlled() -> None:
    service = ApprovalContextExtractionService(
        FakeApprovalExtractionProvider(
            error=ApprovalExtractionProviderError("provider unavailable")
        )
    )
    app.dependency_overrides[
        get_approval_extraction_service
    ] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/approval-context/extract",
            json={"text": "Закупка бюджетная на 180000"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "provider unavailable"


def test_extract_and_evaluate_endpoint(
    extraction_service: ApprovalContextExtractionService,
) -> None:
    approval_service = Mock()
    approval_service.evaluate.return_value = ApprovalRouteResult(
        status="resolved",
        final_approvers=["Руководитель подразделения"],
    )
    orchestrator = ApprovalEvaluationOrchestrator(
        extraction_service,
        approval_service,
    )
    app.dependency_overrides[
        get_approval_evaluation_orchestrator
    ] = lambda: orchestrator

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/approval-context/extract-and-evaluate",
            json={"text": "Мониторы на 180 тысяч, закупка бюджетная"},
        )

    assert response.status_code == 200
    assert response.json()["approval_route_result"]["status"] == "resolved"
    approval_service.evaluate.assert_called_once()


def test_cli_rule_based_json_does_not_use_openai(
    monkeypatch,
    capsys,
) -> None:
    def fail_openai(*args, **kwargs):
        raise AssertionError("OpenAI must not be constructed")

    monkeypatch.setattr(
        extract_approval_context,
        "OpenAIApprovalExtractionProvider",
        fail_openai,
    )

    exit_code = extract_approval_context.main(
        [
            "Мониторы Samsung на 180 тысяч, закупка бюджетная",
            "--provider",
            "rule-based",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "extracted"
    assert payload["extraction"]["single_supplier"] is False


def test_cli_returns_structured_range_and_specific_question(capsys) -> None:
    exit_code = extract_approval_context.main(
        [
            "Стоимость будет от 180 до 220 тысяч, закупка бюджетная.",
            "--provider",
            "rule-based",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "needs_clarification"
    assert payload["extraction"]["money"] == {
        "amount": None,
        "min_amount": "180000",
        "max_amount": "220000",
        "amount_type": "range",
        "currency": "RUB",
        "evidence": "от 180 до 220 тысяч",
    }
    assert payload["approval_context"] is None
    assert payload["clarification_questions"] == [
        "Какую сумму использовать для определения маршрута согласования: "
        "ожидаемую или максимальную?"
    ]


def test_openai_provider_uses_structured_output_without_network() -> None:
    payload = OpenAIApprovalExtractionPayload(
        amount_raw="180000",
        budget_status_raw=None,
        urgency_raw=None,
        single_supplier_raw=None,
        category_raw=None,
        has_data_access_raw=None,
        work_on_site_raw=None,
        procurement_type_raw=None,
        item_name_raw=None,
        quantity_raw=None,
        unit_raw=None,
        specifications_raw=None,
        desired_result_raw=None,
        amount_modifier_raw=None,
        billing_period_raw=None,
        desired_delivery_date_raw=None,
        delivery_location_raw=None,
        business_justification_raw=None,
        department_raw=None,
        contact_person_raw=None,
        urgency_claimed=False,
        confidence_items=[],
        evidence_items=[
            {"field_name": "amount", "evidence": "180000"}
        ],
        unknown_fields=[],
        contradictions=[],
    )
    client = Mock()
    client.responses.parse.return_value = SimpleNamespace(
        output_parsed=payload,
        id="response-test",
        status="completed",
        incomplete_details=None,
        error=None,
        output=[],
        output_text="",
    )
    provider = OpenAIApprovalExtractionProvider(
        api_key="test-key",
        model="gpt-5-nano",
        client=client,
    )

    result = provider.extract("Сумма 180000")

    assert result == RawApprovalExtraction(
        amount_raw="180000",
        evidence_by_field={"amount": "180000"},
    )
    kwargs = client.responses.parse.call_args.kwargs
    assert kwargs["text_format"] is OpenAIApprovalExtractionPayload
    assert "temperature" not in kwargs
    assert kwargs["model"] == "gpt-5-nano"
    assert kwargs["input"] == "Сумма 180000"
    assert kwargs["instructions"]
    assert kwargs["store"] is False
    assert kwargs["timeout"] == 30


def test_openai_provider_requires_key() -> None:
    with pytest.raises(
        ApprovalExtractionConfigurationError,
        match="OPENAI_API_KEY",
    ):
        OpenAIApprovalExtractionProvider(
            api_key=None,
            model="gpt-5-nano",
        )


def test_openai_validation_error_is_not_retried() -> None:
    client = Mock()
    client.responses.parse.side_effect = ValueError("invalid payload")
    provider = OpenAIApprovalExtractionProvider(
        api_key="test-key",
        model="gpt-5-nano",
        max_retries=2,
        client=client,
    )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="response could not be processed",
    ):
        provider.extract("Сумма 180000")

    assert client.responses.parse.call_count == 1


def test_health_reports_unconfigured_openai(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("APPROVAL_EXTRACTION_PROVIDER", "openai")
    get_settings.cache_clear()
    get_approval_extraction_provider.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/approval-context/health")

        assert response.status_code == 200
        assert response.json() == {
            "provider": "openai",
            "configured": False,
            "openai_configured": False,
        }
    finally:
        get_settings.cache_clear()
        get_approval_extraction_provider.cache_clear()
