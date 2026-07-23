import json

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_approval_rule_service,
    get_optional_approval_rule_repository,
)
from app.main import app
from app.rules.models import ApprovalContext
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from scripts import evaluate_approval_route
from scripts.validate_approval_rules import load_rule_seed


@pytest.fixture
def rule_repository() -> InMemoryApprovalRuleRepository:
    _, base, additional = load_rule_seed()
    return InMemoryApprovalRuleRepository(base, additional)


@pytest.fixture
def rule_service(
    rule_repository: InMemoryApprovalRuleRepository,
) -> ApprovalRuleService:
    return ApprovalRuleService(rule_repository)


@pytest.fixture(autouse=True)
def clear_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def test_api_returns_resolved_route(rule_service: ApprovalRuleService) -> None:
    app.dependency_overrides[get_approval_rule_service] = lambda: rule_service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/approval-rules/evaluate",
            json={"amount": "180000", "budget_status": "budgeted"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert (
        response.json()["base_rule_code"]
        == "BUDGETED_100000_01_500000"
    )


def test_api_returns_clarification_without_budget_status(
    rule_service: ApprovalRuleService,
) -> None:
    app.dependency_overrides[get_approval_rule_service] = lambda: rule_service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/approval-rules/evaluate",
            json={"amount": "180000"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_clarification"


def test_api_rejects_negative_amount(
    rule_service: ApprovalRuleService,
) -> None:
    app.dependency_overrides[get_approval_rule_service] = lambda: rule_service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/approval-rules/evaluate",
            json={"amount": "-1", "budget_status": "budgeted"},
        )

    assert response.status_code == 422


def test_api_health_returns_rule_counts(
    rule_repository: InMemoryApprovalRuleRepository,
) -> None:
    app.dependency_overrides[
        get_optional_approval_rule_repository
    ] = lambda: rule_repository

    with TestClient(app) as client:
        response = client.get("/api/v1/approval-rules/health")

    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["base_rules_count"] == 5
    assert response.json()["additional_rules_count"] == 7


def test_cli_json_output(
    monkeypatch,
    capsys,
    rule_service: ApprovalRuleService,
) -> None:
    monkeypatch.setattr(
        evaluate_approval_route,
        "build_service",
        lambda: rule_service,
    )

    exit_code = evaluate_approval_route.main(
        [
            "--amount",
            "180000",
            "--budget-status",
            "budgeted",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "resolved"
    assert output["base_rule_code"] == "BUDGETED_100000_01_500000"


def test_rule_engine_does_not_need_openai_or_rag(
    rule_service: ApprovalRuleService,
) -> None:
    result = rule_service.evaluate(
        ApprovalContext(amount="180000", budget_status="budgeted")
    )

    assert result.status == "resolved"
