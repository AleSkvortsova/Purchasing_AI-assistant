from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.extraction.exceptions import ApprovalExtractionProviderError
from app.extraction.models import RawApprovalExtraction
from app.extraction.openai_schema import OpenAIApprovalExtractionPayload
from app.extraction.postprocessing import MULTIPLE_AMOUNTS_CONTRADICTION
from app.extraction.provider import (
    FakeApprovalExtractionProvider,
    OpenAIApprovalExtractionProvider,
    RuleBasedApprovalExtractionProvider,
)
from app.extraction.service import (
    ApprovalContextExtractionService,
    ApprovalEvaluationOrchestrator,
)
from app.rules.models import ApprovalRouteResult
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from scripts.validate_approval_rules import load_rule_seed


def test_rule_based_extraction_success() -> None:
    service = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    )

    result = service.extract(
        "Нужны юридические услуги на 600 тысяч, закупка бюджетная, "
        "поставщик единственный"
    )

    assert result.status == "extracted"
    assert result.extraction.amount == 600_000
    assert result.extraction.budget_status == "budgeted"
    assert result.extraction.category_code == "S11"
    assert result.extraction.single_supplier is True
    assert result.approval_context is not None


def test_real_openai_payload_creates_context_and_runs_rules() -> None:
    text = (
        "Нужны юридические услуги на 600 тысяч, закупка бюджетная, "
        "поставщик единственный."
    )
    payload = OpenAIApprovalExtractionPayload(
        amount_raw="600 тысяч",
        budget_status_raw="budgeted",
        urgency_raw=None,
        single_supplier_raw=True,
        category_raw="S11",
        has_data_access_raw=False,
        work_on_site_raw=False,
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
        confidence_items=[
            {"field_name": "amount", "confidence": 0.99},
            {"field_name": "budget_status", "confidence": 0.99},
            {"field_name": "urgency", "confidence": 0.99},
            {"field_name": "single_supplier", "confidence": 0.99},
            {"field_name": "category", "confidence": 0.99},
            {"field_name": "has_data_access", "confidence": 0.99},
            {"field_name": "work_on_site", "confidence": 0.99},
        ],
        evidence_items=[
            {"field_name": "amount", "evidence": "600 тысяч"},
            {
                "field_name": "budget_status",
                "evidence": "закупка бюджетная",
            },
            {
                "field_name": "single_supplier",
                "evidence": "поставщик единственный",
            },
            {
                "field_name": "category",
                "evidence": "юридические услуги",
            },
        ],
        unknown_fields=["urgency", "has_data_access", "work_on_site"],
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
    openai_provider = OpenAIApprovalExtractionProvider(
        api_key="test-key",
        model="gpt-5.6-luna",
        client=client,
    )
    openai_service = ApprovalContextExtractionService(openai_provider)
    openai_result = openai_service.extract(text)

    assert openai_result.status == "extracted"
    assert openai_result.extraction.missing_fields == []
    assert openai_result.clarification_questions == []
    assert openai_result.approval_context is not None
    assert openai_result.approval_context.has_data_access is False
    assert openai_result.approval_context.work_on_site is False
    assert openai_result.approval_context.urgency is None

    rule_based_result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract(text)
    excluded = {"confidence_by_field", "evidence_by_field"}
    assert openai_result.extraction.model_dump(exclude=excluded) == (
        rule_based_result.extraction.model_dump(exclude=excluded)
    )

    _, base_rules, additional_rules = load_rule_seed()
    approval_service = ApprovalRuleService(
        InMemoryApprovalRuleRepository(base_rules, additional_rules)
    )
    route = ApprovalEvaluationOrchestrator(
        openai_service,
        approval_service,
    ).extract_and_evaluate(text)

    assert route.approval_route_result is not None
    assert route.approval_route_result.final_approvers == [
        "Руководитель подразделения",
        "Финансовый блок",
        "Руководитель закупок",
        "Юридическая служба",
    ]


@pytest.mark.parametrize(
    ("field_name", "raw_field", "value"),
    [
        ("single_supplier", "single_supplier_raw", False),
        ("single_supplier", "single_supplier_raw", True),
        ("single_supplier", "single_supplier_raw", None),
        ("has_data_access", "has_data_access_raw", False),
        ("has_data_access", "has_data_access_raw", True),
        ("has_data_access", "has_data_access_raw", None),
        ("work_on_site", "work_on_site_raw", False),
        ("work_on_site", "work_on_site_raw", True),
        ("work_on_site", "work_on_site_raw", None),
    ],
)
def test_optional_boolean_values_do_not_become_missing(
    field_name: str,
    raw_field: str,
    value: bool | None,
) -> None:
    text = "Закупка бюджетная на 180000, подтверждённый признак"
    evidence = {
        "amount": "180000",
        "budget_status": "Закупка бюджетная",
    }
    if value is True:
        evidence[field_name] = "подтверждённый признак"
    raw = RawApprovalExtraction(
        amount_raw="180000",
        budget_status_raw="budgeted",
        evidence_by_field=evidence,
        unknown_fields=[field_name] if value is None else [],
        **{raw_field: value},
    )

    result = ApprovalContextExtractionService(
        FakeApprovalExtractionProvider(raw)
    ).extract(text)

    assert result.status == "extracted"
    assert result.extraction.missing_fields == []
    assert getattr(result.extraction, field_name) is (value is True)
    assert result.approval_context is not None


@pytest.mark.parametrize(
    ("text", "raw", "missing_field"),
    [
        (
            "Закупка бюджетная",
            RawApprovalExtraction(
                budget_status_raw="budgeted",
                evidence_by_field={
                    "budget_status": "Закупка бюджетная"
                },
            ),
            "amount",
        ),
        (
            "Сумма 180000",
            RawApprovalExtraction(
                amount_raw="180000",
                evidence_by_field={"amount": "180000"},
            ),
            "budget_status",
        ),
    ],
)
def test_none_for_required_field_is_missing(
    text: str,
    raw: RawApprovalExtraction,
    missing_field: str,
) -> None:
    result = ApprovalContextExtractionService(
        FakeApprovalExtractionProvider(raw)
    ).extract(text)

    assert result.status == "needs_clarification"
    assert result.extraction.missing_fields == [missing_field]
    assert result.approval_context is None


def test_claimed_urgency_without_priority_does_not_block_base_context() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract("Очень срочно: закупка бюджетная на 180000")

    assert result.status == "extracted"
    assert result.extraction.urgency_claimed is True
    assert result.extraction.urgency is None
    assert result.extraction.missing_fields == []
    assert result.extraction.warnings
    assert result.approval_context is not None


def test_range_requires_clarification() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract("Закупка бюджетная, сумма 180–220 тысяч")

    assert result.status == "needs_clarification"
    assert "amount" in result.extraction.missing_fields
    assert "ожидаемую или максимальную" in result.clarification_questions[0]
    assert result.approval_context is None


def test_from_to_range_regression_uses_specific_clarification() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract(
        "Стоимость будет от 180 до 220 тысяч, закупка бюджетная."
    )

    assert result.status == "needs_clarification"
    assert result.extraction.amount is None
    assert result.extraction.money is not None
    assert result.extraction.money.min_amount == 180_000
    assert result.extraction.money.max_amount == 220_000
    assert result.extraction.money.amount_type == "range"
    assert result.extraction.money.currency == "RUB"
    assert "от 180 до 220 тысяч" in result.extraction.money.evidence
    assert result.approval_context is None
    assert result.clarification_questions == [
        "Какую сумму использовать для определения маршрута согласования: "
        "ожидаемую или максимальную?"
    ]


def test_openai_shaped_raw_range_is_normalized_the_same_way() -> None:
    provider = FakeApprovalExtractionProvider(
        RawApprovalExtraction(
            amount_raw="от 1,2 до 1,5 млн",
            budget_status_raw="budgeted",
            evidence_by_field={
                "amount": "от 1,2 до 1,5 млн",
                "budget_status": "закупка бюджетная",
            },
        )
    )

    result = ApprovalContextExtractionService(provider).extract(
        "Стоимость от 1,2 до 1,5 млн, закупка бюджетная"
    )

    assert result.status == "needs_clarification"
    assert result.extraction.money is not None
    assert result.extraction.money.min_amount == 1_200_000
    assert result.extraction.money.max_amount == 1_500_000
    assert result.approval_context is None


def test_multiple_ranges_return_conflict() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract(
        "Стоимость от 100 до 200 тысяч или от 300 до 400 тысяч, "
        "закупка бюджетная"
    )

    assert result.status == "conflict"
    assert result.approval_context is None
    assert result.extraction.contradictions == [
        MULTIPLE_AMOUNTS_CONTRADICTION
    ]


def test_multiple_amounts_are_conflict_for_rule_based_and_openai_mock() -> None:
    text = "Товар стоит 100 тысяч, доставка 20 тысяч, закупка бюджетная"
    rule_result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract(text)
    payload = OpenAIApprovalExtractionPayload(
        amount_raw=None,
        budget_status_raw="budgeted",
        urgency_raw=None,
        single_supplier_raw=False,
        category_raw=None,
        has_data_access_raw=False,
        work_on_site_raw=False,
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
            {
                "field_name": "budget_status",
                "evidence": "закупка бюджетная",
            }
        ],
        unknown_fields=["amount"],
        contradictions=[],
    )
    client = Mock()
    client.responses.parse.return_value = SimpleNamespace(
        output_parsed=payload,
        id="response-multiple",
        status="completed",
        incomplete_details=None,
        error=None,
        output=[],
        output_text="",
    )
    openai_result = ApprovalContextExtractionService(
        OpenAIApprovalExtractionProvider(
            api_key="test-key",
            model="gpt-5.6-luna",
            client=client,
        )
    ).extract(text)

    for result in (rule_result, openai_result):
        assert result.status == "conflict"
        assert result.extraction.amount is None
        assert result.extraction.missing_fields == ["amount"]
        assert result.extraction.contradictions == [
            MULTIPLE_AMOUNTS_CONTRADICTION
        ]
        assert result.clarification_questions == [
            "Укажите сумму закупки."
        ]
        assert result.approval_context is None


def test_explicit_unit_and_total_amount_roles_are_not_conflict() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract(
        "Цена за единицу 100 рублей, общая сумма 1000 рублей, "
        "закупка бюджетная"
    )

    assert result.status == "extracted"
    assert result.extraction.amount == 1000
    assert result.extraction.contradictions == []
    assert result.approval_context is not None


def test_missing_budget_status_requires_clarification() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract("Бюджет 180 тысяч на мониторы Samsung")

    assert result.status == "needs_clarification"
    assert result.extraction.amount == 180_000
    assert result.extraction.budget_status is None
    assert result.extraction.single_supplier is False
    assert "budget_status" in result.extraction.missing_fields


@pytest.mark.parametrize(
    ("raw_budget_status", "budget_evidence"),
    [
        ("unbudgeted", "в бюджете не предусмотрена"),
        ("budgeted", "Закупка бюджетная"),
        (None, None),
    ],
)
def test_conflicting_budget_clears_any_provider_value(
    raw_budget_status: str | None,
    budget_evidence: str | None,
) -> None:
    text = (
        "Закупка бюджетная, но в бюджете не предусмотрена, сумма 180000"
    )
    evidence = {"amount": "180000"}
    if budget_evidence is not None:
        evidence["budget_status"] = budget_evidence
    raw = RawApprovalExtraction(
        amount_raw="180000",
        budget_status_raw=raw_budget_status,
        evidence_by_field=evidence,
        contradictions=[
            "Both budgeted and unbudgeted signals are in conflict"
        ],
    )

    result = ApprovalContextExtractionService(
        FakeApprovalExtractionProvider(raw)
    ).extract(text)

    assert result.status == "conflict"
    assert result.extraction.budget_status is None
    assert result.extraction.missing_fields == ["budget_status"]
    assert result.extraction.contradictions == [
        "Противоречивые сведения о бюджетном статусе"
    ]
    assert result.clarification_questions == [
        "Закупка предусмотрена бюджетом или является внебюджетной?"
    ]
    assert not any(
        "Неподтверждённое замечание provider" in warning
        for warning in result.warnings
    )
    assert result.approval_context is None


def test_it_without_access_information_does_not_block_base_context() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract("IT-интеграция на 200 тысяч, закупка бюджетная")

    assert result.status == "extracted"
    assert result.extraction.category_code == "S05"
    assert result.extraction.has_data_access is False
    assert result.extraction.missing_fields == []
    assert result.approval_context is not None


def test_fabricated_evidence_causes_conflict() -> None:
    provider = FakeApprovalExtractionProvider(
        RawApprovalExtraction(
            amount_raw="180000",
            budget_status_raw="budgeted",
            evidence_by_field={
                "amount": "180000",
                "budget_status": "этого фрагмента нет",
            },
        )
    )

    result = ApprovalContextExtractionService(provider).extract(
        "Закупка бюджетная на 180000"
    )

    assert result.status == "conflict"
    assert "budget_status" not in result.extraction.evidence_by_field
    assert result.approval_context is None


def test_extracted_fact_without_evidence_causes_conflict() -> None:
    provider = FakeApprovalExtractionProvider(
        RawApprovalExtraction(
            amount_raw="180000",
            budget_status_raw="budgeted",
            evidence_by_field={"amount": "180000"},
        )
    )

    result = ApprovalContextExtractionService(provider).extract(
        "Закупка бюджетная на 180000"
    )

    assert result.status == "conflict"
    assert any(
        "budget_status" in contradiction
        for contradiction in result.extraction.contradictions
    )


@pytest.mark.parametrize(
    ("field_name", "raw_values", "text_suffix"),
    [
        ("urgency", {"urgency_raw": "P1"}, " приоритет P1"),
        (
            "single_supplier",
            {"single_supplier_raw": True},
            " поставщик единственный",
        ),
        ("category", {"category_raw": "S11"}, " юридические услуги"),
        (
            "has_data_access",
            {"has_data_access_raw": True},
            " доступ к данным",
        ),
        (
            "work_on_site",
            {"work_on_site_raw": True},
            " работы на объекте",
        ),
    ],
)
def test_positive_optional_fact_still_requires_evidence(
    field_name: str,
    raw_values: dict,
    text_suffix: str,
) -> None:
    raw = RawApprovalExtraction(
        amount_raw="180000",
        budget_status_raw="budgeted",
        evidence_by_field={
            "amount": "180000",
            "budget_status": "закупка бюджетная",
        },
        **raw_values,
    )

    result = ApprovalContextExtractionService(
        FakeApprovalExtractionProvider(raw)
    ).extract("Закупка бюджетная на 180000" + text_suffix)

    assert result.status == "conflict"
    assert any(
        field_name in contradiction
        for contradiction in result.extraction.contradictions
    )
    assert result.approval_context is None


def test_provider_failure_is_not_retried_by_service() -> None:
    provider = FakeApprovalExtractionProvider(
        error=ApprovalExtractionProviderError("controlled failure")
    )

    try:
        ApprovalContextExtractionService(provider).extract(
            "Закупка бюджетная на 180000"
        )
    except ApprovalExtractionProviderError as exc:
        assert str(exc) == "controlled failure"
    else:
        raise AssertionError("provider error must be propagated")
    assert provider.calls == 1


def test_unexpected_provider_failure_returns_failed_status() -> None:
    provider = FakeApprovalExtractionProvider(error=ValueError("bad payload"))

    result = ApprovalContextExtractionService(provider).extract(
        "Закупка бюджетная на 180000"
    )

    assert result.status == "failed"
    assert result.approval_context is None


def test_unknown_explicit_category_does_not_block_base_context() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract(
        "Категория неизвестная, сумма 180 тысяч, закупка бюджетная"
    )

    assert result.status == "extracted"
    assert result.extraction.category_code is None
    assert result.extraction.missing_fields == []
    assert result.approval_context is not None


def test_ambiguous_category_without_opposing_facts_is_not_conflict() -> None:
    text = (
        "Нужна готовая полиграфия и печать по макету на 100 тысяч, "
        "закупка бюджетная"
    )
    raw = RawApprovalExtraction(
        amount_raw="100 тысяч",
        budget_status_raw="budgeted",
        category_raw="G11",
        evidence_by_field={
            "amount": "100 тысяч",
            "budget_status": "закупка бюджетная",
            "category": "готовая полиграфия",
        },
        contradictions=["Модель сочла категории противоречивыми"],
    )

    result = ApprovalContextExtractionService(
        FakeApprovalExtractionProvider(raw)
    ).extract(text)

    assert result.status == "extracted"
    assert result.extraction.category_code is None
    assert result.extraction.contradictions == []
    assert result.extraction.missing_fields == []
    assert any(
        "Неподтверждённое замечание provider" in warning
        for warning in result.warnings
    )
    assert result.approval_context is not None


def test_ambiguous_category_needed_by_additional_rule_is_clarified() -> None:
    result = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    ).extract(
        "Юридические услуги и перевозка на 100 тысяч, закупка бюджетная"
    )

    assert result.status == "needs_clarification"
    assert result.extraction.category_code is None
    assert result.extraction.contradictions == []
    assert result.extraction.missing_fields == ["category_code"]
    assert result.clarification_questions == ["Уточните тип закупки."]
    assert result.approval_context is None


def test_unconfirmed_model_contradiction_becomes_warning() -> None:
    raw = RawApprovalExtraction(
        amount_raw="180000",
        budget_status_raw="budgeted",
        evidence_by_field={
            "amount": "180000",
            "budget_status": "закупка бюджетная",
        },
        contradictions=["Произвольное неподтверждённое противоречие"],
    )

    result = ApprovalContextExtractionService(
        FakeApprovalExtractionProvider(raw)
    ).extract("Закупка бюджетная на 180000")

    assert result.status == "extracted"
    assert result.extraction.contradictions == []
    assert result.extraction.missing_fields == []
    assert result.warnings == [
        "Неподтверждённое замечание provider: "
        "Произвольное неподтверждённое противоречие"
    ]
    assert result.approval_context is not None


def test_evidence_matching_normalizes_case_and_spaces() -> None:
    provider = FakeApprovalExtractionProvider(
        RawApprovalExtraction(
            amount_raw="180 000",
            budget_status_raw="budgeted",
            evidence_by_field={
                "amount": "180   000",
                "budget_status": "ЗАКУПКА БЮДЖЕТНАЯ",
            },
        )
    )

    result = ApprovalContextExtractionService(provider).extract(
        "Закупка   бюджетная на 180 000"
    )

    assert result.status == "extracted"
    assert result.extraction.amount == 180_000


def test_orchestrator_skips_rule_engine_for_incomplete_context() -> None:
    extraction = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    )
    approval_service = Mock()
    orchestrator = ApprovalEvaluationOrchestrator(
        extraction,
        approval_service,
    )

    result = orchestrator.extract_and_evaluate("Мониторы на 180 тысяч")

    assert result.approval_route_result is None
    approval_service.evaluate.assert_not_called()


def test_orchestrator_calls_rule_engine_once_for_complete_context() -> None:
    extraction = ApprovalContextExtractionService(
        RuleBasedApprovalExtractionProvider()
    )
    approval_service = Mock()
    approval_service.evaluate.return_value = ApprovalRouteResult(
        status="resolved"
    )
    orchestrator = ApprovalEvaluationOrchestrator(
        extraction,
        approval_service,
    )

    result = orchestrator.extract_and_evaluate(
        "Мониторы на 180 тысяч, закупка бюджетная"
    )

    assert result.approval_route_result is not None
    approval_service.evaluate.assert_called_once()
