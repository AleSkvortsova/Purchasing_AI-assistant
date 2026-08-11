from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.extraction.models import (
    ApprovalExtractionResult,
    MoneyExtraction,
    NormalizedApprovalExtraction,
)
from app.intake.card import RequestCardBuilder
from app.intake.completeness import RequestCompletenessService
from app.intake.field_registry import RequestFieldRegistry
from app.intake.merge import RequestMergeService
from app.intake.models import (
    IntakeFieldUpdate,
    IntakeStatus,
    RequestDraftData,
    UpdateSource,
)
from app.intake.questions import NextQuestionSelector
from app.intake.service import RequestIntakeService
from app.intake.validators import IntakeFieldValidator
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from scripts.validate_approval_rules import load_rule_seed


@pytest.mark.parametrize("procurement_type", ["goods", "service"])
def test_canonical_procurement_types_are_accepted(procurement_type: str) -> None:
    assert RequestDraftData(procurement_type=procurement_type).procurement_type == (
        procurement_type
    )


def test_work_is_rejected_by_canonical_draft_and_new_update() -> None:
    with pytest.raises(ValidationError):
        RequestDraftData(procurement_type="work")

    result = RequestIntakeService().process_step(
        RequestDraftData(),
        IntakeFieldUpdate(values={"procurement_type": "work"}),
    )
    assert result.draft.procurement_type is None
    assert "procurement_type" in result.completeness.invalid_fields


@pytest.mark.parametrize(
    ("procurement_type", "category_code"),
    [("goods", "S01"), ("service", "G02")],
)
def test_category_must_match_procurement_type(
    procurement_type: str,
    category_code: str,
) -> None:
    errors = IntakeFieldValidator().validate_draft(
        RequestDraftData(
            procurement_type=procurement_type,
            category_code=category_code,
        )
    )
    assert errors["category_code"] == "Категория не соответствует типу закупки"


def complete_goods(**changes) -> RequestDraftData:
    values = {
        "procurement_type": "goods",
        "category_code": "G03",
        "title": "Мониторы для отдела",
        "item_name": "Монитор",
        "quantity": "10",
        "unit": "шт.",
        "specifications": "27 дюймов, IPS",
        "analogs_allowed": True,
        "amount": "180000",
        "budget_status": "budgeted",
        "desired_delivery_date": date.today() + timedelta(days=30),
        "delivery_location": "Центральный офис",
        "business_justification": "Оснащение рабочих мест",
        "department": "ИТ",
        "contact_person": "Анна Петрова",
    }
    values.update(changes)
    return RequestDraftData.model_validate(values)


def complete_service(**changes) -> RequestDraftData:
    values = {
        "procurement_type": "service",
        "category_code": "S11",
        "title": "Юридические услуги",
        "item_name": "Юридическое сопровождение",
        "description": "Проверка договоров",
        "specifications": "Проверить 20 договоров",
        "desired_result": "Юридические заключения",
        "amount": "600000",
        "budget_status": "budgeted",
        "desired_delivery_date": date.today() + timedelta(days=30),
        "delivery_location": "Удалённо",
        "business_justification": "Снижение юридических рисков",
        "department": "Правовой отдел",
        "contact_person": "Ирина Волкова",
        "single_supplier": True,
        "supplier_name": "ООО Право",
        "single_supplier_justification": "Уникальная экспертиза",
    }
    values.update(changes)
    return RequestDraftData.model_validate(values)


def approval_service() -> ApprovalRuleService:
    _, base, additional = load_rule_seed()
    return ApprovalRuleService(InMemoryApprovalRuleRepository(base, additional))


def test_registry_codes_and_order_are_stable() -> None:
    fields = RequestFieldRegistry().all()
    assert len(fields) == len({item.code for item in fields})
    assert [item.priority for item in fields] == [
        item.priority for item in RequestFieldRegistry().all()
    ]
    assert [item.display_order for item in fields] == sorted(
        item.display_order for item in fields
    )


def test_registry_category_rules_and_unknown_category() -> None:
    registry = RequestFieldRegistry()
    g03 = RequestDraftData(procurement_type="goods", category_code="G03")
    unknown = RequestDraftData(procurement_type="goods", category_code="X99")
    assert registry.is_required(registry.get("analogs_allowed"), g03)  # type: ignore[arg-type]
    assert "analogs_allowed" not in {item.code for item in registry.applicable(unknown)}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("quantity", "1.25", Decimal("1.25")),
        ("amount", "0", Decimal("0")),
        ("analogs_allowed", False, False),
        ("analogs_allowed", True, True),
        ("title", "   ", None),
    ],
)
def test_validator_normalizes_values(field, value, expected) -> None:
    assert IntakeFieldValidator().normalize(field, value) == expected


@pytest.mark.parametrize(("field", "value"), [("quantity", "0"), ("amount", "-0.01")])
def test_validator_rejects_invalid_numbers(field, value) -> None:
    with pytest.raises(ValueError):
        IntakeFieldValidator().normalize(field, value)


def test_conditional_justifications() -> None:
    validator = IntakeFieldValidator()
    restricted = complete_goods(
        preferred_brand="Samsung",
        analogs_allowed=False,
        brand_justification=None,
    )
    assert "brand_justification" in validator.validate_draft(restricted)
    assert "single_supplier" not in validator.validate_draft(restricted)
    assert "urgency_justification" not in validator.validate_draft(
        complete_goods(urgency="P3")
    )
    assert "urgency_justification" in validator.validate_draft(
        complete_goods(urgency="P1")
    )


def test_merge_fill_same_none_false_and_previous_value() -> None:
    service = RequestMergeService()
    first = service.merge(
        RequestDraftData(),
        IntakeFieldUpdate(values={"analogs_allowed": False, "amount": "10"}),
    )
    assert first.draft.analogs_allowed is False
    same = service.merge(
        first.draft, IntakeFieldUpdate(values={"amount": "10", "title": None})
    )
    assert same.applied_changes == []
    corrected = service.merge(
        same.draft,
        IntakeFieldUpdate(values={"amount": "20"}, explicit_correction=True),
    )
    assert corrected.draft.amount == Decimal("20")
    assert corrected.draft.field_states["amount"].previous_value == Decimal("10")


def test_unconfirmed_change_creates_conflict() -> None:
    draft = complete_goods()
    result = RequestMergeService().merge(
        draft, IntakeFieldUpdate(values={"amount": "200000"})
    )
    assert result.draft.amount == Decimal("180000")
    assert result.draft.conflicts[0].field_code == "amount"


def test_extraction_does_not_replace_confirmed_user_value() -> None:
    merger = RequestMergeService()
    user = merger.merge(
        RequestDraftData(), IntakeFieldUpdate(values={"amount": "10"})
    ).draft
    result = merger.merge(
        user,
        IntakeFieldUpdate(values={"amount": "20"}, source=UpdateSource.EXTRACTION),
    )
    assert result.draft.amount == Decimal("10")
    assert result.draft.conflicts


def test_user_answer_replaces_unconfirmed_extraction_without_conflict() -> None:
    merger = RequestMergeService()
    proposed = merger.merge(
        RequestDraftData(),
        IntakeFieldUpdate(
            values={"desired_result": "установить кондиционеры"},
            source=UpdateSource.EXTRACTION,
        ),
    ).draft

    assert proposed.field_states["desired_result"].confirmed is False
    answered = merger.merge(
        proposed,
        IntakeFieldUpdate(
            values={
                "desired_result": (
                    "кондиционеры работают по заявленным характеристикам"
                )
            },
            source=UpdateSource.USER,
            answered_field_code="desired_result",
        ),
    ).draft

    assert answered.conflicts == []
    assert answered.desired_result == (
        "кондиционеры работают по заявленным характеристикам"
    )
    assert answered.field_states["desired_result"].source == UpdateSource.USER
    assert answered.field_states["desired_result"].confirmed is True


def test_direct_answer_does_not_replace_confirmed_user_value() -> None:
    merger = RequestMergeService()
    confirmed = merger.merge(
        RequestDraftData(),
        IntakeFieldUpdate(values={"desired_result": "Рабочая система"}),
    ).draft
    changed = merger.merge(
        confirmed,
        IntakeFieldUpdate(
            values={"desired_result": "Другой результат"},
            answered_field_code="desired_result",
        ),
    ).draft

    assert changed.desired_result == "Рабочая система"
    assert changed.conflicts[0].proposed_value == "Другой результат"


def test_new_service_requirement_enriches_specs_without_conflict() -> None:
    merger = RequestMergeService()
    proposed = merger.merge(
        RequestDraftData(),
        IntakeFieldUpdate(
            values={
                "specifications": (
                    "два кондиционера в переговорных комнатах"
                )
            },
            source=UpdateSource.EXTRACTION,
        ),
    ).draft
    enriched = merger.merge(
        proposed,
        IntakeFieldUpdate(
            values={"specifications": "работы проводить в утренние часы"},
            source=UpdateSource.USER,
            answered_field_code="desired_result",
        ),
    ).draft

    assert enriched.conflicts == []
    assert "два кондиционера" in enriched.specifications
    assert "утренние часы" in enriched.specifications


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [("keep", Decimal("10")), ("accept", Decimal("20"))],
)
def test_pending_conflict_can_be_resolved_once(
    resolution: str,
    expected: Decimal,
) -> None:
    merger = RequestMergeService()
    confirmed = merger.merge(
        RequestDraftData(), IntakeFieldUpdate(values={"amount": "10"})
    ).draft
    conflicted = merger.merge(
        confirmed, IntakeFieldUpdate(values={"amount": "20"})
    ).draft
    conflict_id = conflicted.conflicts[0].id

    resolved = merger.merge(
        conflicted,
        IntakeFieldUpdate(
            resolve_conflict_id=conflict_id,
            conflict_resolution=resolution,
        ),
    ).draft
    replayed = merger.merge(
        resolved,
        IntakeFieldUpdate(
            resolve_conflict_id=conflict_id,
            conflict_resolution=resolution,
        ),
    ).draft

    assert resolved.amount == expected
    assert resolved.conflicts == []
    assert replayed.amount == expected
    assert replayed.conflicts == []


def test_completeness_goods_service_and_conditionals() -> None:
    service = RequestCompletenessService()
    goods = service.evaluate(RequestDraftData(procurement_type="goods"))
    assert "quantity" in goods.missing_fields
    legal = service.evaluate(complete_service())
    assert "quantity" not in legal.required_fields
    assert legal.is_complete
    restricted = service.evaluate(
        complete_goods(
            preferred_brand="Samsung",
            analogs_allowed=False,
            brand_justification=None,
        )
    )
    assert "brand_justification" in restricted.missing_fields


def test_false_is_completed_when_required() -> None:
    result = RequestCompletenessService().evaluate(
        complete_goods(analogs_allowed=False, brand_justification="Стандарт")
    )
    assert "analogs_allowed" in result.completed_fields


def test_question_is_single_stable_and_conflict_first() -> None:
    registry = RequestFieldRegistry()
    completeness = RequestCompletenessService(registry).evaluate(RequestDraftData())
    question = NextQuestionSelector(registry).select(RequestDraftData(), completeness)
    assert question is not None
    assert question.field_code == "procurement_type"
    conflicted = (
        RequestMergeService(registry)
        .merge(complete_goods(), IntakeFieldUpdate(values={"amount": "20"}))
        .draft
    )
    conflict_question = NextQuestionSelector(registry).select(
        conflicted, RequestCompletenessService(registry).evaluate(conflicted)
    )
    assert conflict_question is not None
    assert conflict_question.question_type == "confirmation"


def test_boolean_question_has_options() -> None:
    draft = complete_goods(analogs_allowed=None)
    completeness = RequestCompletenessService().evaluate(draft)
    question = NextQuestionSelector().select(draft, completeness)
    assert question is not None
    assert question.field_code == "analogs_allowed"
    assert question.options == ["Да", "Нет"]


def test_card_formats_decimal_false_and_route_without_duplicates() -> None:
    draft = complete_goods(single_supplier=False)
    route = approval_service().evaluate(
        RequestIntakeService._approval_context(draft)  # type: ignore[arg-type]
    )
    card = RequestCardBuilder().build(draft, route)
    amount = next(
        field
        for section in card.sections
        for field in section.fields
        if field.code == "amount"
    )
    supplier = next(
        field
        for section in card.sections
        for field in section.fields
        if field.code == "single_supplier"
    )
    assert amount.display_value == "180 000 ₽"
    assert supplier.display_value == "Нет"
    assert len(route.final_approvers) == len(set(route.final_approvers))


def test_service_complete_update_builds_context_route_and_card() -> None:
    service = RequestIntakeService(approval_service())
    update = IntakeFieldUpdate(
        values=complete_service().model_dump(
            exclude={"field_states", "conflicts", "warnings"},
            exclude_none=True,
        )
    )
    result = service.process_step(RequestDraftData(), update)
    assert result.status == IntakeStatus.READY_FOR_CONFIRMATION
    assert result.next_question is None
    assert result.approval_context is not None
    assert result.approval_route is not None
    assert "Юридическая служба" in result.approval_route.final_approvers
    assert result.request_card is not None


def test_service_incomplete_returns_one_question_without_card() -> None:
    result = RequestIntakeService().process_step(
        RequestDraftData(), IntakeFieldUpdate(values={"item_name": "Кресло"})
    )
    assert result.status == IntakeStatus.COLLECTING
    assert result.next_question is not None
    assert result.request_card is None
    assert result.metadata == {
        "persistence_performed": False,
        "openai_called": False,
    }


def test_service_change_requires_explicit_correction() -> None:
    service = RequestIntakeService()
    blocked = service.process_step(
        complete_goods(), IntakeFieldUpdate(values={"amount": "200000"})
    )
    assert blocked.status == IntakeStatus.CONFLICT
    assert blocked.approval_context is None
    corrected = service.process_step(
        complete_goods(),
        IntakeFieldUpdate(values={"amount": "200000"}, explicit_correction=True),
    )
    assert corrected.draft.amount == Decimal("200000")


def test_preferred_brand_does_not_imply_single_supplier() -> None:
    result = RequestIntakeService().process_step(
        RequestDraftData(),
        IntakeFieldUpdate(values={"preferred_brand": "Samsung"}),
    )
    assert result.draft.single_supplier is None


def test_full_draft_has_no_next_question() -> None:
    draft = complete_goods()
    completeness = RequestCompletenessService().evaluate(draft)
    assert NextQuestionSelector().select(draft, completeness) is None


def test_title_is_optional_and_card_uses_item_name() -> None:
    draft = complete_goods(title=None)
    completeness = RequestCompletenessService().evaluate(draft)
    assert "title" not in completeness.required_fields
    assert completeness.is_complete
    assert RequestCardBuilder().build(draft).title == "Монитор"


def test_card_title_fallback_order() -> None:
    builder = RequestCardBuilder()
    assert builder.build(complete_goods(title="Мониторы для ИТ")).title == (
        "Мониторы для ИТ"
    )
    assert (
        builder.build(RequestDraftData(category_code="G03")).title == "IT-оборудование"
    )
    assert builder.build(RequestDraftData()).title == "Заявка на закупку"


def test_item_name_remains_required() -> None:
    completeness = RequestCompletenessService().evaluate(
        complete_goods(item_name=None, title=None)
    )
    assert "item_name" in completeness.missing_fields
    assert not completeness.is_complete


@pytest.mark.parametrize(
    "draft",
    [
        complete_goods(
            procurement_type="goods",
            category_code="G05",
            item_name="Лицензии на офисное программное обеспечение",
            specifications="Нужно приобрести готовые лицензии",
            delivery_location=None,
        ),
        complete_service(delivery_location=None, work_on_site=False),
        complete_service(
            category_code="S05",
            item_name="Доработка информационной системы",
            description="Доработать корпоративную систему",
            specifications="Настроить интеграцию",
            desired_result="Работающая интеграция",
            has_data_access=False,
            delivery_location=None,
            work_on_site=False,
        ),
    ],
    ids=[
        "digital-license-g05",
        "remote-legal-service-s11",
        "remote-development-s05",
    ],
)
def test_remote_or_digital_procurement_does_not_require_location(
    draft: RequestDraftData,
) -> None:
    completeness = RequestCompletenessService().evaluate(draft)
    assert "delivery_location" not in completeness.required_fields
    assert completeness.is_complete


@pytest.mark.parametrize(
    "draft",
    [
        complete_goods(delivery_location=None),
        complete_service(
            category_code="S01",
            delivery_location=None,
            work_on_site=True,
        ),
    ],
    ids=["goods", "on-site-service"],
)
def test_goods_and_on_site_service_require_location(draft: RequestDraftData) -> None:
    completeness = RequestCompletenessService().evaluate(draft)
    assert "delivery_location" in completeness.missing_fields


def test_service_on_site_requires_location() -> None:
    completeness = RequestCompletenessService().evaluate(
        complete_service(delivery_location=None, work_on_site=True)
    )
    assert "delivery_location" in completeness.missing_fields


def test_department_and_contact_from_system_are_not_asked() -> None:
    draft = complete_goods(department=None, contact_person=None, title=None)
    result = RequestIntakeService().process_step(
        draft,
        IntakeFieldUpdate(
            values={"department": "ИТ", "contact_person": "Анна Петрова"},
            source=UpdateSource.SYSTEM,
        ),
    )
    assert result.status == IntakeStatus.READY_FOR_CONFIRMATION
    assert result.next_question is None
    assert result.draft.field_states["department"].source == UpdateSource.SYSTEM
    assert result.draft.field_states["contact_person"].source == UpdateSource.SYSTEM


def test_missing_profile_fields_block_readiness_and_are_asked_last() -> None:
    draft = complete_goods(department=None, contact_person=None)
    completeness = RequestCompletenessService().evaluate(draft)
    question = NextQuestionSelector().select(draft, completeness)
    assert not completeness.is_complete
    assert question is not None
    assert question.field_code == "department"

    with_department = RequestIntakeService().process_step(
        draft,
        IntakeFieldUpdate(values={"department": "ИТ"}, source=UpdateSource.SYSTEM),
    )
    assert with_department.next_question is not None
    assert with_department.next_question.field_code == "contact_person"


def test_system_profile_values_are_completed_and_protected_from_extraction() -> None:
    merger = RequestMergeService()
    system = merger.merge(
        RequestDraftData(),
        IntakeFieldUpdate(
            values={"department": "ИТ", "contact_person": "Анна Петрова"},
            source=UpdateSource.SYSTEM,
        ),
    ).draft
    completeness = RequestCompletenessService().evaluate(system)
    assert "department" in completeness.completed_fields
    assert "contact_person" in completeness.completed_fields
    extracted = merger.merge(
        system,
        IntakeFieldUpdate(
            values={"department": "Финансы"},
            source=UpdateSource.EXTRACTION,
        ),
    ).draft
    assert extracted.department == "ИТ"
    assert extracted.conflicts[0].field_code == "department"


def test_initial_question_order_and_category_options_are_stable() -> None:
    service = RequestIntakeService()
    first = service.process_step(RequestDraftData(), IntakeFieldUpdate())
    assert first.next_question is not None
    assert first.next_question.field_code == "procurement_type"
    second = service.process_step(
        RequestDraftData(procurement_type="goods"), IntakeFieldUpdate()
    )
    assert second.next_question is not None
    assert second.next_question.field_code == "item_name"
    third = service.process_step(
        RequestDraftData(procurement_type="goods", item_name="Монитор"),
        IntakeFieldUpdate(),
    )
    assert third.next_question is not None
    assert third.next_question.field_code == "category_code"
    assert "G03 — IT-оборудование" in third.next_question.options

    service_draft = RequestDraftData(
        procurement_type="service", item_name="Юридические услуги"
    )
    service_question = service.process_step(
        service_draft, IntakeFieldUpdate()
    ).next_question
    assert service_question is not None
    assert service_question.field_code == "description"


def test_business_justification_precedes_optional_features() -> None:
    result = RequestIntakeService().process_step(
        complete_goods(business_justification=None, urgency=None),
        IntakeFieldUpdate(),
    )
    assert result.next_question is not None
    assert result.next_question.field_code == "business_justification"
    ready = RequestIntakeService().process_step(
        complete_goods(urgency=None), IntakeFieldUpdate()
    )
    assert ready.status == IntakeStatus.READY_FOR_CONFIRMATION
    assert ready.next_question is None


def test_unknown_budget_is_complete_but_has_unresolved_approval_route() -> None:
    result = RequestIntakeService(approval_service()).process_step(
        complete_goods(budget_status="unknown"),
        IntakeFieldUpdate(),
    )
    assert result.status == IntakeStatus.READY_FOR_CONFIRMATION
    assert result.completeness.is_complete is True
    assert "budget_status" in result.completeness.completed_fields
    assert result.next_question is None
    assert result.approval_context is not None
    assert result.approval_context.budget_status == "unknown"
    assert result.approval_route is not None
    assert result.approval_route.status == "needs_clarification"
    assert result.approval_route.final_approvers == []
    assert result.approval_route.warnings
    assert result.request_card is not None
    budget_fields = [
        field
        for section in result.request_card.sections
        for field in section.fields
        if field.code == "budget_status"
    ]
    assert budget_fields[0].display_value == "Требуется уточнение"


@pytest.mark.parametrize(
    ("brand", "analogs", "justification_required"),
    [
        (None, True, False),
        ("Samsung", True, False),
        ("Samsung", False, True),
    ],
)
def test_g03_brand_rules(
    brand: str | None,
    analogs: bool,
    justification_required: bool,
) -> None:
    draft = complete_goods(
        preferred_brand=brand,
        analogs_allowed=analogs,
        brand_justification=None,
    )
    completeness = RequestCompletenessService().evaluate(draft)
    assert (
        "brand_justification" in completeness.missing_fields
    ) is justification_required
    assert draft.single_supplier is None


def extraction_result(
    *,
    amount=None,
    budget_status="budgeted",
    money=None,
    contradictions=None,
    questions=None,
) -> ApprovalExtractionResult:
    return ApprovalExtractionResult(
        status="conflict" if contradictions else "needs_clarification",
        extraction=NormalizedApprovalExtraction(
            amount=amount,
            budget_status=budget_status,
            source_text="test",
            money=money,
            contradictions=contradictions or [],
        ),
        clarification_questions=questions or [],
        duration_ms=0,
    )


def test_range_amount_keeps_special_question_and_has_no_context() -> None:
    extraction = extraction_result(
        amount=None,
        money=MoneyExtraction(
            min_amount="180000",
            max_amount="220000",
            amount_type="range",
            currency="RUB",
            evidence="от 180 до 220 тысяч",
        ),
        questions=[
            "Какую сумму использовать для определения маршрута согласования: "
            "ожидаемую или максимальную?"
        ],
    )
    result = RequestIntakeService().process_step(
        RequestDraftData(), IntakeFieldUpdate(), extraction
    )
    assert result.status == IntakeStatus.COLLECTING
    assert result.approval_context is None
    assert result.next_question is not None
    assert "ожидаемую или максимальную" in result.next_question.text


def test_budget_conflict_does_not_call_rule_engine() -> None:
    class FailingRuleService:
        def evaluate(self, _context):
            raise AssertionError("rule engine must not be called")

    extraction = extraction_result(
        amount="100",
        budget_status=None,
        contradictions=["Противоречивые сведения о бюджетном статусе"],
        questions=["Закупка предусмотрена бюджетом?"],
    )
    service = RequestIntakeService(FailingRuleService())  # type: ignore[arg-type]
    result = service.process_step(RequestDraftData(), IntakeFieldUpdate(), extraction)
    assert result.status == IntakeStatus.CONFLICT
    assert result.approval_context is None
    assert result.approval_route is None


def test_sequential_updates_preserve_draft() -> None:
    service = RequestIntakeService()
    first = service.process_step(
        RequestDraftData(),
        IntakeFieldUpdate(values={"procurement_type": "goods", "item_name": "Кресло"}),
    )
    second = service.process_step(
        first.draft,
        IntakeFieldUpdate(values={"quantity": "5", "unit": "шт."}),
    )
    assert second.draft.item_name == "Кресло"
    assert second.draft.quantity == Decimal("5")
