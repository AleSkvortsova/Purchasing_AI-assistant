from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.rules.models import ApprovalContext
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from scripts.validate_approval_rules import (
    load_rule_seed,
    validate_approval_rules,
)


@pytest.fixture
def rules():
    _, base, additional = load_rule_seed()
    return base, additional


@pytest.fixture
def service(rules) -> ApprovalRuleService:
    base, additional = rules
    return ApprovalRuleService(
        InMemoryApprovalRuleRepository(base, additional)
    )


@pytest.mark.parametrize(
    ("amount", "expected_code", "expected_approvers"),
    [
        ("0", "BUDGETED_0_100000", ["Руководитель подразделения"]),
        ("99999", "BUDGETED_0_100000", ["Руководитель подразделения"]),
        ("100000", "BUDGETED_0_100000", ["Руководитель подразделения"]),
        (
            "100000.01",
            "BUDGETED_100000_01_500000",
            ["Руководитель подразделения", "Финансовый контролёр"],
        ),
        (
            "100001",
            "BUDGETED_100000_01_500000",
            ["Руководитель подразделения", "Финансовый контролёр"],
        ),
        (
            "180000",
            "BUDGETED_100000_01_500000",
            ["Руководитель подразделения", "Финансовый контролёр"],
        ),
        (
            "500000",
            "BUDGETED_100000_01_500000",
            ["Руководитель подразделения", "Финансовый контролёр"],
        ),
        (
            "500000.01",
            "BUDGETED_500000_01_PLUS",
            [
                "Руководитель подразделения",
                "Финансовый блок",
                "Руководитель закупок",
            ],
        ),
        (
            "500001",
            "BUDGETED_500000_01_PLUS",
            [
                "Руководитель подразделения",
                "Финансовый блок",
                "Руководитель закупок",
            ],
        ),
    ],
)
def test_budgeted_boundaries(
    service: ApprovalRuleService,
    amount: str,
    expected_code: str,
    expected_approvers: list[str],
) -> None:
    result = service.evaluate(
        ApprovalContext(amount=amount, budget_status="budgeted")
    )

    assert result.status == "resolved"
    assert result.base_rule_code == expected_code
    assert result.final_approvers == expected_approvers


@pytest.mark.parametrize(
    ("amount", "expected_code"),
    [
        ("0", "UNBUDGETED_0_100000"),
        ("100000", "UNBUDGETED_0_100000"),
        ("100000.01", "UNBUDGETED_100000_01_PLUS"),
        ("100001", "UNBUDGETED_100000_01_PLUS"),
    ],
)
def test_unbudgeted_boundaries(
    service: ApprovalRuleService,
    amount: str,
    expected_code: str,
) -> None:
    result = service.evaluate(
        ApprovalContext(amount=amount, budget_status="unbudgeted")
    )

    assert result.status == "resolved"
    assert result.base_rule_code == expected_code


def test_missing_budget_status_requests_clarification(
    service: ApprovalRuleService,
) -> None:
    result = service.evaluate(ApprovalContext(amount="180000"))

    assert result.status == "needs_clarification"
    assert result.missing_fields == ["budget_status"]
    assert result.clarification_questions == [
        "Закупка предусмотрена бюджетом или является внебюджетной?"
    ]


def test_missing_amount_is_validation_error() -> None:
    with pytest.raises(ValidationError):
        ApprovalContext.model_validate({"budget_status": "budgeted"})


@pytest.mark.parametrize("amount", ["-1", -1, 1.5])
def test_invalid_amount_is_rejected(amount) -> None:
    with pytest.raises(ValidationError):
        ApprovalContext(amount=amount, budget_status="budgeted")


def test_unknown_budget_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ApprovalContext(amount="1", budget_status="unknown")


def test_context_normalizes_category_and_urgency() -> None:
    context = ApprovalContext(
        amount="1",
        budget_status="budgeted",
        category_code=" s11 ",
        urgency="p2",
    )

    assert context.category_code == "S11"
    assert context.urgency == "P2"


@pytest.mark.parametrize(
    ("urgency", "expected"),
    [
        ("P1", "URGENCY_P1"),
        ("P2", "URGENCY_P2"),
        ("P3", None),
    ],
)
def test_urgency_rules(
    service: ApprovalRuleService,
    urgency: str,
    expected: str | None,
) -> None:
    result = service.evaluate(
        ApprovalContext(
            amount="180000",
            budget_status="budgeted",
            urgency=urgency,
        )
    )

    assert (expected in result.applied_additional_rule_codes) is (
        expected is not None
    )


@pytest.mark.parametrize(
    ("amount", "expected_codes"),
    [
        ("500000", ["SINGLE_SUPPLIER"]),
        (
            "500001",
            ["SINGLE_SUPPLIER", "SINGLE_SUPPLIER_OVER_500000"],
        ),
    ],
)
def test_single_supplier_rules(
    service: ApprovalRuleService,
    amount: str,
    expected_codes: list[str],
) -> None:
    result = service.evaluate(
        ApprovalContext(
            amount=amount,
            budget_status="budgeted",
            single_supplier=True,
        )
    )

    assert result.applied_additional_rule_codes == expected_codes


@pytest.mark.parametrize(
    ("context_values", "rule_code", "approver"),
    [
        (
            {"has_data_access": True},
            "IT_DATA_ACCESS",
            "IT / информационная безопасность",
        ),
        (
            {"category_code": "S11"},
            "LEGAL_SERVICES",
            "Юридическая служба",
        ),
        (
            {"work_on_site": True},
            "WORK_ON_SITE",
            "АХО / охрана труда",
        ),
    ],
)
def test_special_conditions(
    service: ApprovalRuleService,
    context_values: dict,
    rule_code: str,
    approver: str,
) -> None:
    result = service.evaluate(
        ApprovalContext(
            amount="180000",
            budget_status="budgeted",
            **context_values,
        )
    )

    assert rule_code in result.applied_additional_rule_codes
    assert approver in result.final_approvers


def test_combined_conditions_deduplicate_and_keep_stable_order(
    service: ApprovalRuleService,
) -> None:
    result = service.evaluate(
        ApprovalContext(
            amount="600000",
            budget_status="budgeted",
            urgency="P1",
            single_supplier=True,
            category_code="S11",
            has_data_access=True,
            work_on_site=True,
        )
    )

    assert result.final_approvers == [
        "Руководитель подразделения",
        "Финансовый блок",
        "Руководитель закупок",
        "IT / информационная безопасность",
        "Юридическая служба",
        "АХО / охрана труда",
    ]
    assert len(result.final_approvers) == len(set(result.final_approvers))
    assert (
        result.source_references[0].rule_code
        == "BUDGETED_500000_01_PLUS"
    )


def test_overlapping_base_rules_return_conflict(rules) -> None:
    base, additional = rules
    overlap = base[1].model_copy(
        update={
            "rule_code": "OVERLAP",
            "min_amount": Decimal("1"),
            "max_amount": Decimal("200000"),
        }
    )
    service = ApprovalRuleService(
        InMemoryApprovalRuleRepository([*base, overlap], additional)
    )

    result = service.evaluate(
        ApprovalContext(amount="180000", budget_status="budgeted")
    )

    assert result.status == "conflict"
    assert "BUDGETED_100000_01_500000" in result.warnings[0]
    assert "OVERLAP" in result.warnings[0]


def test_no_matching_rule_returns_manual_warning(rules) -> None:
    _, additional = rules
    service = ApprovalRuleService(
        InMemoryApprovalRuleRepository([], additional)
    )

    result = service.evaluate(
        ApprovalContext(amount="1", budget_status="budgeted")
    )

    assert result.status == "no_matching_rule"
    assert "ручная проверка" in result.warnings[0]


def test_inactive_and_out_of_period_rules_are_ignored(rules) -> None:
    base, additional = rules
    inactive = base[0].model_copy(
        update={"rule_code": "INACTIVE", "is_active": False}
    )
    expired = base[0].model_copy(
        update={
            "rule_code": "EXPIRED",
            "effective_from": date(2026, 1, 1),
            "effective_to": date(2026, 7, 20),
        }
    )
    service = ApprovalRuleService(
        InMemoryApprovalRuleRepository(
            [inactive, expired],
            additional,
        )
    )

    result = service.evaluate(
        ApprovalContext(
            amount="1",
            budget_status="budgeted",
            evaluation_date=date(2026, 7, 23),
        )
    )

    assert result.status == "no_matching_rule"


def test_rule_seed_is_valid() -> None:
    assert validate_approval_rules() == []
