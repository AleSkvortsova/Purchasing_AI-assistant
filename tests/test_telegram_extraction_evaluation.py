import json
from pathlib import Path

from app.bot.normalization import NaturalDateParser
from app.bot.parser import DeterministicIntakeParser
from app.extraction.exceptions import ApprovalExtractionProviderError
from app.extraction.intake import TelegramIntakeExtractionService
from app.extraction.models import RawApprovalExtraction
from app.extraction.provider import FakeApprovalExtractionProvider
from app.intake.models import RequestDraftData
from app.intake.service import RequestIntakeService
from scripts.evaluate_telegram_extraction import (
    REFERENCE_DATE,
    _print_report,
    evaluate_cases,
)

CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "evaluation"
    / "telegram_intake_holdout.json"
)


def _cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


class SequenceProvider:
    def __init__(self, results: list[RawApprovalExtraction | Exception]) -> None:
        self.results = list(results)
        self.calls = 0
        self.last_metadata = {"provider": "sequence"}

    def extract(self, text: str) -> RawApprovalExtraction:
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result.model_copy(deep=True)


def _evidence_error() -> ApprovalExtractionProviderError:
    return ApprovalExtractionProviderError(
        "OpenAI structured output failed evidence validation",
        error_type="ApprovalEvidenceValidationError",
        diagnostic_code="evidence_validation_failed",
        validation_errors=["item_name: evidence not present in input"],
        validation_error_codes={"item_name": "unsupported_evidence"},
    )


def _provider_cases() -> list[dict]:
    template = {
        "context": {},
        "expected_missing_fields": [],
        "expected_next_question": None,
        "acceptable_text_fields": {},
        "expected_null_fields": [],
        "critical_fields": [],
        "tags": ["provider_failure"],
    }
    return [
        {
            **template,
            "case_id": "evidence_failure_case",
            "input": "TOP_SECRET_FULL_USER_TEXT",
            "expected_fields": {"procurement_type": "service"},
        },
        {
            **template,
            "case_id": "case_after_failure",
            "input": "Купить кондиционер",
            "expected_fields": {"procurement_type": "goods"},
            "acceptable_text_fields": {"item_name": ["кондиционер"]},
        },
    ]


def _successful_raw() -> RawApprovalExtraction:
    return RawApprovalExtraction(
        procurement_type_raw="goods",
        item_name_raw="кондиционер",
        confidence_by_field={"procurement_type": 0.99, "item_name": 0.99},
        evidence_by_field={
            "procurement_type": "Купить",
            "item_name": "кондиционер",
        },
    )


def _sequence_service(provider: SequenceProvider) -> TelegramIntakeExtractionService:
    return TelegramIntakeExtractionService(
        provider,
        date_parser=NaturalDateParser(today_provider=lambda: REFERENCE_DATE),
    )


def test_telegram_holdout_has_required_shape_and_at_least_30_cases() -> None:
    cases = _cases()

    assert len(cases) >= 30
    assert len({case["case_id"] for case in cases}) == len(cases)
    required = {
        "input",
        "context",
        "expected_fields",
        "expected_missing_fields",
        "expected_next_question",
        "acceptable_text_fields",
        "critical_fields",
        "tags",
    }
    assert all(required <= case.keys() for case in cases)
    tags = {tag for case in cases for tag in case["tags"]}
    assert {
        "word_quantity",
        "countable_goods",
        "measure_unit",
        "packaging_unit",
        "capacity",
        "relative_date",
        "frequency",
        "duration",
        "multiple_positions",
    } <= tags


def test_evaluator_reports_scalar_accuracy_and_hallucination_metrics(
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    cases = [
        {
            "case_id": "scalar_metrics",
            "input": "Купить три контейнера через неделю",
            "context": {},
            "expected_fields": {
                "quantity": "3",
                "unit": "шт.",
                "desired_delivery_date": "2026-08-05",
            },
            "acceptable_text_fields": {},
            "expected_null_fields": [],
            "expected_missing_fields": [],
            "expected_next_question": None,
            "critical_fields": ["quantity", "unit", "desired_delivery_date"],
            "tags": ["metrics"],
        }
    ]

    metrics, failures = evaluate_cases(cases, mode="rule")

    assert failures == []
    assert metrics["quantity_exact_match"] == 1
    assert metrics["unit_exact_match"] == 1
    assert metrics["date_exact_match"] == 1
    assert "hallucinated_quantity_rate" in metrics
    assert "hallucinated_unit_rate" in metrics
    assert "hallucinated_date_rate" in metrics


def test_new_deterministic_scalar_holdout_cases_match_without_network(
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    new_tags = {
        "word_quantity",
        "measure_unit",
        "packaging_unit",
        "capacity",
        "word_date",
        "frequency",
        "duration",
        "multiple_positions",
    }
    cases = [case for case in _cases() if new_tags & set(case["tags"])]

    metrics, failures = evaluate_cases(cases, mode="rule")

    assert failures == []
    assert metrics["quantity_exact_match"] == 1
    assert metrics["unit_exact_match"] == 1
    assert metrics["date_exact_match"] == 1
    assert metrics["hallucinated_quantity_rate"] == 0
    assert metrics["hallucinated_unit_rate"] == 0
    assert metrics["hallucinated_date_rate"] == 0


def test_fake_hybrid_evaluation_resolves_furniture_assembly_case(
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    case = next(
        case
        for case in _cases()
        if case["case_id"] == "service_furniture_assembly"
    )
    raw = RawApprovalExtraction(
        amount_raw="до 20 000 рублей",
        procurement_type_raw="service",
        item_name_raw="сборка мебели",
        specifications_raw="собрать 3 шкафа и 2 офисных стола",
        amount_modifier_raw="maximum",
        billing_period_raw="one_time",
        desired_delivery_date_raw="на 6 августа",
        category_raw="S15",
        confidence_by_field={
            "amount": 0.99,
            "procurement_type": 0.99,
            "item_name": 0.95,
            "specifications": 0.95,
            "desired_delivery_date": 0.99,
            "category": 0.90,
        },
        evidence_by_field={
            "amount": "до 20 000 рублей",
            "procurement_type": "услуги по сборке",
            "item_name": "сборке",
            "specifications": "сборке 3 шкафов и 2 офисных столов",
            "desired_delivery_date": "на 6 августа",
            "category": "сборке",
        },
    )
    provider = FakeApprovalExtractionProvider(raw)
    structured = TelegramIntakeExtractionService(
        provider,
        date_parser=NaturalDateParser(today_provider=lambda: REFERENCE_DATE),
    )

    metrics, failures = evaluate_cases(
        [case], mode="hybrid", structured=structured
    )

    assert provider.calls == 1
    assert failures == []
    assert metrics["procurement_type_accuracy"] == 1
    assert metrics["amount_exact_match"] == 1
    assert metrics["date_exact_match"] == 1
    assert metrics["hallucinated_field_rate"] == 0


def _goods_furniture_raw() -> RawApprovalExtraction:
    return RawApprovalExtraction(
        procurement_type_raw="goods",
        item_name_raw="шкафы",
        quantity_raw="5",
        specifications_raw="для переговорных",
        category_raw="G02",
        confidence_by_field={
            "procurement_type": 0.99,
            "item_name": 0.99,
            "quantity": 0.99,
            "specifications": 0.95,
            "category": 0.95,
        },
        evidence_by_field={
            "procurement_type": "Купить",
            "item_name": "шкафов",
            "quantity": "5 шкафов",
            "specifications": "для переговорных",
            "category": "шкафов",
        },
    )


def test_goods_furniture_debug_confirms_specs_and_amount_question() -> None:
    case = next(case for case in _cases() if case["case_id"] == "goods_furniture")
    dates = NaturalDateParser(today_provider=lambda: REFERENCE_DATE)
    structured = TelegramIntakeExtractionService(
        FakeApprovalExtractionProvider(_goods_furniture_raw()),
        date_parser=dates,
    )
    deterministic = DeterministicIntakeParser(date_parser=dates).parse(
        case["input"]
    )
    resolution = structured.resolve_message(
        case["input"],
        RequestDraftData(),
        None,
        deterministic,
        source_kind="initial_description",
        merge_deterministic=True,
        fallback_on_error=True,
    )
    result = RequestIntakeService().process_step(
        RequestDraftData(), resolution.update
    )
    diagnostic = {
        "input": case["input"],
        "accepted_fields": sorted((resolution.update or {}).values),
        "missing_fields": result.completeness.missing_fields,
        "next_question": result.next_question.field_code,
        "specifications": result.draft.specifications,
        "specifications_completed": (
            "specifications" in result.completeness.completed_fields
        ),
    }

    assert diagnostic["specifications"] == "для переговорных"
    assert diagnostic["specifications_completed"] is True
    assert "specifications" not in diagnostic["missing_fields"]
    assert "amount" in diagnostic["missing_fields"]
    assert diagnostic["next_question"] == "amount"


def test_other_valid_question_order_is_not_completeness_failure() -> None:
    case = {
        "case_id": "valid_alternative_question_order",
        "input": "Дополнительных данных пока нет",
        "context": {
            "procurement_type": "goods",
            "item_name": "офисные шкафы",
            "category_code": "G02",
            "quantity": "5",
            "unit": "шт.",
        },
        "expected_fields": {},
        "acceptable_text_fields": {},
        "expected_null_fields": [],
        "expected_missing_fields": ["specifications", "amount"],
        "expected_next_question": "amount",
        "critical_fields": [],
        "tags": ["completeness", "question_order"],
    }

    metrics, failures = evaluate_cases(
        [case],
        mode="fake",
        structured=TelegramIntakeExtractionService(
            FakeApprovalExtractionProvider(RawApprovalExtraction())
        ),
    )

    assert metrics["missing_fields_correctness"] == 1
    assert metrics["completeness_decision_accuracy"] == 1
    assert metrics["next_question_validity_accuracy"] == 1
    assert metrics["next_question_order_exact_match"] == 0
    assert metrics["unnecessary_question_rate"] == 0
    assert metrics["missed_question_rate"] == 0
    assert failures[0]["mismatch_types"] == ["question_order_difference"]


def test_short_integration_item_is_valid_when_details_are_preserved() -> None:
    case = next(
        case for case in _cases() if case["case_id"] == "service_integration"
    )
    raw = RawApprovalExtraction(
        procurement_type_raw="service",
        item_name_raw="разработка интеграции",
        specifications_raw="CRM с телефонией",
        amount_raw="1,2 млн рублей",
        desired_delivery_date_raw="к 1 сентября",
        category_raw="S05",
        confidence_by_field={
            "procurement_type": 0.99,
            "item_name": 0.99,
            "specifications": 0.99,
            "amount": 0.99,
            "desired_delivery_date": 0.99,
            "category": 0.99,
        },
        evidence_by_field={
            "procurement_type": "Разработать",
            "item_name": "Разработать интеграцию",
            "specifications": "CRM с телефонией",
            "amount": "1,2 млн рублей",
            "desired_delivery_date": "к 1 сентября",
            "category": "интеграцию CRM с телефонией",
        },
    )

    metrics, failures = evaluate_cases(
        [case],
        mode="hybrid",
        structured=TelegramIntakeExtractionService(
            FakeApprovalExtractionProvider(raw),
            date_parser=NaturalDateParser(
                today_provider=lambda: REFERENCE_DATE
            ),
        ),
    )

    assert failures == []
    assert metrics["field_level_recall"] == 1


def test_office_cleaning_is_accepted_for_cleaning_case() -> None:
    case = next(
        case
        for case in _cases()
        if case["case_id"] == "service_location_purpose"
    )
    raw = RawApprovalExtraction(
        procurement_type_raw="service",
        item_name_raw="уборка офиса",
        delivery_location_raw="в новом офисе на Тверской",
        category_raw="S02",
        confidence_by_field={
            "procurement_type": 0.99,
            "item_name": 0.99,
            "delivery_location": 0.99,
            "category": 0.99,
        },
        evidence_by_field={
            "procurement_type": "клининг",
            "item_name": "клининг",
            "delivery_location": "в новом офисе на Тверской",
            "category": "клининг",
        },
    )

    _, failures = evaluate_cases(
        [case],
        mode="hybrid",
        structured=TelegramIntakeExtractionService(
            FakeApprovalExtractionProvider(raw)
        ),
    )

    assert failures == []


def test_rule_evaluation_reports_all_required_metrics() -> None:
    metrics, _ = evaluate_cases(_cases()[:3], mode="rule")

    assert {
        "procurement_type_accuracy",
        "category_accuracy",
        "field_level_precision",
        "field_level_recall",
        "critical_field_exact_match",
        "completeness_decision_accuracy",
        "missing_fields_correctness",
        "next_question_validity_accuracy",
        "next_question_order_exact_match",
        "unnecessary_question_rate",
        "missed_field_rate",
        "hallucinated_field_rate",
    } <= metrics.keys()


def test_openai_provider_error_is_failed_case_without_fallback() -> None:
    provider = SequenceProvider([_evidence_error(), _successful_raw()])

    metrics, failures = evaluate_cases(
        _provider_cases(),
        mode="openai",
        structured=_sequence_service(provider),
    )

    assert provider.calls == 2
    assert metrics["provider_call_count"] == 2
    assert metrics["provider_success_count"] == 1
    assert metrics["provider_failure_count"] == 1
    assert metrics["evidence_validation_failure_count"] == 1
    assert metrics["fallback_count"] == 0
    assert metrics["fallback_rate"] == 0
    assert metrics["procurement_type_accuracy"] == 0.5
    failure = next(item for item in failures if item["provider_failed"])
    assert failure == {
        "case_id": "evidence_failure_case",
        "mode": "openai",
        "stage": "openai_provider",
        "error": "ApprovalExtractionProviderError",
        "reason": "OpenAI structured output failed evidence validation",
        "error_type": "ApprovalEvidenceValidationError",
        "error_code": "evidence_validation_failed",
        "validation_error_codes": {"item_name": "unsupported_evidence"},
        "provider_failed": True,
        "fallback_used": False,
        "mismatches": [
            "procurement_type: expected 'service', got None"
        ],
        "mismatch_types": ["missing_field"],
    }


def test_hybrid_provider_error_uses_shared_fallback_and_continues() -> None:
    provider = SequenceProvider([_evidence_error(), _successful_raw()])
    service = _sequence_service(provider)

    metrics, failures = evaluate_cases(
        _provider_cases(), mode="hybrid", structured=service
    )

    assert provider.calls == 2
    assert metrics["provider_success_count"] == 1
    assert metrics["provider_failure_count"] == 1
    assert metrics["evidence_validation_failure_count"] == 1
    assert metrics["fallback_count"] == 1
    assert metrics["fallback_rate"] == 0.5
    failure = next(item for item in failures if item["provider_failed"])
    assert failure["case_id"] == "evidence_failure_case"
    assert failure["stage"] == "openai_provider"
    assert failure["error_code"] == "evidence_validation_failed"
    assert failure["fallback_used"] is True
    assert not any(item["case_id"] == "case_after_failure" for item in failures)


def test_rule_mode_never_calls_configured_provider() -> None:
    provider = SequenceProvider([_successful_raw()])

    metrics, _ = evaluate_cases(
        _provider_cases()[:1],
        mode="rule",
        structured=_sequence_service(provider),
    )

    assert provider.calls == 0
    assert metrics["provider_call_count"] == 0
    assert metrics["fallback_count"] == 0


def test_fake_mode_is_deterministic_without_fallback() -> None:
    provider = FakeApprovalExtractionProvider(_successful_raw())

    metrics, _ = evaluate_cases(
        _provider_cases()[1:],
        mode="fake",
        structured=TelegramIntakeExtractionService(provider),
    )

    assert provider.calls == 1
    assert metrics["provider_success_count"] == 1
    assert metrics["provider_failure_count"] == 0
    assert metrics["fallback_count"] == 0


def test_failure_report_is_safe_and_optional(capsys) -> None:
    provider = SequenceProvider([_evidence_error(), _successful_raw()])
    metrics, failures = evaluate_cases(
        _provider_cases(),
        mode="hybrid",
        structured=_sequence_service(provider),
    )

    _print_report(
        "hybrid", metrics, failures, show_failures=False, json_output=False
    )
    compact = capsys.readouterr().out
    assert "provider_failure_count: 1" in compact
    assert "evidence_failure_case" not in compact
    assert "TOP_SECRET_FULL_USER_TEXT" not in compact

    _print_report(
        "hybrid", metrics, failures, show_failures=True, json_output=False
    )
    detailed = capsys.readouterr().out
    assert '"case_id": "evidence_failure_case"' in detailed
    assert '"stage": "openai_provider"' in detailed
    assert '"error_code": "evidence_validation_failed"' in detailed
    assert "TOP_SECRET_FULL_USER_TEXT" not in detailed


def test_evaluator_distinguishes_semantic_error_from_normalized_variant() -> None:
    semantic_case = {
        **_provider_cases()[1],
        "case_id": "semantic_error",
        "expected_fields": {"procurement_type": "service"},
        "acceptable_text_fields": {},
    }
    metrics, failures = evaluate_cases(
        [semantic_case],
        mode="fake",
        structured=TelegramIntakeExtractionService(
            FakeApprovalExtractionProvider(_successful_raw())
        ),
    )
    assert metrics["procurement_type_accuracy"] == 0
    assert failures[0]["mismatch_types"] == ["semantic_error"]

    normalized_case = {
        **_provider_cases()[1],
        "case_id": "normalized_variant",
        "expected_fields": {"procurement_type": "goods"},
        "acceptable_text_fields": {"item_name": ["кондиционера"]},
    }
    _, normalized_failures = evaluate_cases(
        [normalized_case],
        mode="fake",
        structured=TelegramIntakeExtractionService(
            FakeApprovalExtractionProvider(_successful_raw())
        ),
    )
    assert normalized_failures == []
