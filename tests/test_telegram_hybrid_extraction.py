from datetime import date
from uuid import UUID

import pytest

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.categories import DeterministicCategoryClassifier
from app.bot.keyboards import MENU_EXAMPLES, MENU_HELP, MENU_NEW, encode_callback
from app.bot.normalization import NaturalDateParser
from app.bot.parser import DeterministicIntakeParser
from app.core.config import Settings
from app.extraction.intake import (
    TelegramIntakeExtractionService,
    merge_intake_candidates,
    normalize_service_item_name,
)
from app.extraction.models import RawApprovalExtraction
from app.extraction.normalization import compact_category_reference
from app.extraction.provider import (
    FakeApprovalExtractionProvider,
    _evidence_validation_issues,
)
from app.intake.models import IntakeFieldUpdate, IntakeStatus
from app.intake.service import RequestIntakeService
from app.intake_persistence.repositories import InMemoryIntakePersistenceRepository
from app.intake_persistence.service import PersistentIntakeOrchestrator

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _adapter(raw: RawApprovalExtraction, *, error: Exception | None = None):
    provider = FakeApprovalExtractionProvider(raw, error=error)
    dates = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(),
        RequestIntakeService(),
    )
    structured = TelegramIntakeExtractionService(
        provider,
        date_parser=dates,
    )
    adapter = TelegramIntakeAdapter(
        intake,
        parser=DeterministicIntakeParser(date_parser=dates),
        structured_extractor=structured,
        extraction_mode="hybrid",
    )
    return intake, provider, adapter


def _assembly_raw() -> RawApprovalExtraction:
    return RawApprovalExtraction(
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
            "category": 0.80,
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


def test_furniture_assembly_hybrid_scenario_is_service_with_work_volume() -> None:
    intake, provider, adapter = _adapter(_assembly_raw())
    text = (
        "Нужно заказать услуги по сборке 3 шкафов и 2 офисных столов "
        "на 6 августа, бюджет до 20 000 рублей"
    )

    outcome = adapter.handle_text(USER_ID, 1001, 501, text)
    draft = outcome.result.intake_result.draft

    assert provider.calls == 1
    assert draft.procurement_type == "service"
    assert draft.item_name == "сборка мебели"
    assert draft.category_code == "S15"
    assert draft.quantity is None
    assert draft.unit is None
    assert "3 шкафа" in (draft.specifications or "")
    assert "2 офисных стола" in (draft.specifications or "")
    assert draft.desired_delivery_date == date(2026, 8, 6)
    assert draft.amount == 20000
    assert draft.field_states["amount"].evidence == "amount_modifier=maximum"
    assert outcome.result.intake_result.next_question is not None
    assert outcome.result.intake_result.next_question.field_code not in {
        "quantity",
        "unit",
    }

    replay = adapter.handle_text(USER_ID, 1001, 501, text)
    assert replay.result.request_id == outcome.result.request_id
    assert provider.calls == 1
    assert intake.get_active_session(USER_ID).request_id == outcome.result.request_id


def test_hybrid_provider_failure_falls_back_without_losing_message() -> None:
    intake, provider, adapter = _adapter(
        RawApprovalExtraction(),
        error=RuntimeError("provider unavailable"),
    )

    outcome = adapter.handle_text(
        USER_ID,
        1001,
        502,
        "Нужно купить 5 шкафов",
    )

    assert provider.calls == 1
    assert outcome.result.intake_result.draft.procurement_type is None
    assert outcome.result.intake_result.draft.category_code is None
    assert outcome.result.intake_result.draft.item_name is None
    assert outcome.result.intake_result.draft.quantity == 5
    assert outcome.result.intake_result.next_question.field_code == (
        "procurement_type"
    )
    assert intake.get_active_session(USER_ID).request_id == outcome.result.request_id


def test_conservative_fallback_keeps_numbers_but_not_semantic_guesses() -> None:
    _, provider, adapter = _adapter(
        RawApprovalExtraction(),
        error=RuntimeError("provider unavailable"),
    )

    outcome = adapter.handle_text(
        USER_ID,
        1001,
        503,
        "Нужно что-то для мебели на 6 августа, бюджет до 20 000 рублей",
    )
    draft = outcome.result.intake_result.draft

    assert provider.calls == 1
    assert draft.amount == 20000
    assert draft.desired_delivery_date == date(2026, 8, 6)
    assert draft.procurement_type is None
    assert draft.category_code is None
    assert draft.item_name is None
    assert outcome.result.intake_result.next_question.field_code == (
        "procurement_type"
    )


def test_service_item_name_keeps_action_from_source_text() -> None:
    raw = RawApprovalExtraction(
        procurement_type_raw="service",
        item_name_raw="кондиционер",
        category_raw="S01",
        confidence_by_field={
            "procurement_type": 0.99,
            "item_name": 0.95,
            "category": 0.90,
        },
        evidence_by_field={
            "procurement_type": "Установить",
            "item_name": "кондиционер",
            "category": "Установить кондиционер",
        },
    )
    _, _, adapter = _adapter(raw)

    outcome = adapter.handle_text(
        USER_ID, 1001, 504, "Установить кондиционер"
    )

    assert outcome.result.intake_result.draft.item_name == "установка кондиционера"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("установка — установить кондиционер", "установка кондиционера"),
        ("настройка — настроить систему ЭДО", "настройка системы ЭДО"),
        ("разработка — разработать интеграцию", "разработка интеграции"),
        ("организация — организовать мероприятие", "организация мероприятия"),
        ("уборка — клининг", "клининг"),
        (
            "настройка сервера — резервное копирование",
            "настройка сервера — резервное копирование",
        ),
    ],
)
def test_service_item_name_removes_only_duplicated_action(
    source: str,
    expected: str,
) -> None:
    assert normalize_service_item_name(source) == expected


def test_service_subject_is_not_accepted_as_desired_result() -> None:
    raw = RawApprovalExtraction(
        procurement_type_raw="service",
        item_name_raw="установка кондиционеров",
        desired_result_raw="установить кондиционеры",
        confidence_by_field={
            "procurement_type": 0.99,
            "item_name": 0.99,
            "desired_result": 0.99,
        },
        evidence_by_field={
            "procurement_type": "установить",
            "item_name": "установить кондиционеры",
            "desired_result": "установить кондиционеры",
        },
    )
    service = TelegramIntakeExtractionService(
        FakeApprovalExtractionProvider(raw)
    )

    extracted = service.extract(
        "Нужно установить кондиционеры",
        None,
        None,
        source_kind="initial_description",
    )

    assert extracted.update.values["item_name"] == "установка кондиционеров"
    assert "desired_result" not in extracted.update.values
    assert "desired_result" in extracted.rejected_fields


def test_meaningful_desired_result_is_preserved() -> None:
    raw = RawApprovalExtraction(
        procurement_type_raw="service",
        item_name_raw="уборка офиса",
        desired_result_raw="чистые рабочие зоны после каждой уборки",
        confidence_by_field={
            "procurement_type": 0.99,
            "item_name": 0.99,
            "desired_result": 0.99,
        },
        evidence_by_field={
            "procurement_type": "уборка",
            "item_name": "уборка офиса",
            "desired_result": "чистые рабочие зоны после каждой уборки",
        },
    )
    service = TelegramIntakeExtractionService(
        FakeApprovalExtractionProvider(raw)
    )

    extracted = service.extract(
        "Нужна уборка офиса: чистые рабочие зоны после каждой уборки",
        None,
        None,
        source_kind="initial_description",
    )

    assert extracted.update.values["desired_result"] == (
        "чистые рабочие зоны после каждой уборки"
    )


def test_hybrid_keeps_openai_semantics_and_deterministic_exact_values() -> None:
    deterministic = IntakeFieldUpdate(
        values={
            "procurement_type": "goods",
            "category_code": "G02",
            "item_name": "шкафы",
            "amount": 20000,
            "desired_delivery_date": date(2026, 8, 6),
        }
    )
    structured = IntakeFieldUpdate(
        values={
            "procurement_type": "service",
            "category_code": "S15",
            "item_name": "сборка мебели",
        }
    )

    merged = merge_intake_candidates(deterministic, structured)

    assert merged.values == {
        "procurement_type": "service",
        "category_code": "S15",
        "item_name": "сборка мебели",
        "amount": 20000,
        "desired_delivery_date": date(2026, 8, 6),
    }


def test_hybrid_preserves_service_object_count_and_full_location() -> None:
    text = (
        "Нужно организовать заправку четырех картриджей для офисных "
        "принтеров, офис на Гражданском, не позднее 10 августа"
    )
    dates = NaturalDateParser(today_provider=lambda: date(2026, 7, 29))
    deterministic = DeterministicIntakeParser(date_parser=dates).parse(text)
    structured = IntakeFieldUpdate(
        values={
            "procurement_type": "service",
            "item_name": "заправка картриджей",
            "specifications": "для офисных принтеров",
            "delivery_location": "офис на Гражданском",
        }
    )

    merged = merge_intake_candidates(deterministic, structured)

    assert "четырех картриджей" in merged.values["specifications"]
    assert "офисных принтеров" in merged.values["specifications"]
    assert merged.values["delivery_location"] == "офис на Гражданском"
    assert merged.values["desired_delivery_date"] == date(2026, 8, 10)


def test_hybrid_prefers_longer_supported_location() -> None:
    merged = merge_intake_candidates(
        IntakeFieldUpdate(
            values={
                "delivery_location": "в переговорной на Гражданском",
            }
        ),
        IntakeFieldUpdate(
            values={
                "procurement_type": "service",
                "delivery_location": "в переговорной",
            }
        ),
    )

    assert merged.values["delivery_location"] == (
        "в переговорной на Гражданском"
    )


def test_hybrid_vetoes_provider_quantity_for_multiple_goods_positions() -> None:
    deterministic = DeterministicIntakeParser().parse(
        "Купить 3 шкафа и 2 стола"
    )
    structured = IntakeFieldUpdate(
        values={
            "procurement_type": "goods",
            "item_name": "шкафы и столы",
            "quantity": 5,
            "unit": "шт.",
        }
    )

    merged = merge_intake_candidates(deterministic, structured)

    assert deterministic.suppressed_extraction_fields == ["quantity", "unit"]
    assert "quantity" not in merged.values
    assert "unit" not in merged.values


def test_hybrid_keeps_inferred_piece_unit_when_structured_unit_is_null() -> None:
    deterministic = DeterministicIntakeParser().parse(
        "Закажите семь тарелок"
    )
    structured = IntakeFieldUpdate(
        values={
            "procurement_type": "goods",
            "item_name": "тарелки",
            "quantity": 7,
        },
        evidence_by_field={"quantity": "семь тарелок"},
    )

    merged = merge_intake_candidates(deterministic, structured)

    assert deterministic.values["unit"] == "шт."
    assert "unit" not in structured.values
    assert merged.values["unit"] == "шт."


def test_hybrid_explicit_structured_unit_beats_inferred_piece_unit() -> None:
    deterministic = IntakeFieldUpdate(
        values={"quantity": 3, "unit": "шт."},
        evidence_by_field={"quantity": "три емкости", "unit": "три емкости"},
    )
    structured = IntakeFieldUpdate(
        values={"procurement_type": "goods", "quantity": 3, "unit": "л"},
        evidence_by_field={"quantity": "3 литра", "unit": "3 литра"},
    )

    merged = merge_intake_candidates(deterministic, structured)

    assert merged.values["unit"] == "л"
    assert merged.evidence_by_field["unit"] == "3 литра"


def test_g09_trace_fills_only_missing_typed_exact_category() -> None:
    text = "Купить моющие средства и перчатки для уборки"
    classifier = DeterministicCategoryClassifier()
    before_type = classifier.classify(text)
    after_type = classifier.classify(text, "goods")
    deterministic = DeterministicIntakeParser(
        category_classifier=classifier
    ).parse(text)
    raw = RawApprovalExtraction(
        procurement_type_raw="goods",
        category_raw=None,
        item_name_raw="моющие средства и перчатки",
        confidence_by_field={
            "procurement_type": 0.99,
            "item_name": 0.99,
        },
        evidence_by_field={
            "procurement_type": "Купить",
            "item_name": "моющие средства и перчатки",
        },
    )
    service = TelegramIntakeExtractionService(
        FakeApprovalExtractionProvider(raw)
    )
    resolution = service.resolve_message(
        text,
        None,
        None,
        deterministic,
        source_kind="initial_description",
        merge_deterministic=True,
        fallback_on_error=True,
    )
    trace = {
        "input": text,
        "provider_type": raw.procurement_type_raw,
        "provider_category": raw.category_raw,
        "before_type": before_type.candidates,
        "after_type": after_type.category_code,
        "compact_g09": next(
            line
            for line in compact_category_reference().splitlines()
            if "G09" in line
        ),
        "provider_category_evidence": raw.evidence_by_field.get("category"),
        "evidence_issues": _evidence_validation_issues(text, raw),
        "deterministic_category": deterministic.values.get("category_code"),
        "merged_category": resolution.update.values.get("category_code"),
    }

    assert set(trace["before_type"]) == {"G09", "S02"}
    assert trace["after_type"] == "G09"
    assert "моющие" in trace["compact_g09"]
    assert trace["provider_category"] is None
    assert trace["provider_category_evidence"] is None
    assert trace["evidence_issues"] == {}
    assert trace["deterministic_category"] == "G09"
    assert trace["merged_category"] == "G09"


def test_typed_category_fallback_never_overwrites_openai_category() -> None:
    merged = merge_intake_candidates(
        IntakeFieldUpdate(values={"category_code": "G09"}),
        IntakeFieldUpdate(
            values={"procurement_type": "service", "category_code": "S02"}
        ),
    )

    assert merged.values["category_code"] == "S02"


@pytest.mark.parametrize(
    ("text", "expected_type", "evidence"),
    [
        ("Купить 5 шкафов", "goods", "Купить"),
        ("Собрать 5 шкафов", "service", "Собрать"),
        ("Купить кондиционер", "goods", "Купить"),
        ("Установить кондиционер", "service", "Установить"),
        ("Купить сервер", "goods", "Купить"),
        ("Настроить сервер", "service", "Настроить"),
        ("Купить краску для офиса", "goods", "Купить"),
        ("Покрасить офис", "service", "Покрасить"),
        ("Купить мебель", "goods", "Купить"),
        ("Отремонтировать мебель", "service", "Отремонтировать"),
        ("Заказать доставку 12 паллет", "service", "доставку"),
        ("Купить 12 паллет", "goods", "Купить"),
    ],
)
def test_semantic_type_contrasts_override_object_nouns(
    text: str,
    expected_type: str,
    evidence: str,
) -> None:
    raw = RawApprovalExtraction(
        procurement_type_raw=expected_type,
        item_name_raw="краткий предмет",
        confidence_by_field={"procurement_type": 0.99, "item_name": 0.90},
        evidence_by_field={
            "procurement_type": evidence,
            "item_name": evidence,
        },
    )
    _, provider, adapter = _adapter(raw)

    outcome = adapter.handle_text(USER_ID, 1001, 600, text)
    draft = outcome.result.intake_result.draft

    assert provider.calls == 1
    assert draft.procurement_type == expected_type
    if expected_type == "service":
        assert draft.quantity is None
        assert draft.unit is None
        assert outcome.result.intake_result.next_question.field_code not in {
            "quantity",
            "unit",
        }


def test_incompatible_structured_category_is_rejected() -> None:
    raw = RawApprovalExtraction(
        procurement_type_raw="service",
        category_raw="G02",
        confidence_by_field={"procurement_type": 0.99, "category": 0.99},
        evidence_by_field={
            "procurement_type": "Собрать",
            "category": "шкафов",
        },
    )
    _, _, adapter = _adapter(raw)
    outcome = adapter.handle_text(USER_ID, 1001, 700, "Собрать 5 шкафов")
    assert outcome.result.intake_result.draft.procurement_type == "service"
    assert outcome.result.intake_result.draft.category_code is None


def test_menu_and_start_never_call_structured_extractor() -> None:
    _, provider, adapter = _adapter(_assembly_raw())

    adapter.start_message(USER_ID)
    adapter.handle_menu(USER_ID, MENU_EXAMPLES)
    adapter.handle_menu(USER_ID, MENU_HELP)
    adapter.handle_menu(USER_ID, MENU_NEW)

    assert provider.calls == 0


def test_lifecycle_callback_never_calls_structured_extractor() -> None:
    _, provider, adapter = _adapter(_assembly_raw())

    adapter.handle_callback(
        USER_ID,
        "callback-1",
        encode_callback("confirm", USER_ID, 1),
    )

    assert provider.calls == 0


def test_settings_select_hybrid_with_key_and_rule_without_key() -> None:
    assert Settings(_env_file=None).resolved_telegram_extraction_mode == "rule"
    assert (
        Settings(
            _env_file=None,
            openai_api_key="test-only-key",
        ).resolved_telegram_extraction_mode
        == "hybrid"
    )
    assert (
        Settings(
            _env_file=None,
            openai_api_key="test-only-key",
            telegram_extraction_mode="rule",
        ).resolved_telegram_extraction_mode
        == "rule"
    )

    assert (
        Settings(
            _env_file=None,
            openai_api_key="test-only-key",
            telegram_extraction_mode="openai",
        ).resolved_telegram_extraction_mode
        == "openai"
    )


def test_simple_budget_answer_does_not_call_structured_extractor() -> None:
    intake, provider, adapter = _adapter(_assembly_raw())
    initial = intake.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={
                "procurement_type": "service",
                "item_name": "консультация",
                "description": "консультация",
                "category_code": "S11",
                "specifications": "устная консультация",
                "desired_result": "юридическое заключение",
                "amount": "10000",
            }
        ),
    )
    assert initial.intake_result.next_question.field_code == "budget_status"

    outcome = adapter.handle_text(USER_ID, 1001, 800, "не знаю")

    assert provider.calls == 0
    assert outcome.result.intake_result.draft.budget_status == "unknown"


def test_compound_clarification_can_update_quantity_and_date_once() -> None:
    raw = RawApprovalExtraction(
        quantity_raw="12",
        desired_delivery_date_raw="до 25 августа",
        confidence_by_field={
            "quantity": 0.99,
            "desired_delivery_date": 0.99,
        },
        evidence_by_field={
            "quantity": "12 кресел",
            "desired_delivery_date": "до 25 августа",
        },
    )
    intake, provider, adapter = _adapter(raw)
    initial = intake.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={
                "procurement_type": "goods",
                "item_name": "офисные кресла",
                "category_code": "G02",
            }
        ),
    )
    assert initial.intake_result.next_question.field_code == "quantity"

    outcome = adapter.handle_text(
        USER_ID,
        1001,
        801,
        "Нужно не 10, а 12 кресел и доставка до 25 августа",
    )

    assert provider.calls == 1
    assert outcome.result.intake_result.draft.quantity == 12
    assert outcome.result.intake_result.draft.desired_delivery_date == date(
        2026, 8, 25
    )


def test_air_conditioner_smoke_answer_replaces_proposal_without_conflict() -> None:
    class SequenceProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.last_metadata = {"provider": "fake-sequence"}
            self.results = [
                RawApprovalExtraction(
                    amount_raw="150 тысяч рублей",
                    budget_status_raw="budgeted",
                    procurement_type_raw="service",
                    item_name_raw="установка кондиционеров",
                    specifications_raw=(
                        "два кондиционера в переговорных комнатах"
                    ),
                    desired_result_raw="установить кондиционеры",
                    desired_delivery_date_raw="до 15 августа",
                    delivery_location_raw="в офисе на Невском проспекте",
                    category_raw="S01",
                    confidence_by_field={
                        "amount": 0.99,
                        "budget_status": 0.99,
                        "procurement_type": 0.99,
                        "item_name": 0.99,
                        "specifications": 0.99,
                        "desired_result": 0.99,
                        "desired_delivery_date": 0.99,
                        "delivery_location": 0.99,
                        "category": 0.99,
                    },
                    evidence_by_field={
                        "amount": "150 тысяч рублей",
                        "budget_status": "Закупка предусмотрена бюджетом",
                        "procurement_type": "установить",
                        "item_name": "установить два кондиционера",
                        "specifications": (
                            "два кондиционера в переговорных комнатах"
                        ),
                        "desired_result": "установить кондиционеры",
                        "desired_delivery_date": "до 15 августа",
                        "delivery_location": (
                            "в офисе на Невском проспекте"
                        ),
                        "category": "установить два кондиционера",
                    },
                ),
                RawApprovalExtraction(
                    desired_result_raw=(
                        "кондиционеры работают в соответствии с заявленными "
                        "характеристиками"
                    ),
                    specifications_raw=(
                        "работы нужно проводить в утренние часы"
                    ),
                    confidence_by_field={
                        "desired_result": 0.99,
                        "specifications": 0.99,
                    },
                    evidence_by_field={
                        "desired_result": (
                            "кондиционеры работают в соответствии с "
                            "заявленными характеристиками"
                        ),
                        "specifications": (
                            "работы нужно проводить в утренние часы"
                        ),
                    },
                ),
            ]

        def extract(self, text: str) -> RawApprovalExtraction:
            result = self.results[self.calls]
            self.calls += 1
            return result

    provider = SequenceProvider()
    dates = NaturalDateParser(today_provider=lambda: date(2026, 7, 28))
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(),
        RequestIntakeService(),
    )
    adapter = TelegramIntakeAdapter(
        intake,
        parser=DeterministicIntakeParser(date_parser=dates),
        structured_extractor=TelegramIntakeExtractionService(
            provider,
            date_parser=dates,
        ),
        extraction_mode="hybrid",
    )
    initial_text = (
        "Нужно установить два кондиционера в переговорных комнатах до 15 "
        "августа. Работы провести в офисе на Невском проспекте, "
        "ориентировочный бюджет 150 тысяч рублей. Закупка предусмотрена "
        "бюджетом."
    )
    initial = adapter.handle_text(USER_ID, 1001, 900, initial_text)
    initial_draft = initial.result.intake_result.draft

    assert initial_draft.procurement_type == "service"
    assert initial_draft.item_name == "установка кондиционеров"
    assert "два кондиционера" in initial_draft.specifications
    assert "переговорных комнатах" in initial_draft.specifications
    assert initial_draft.desired_delivery_date == date(2026, 8, 15)
    assert initial_draft.delivery_location == "в офисе на Невском проспекте"
    assert initial_draft.amount == 150000
    assert initial_draft.budget_status == "budgeted"
    assert initial_draft.desired_result is None
    asked_field = initial.result.intake_result.next_question.field_code
    assert asked_field in {
        "description",
        "desired_result",
        "specifications",
    }
    assert initial_draft.field_states["item_name"].confirmed is False
    assert initial_draft.field_states["item_name"].source == "extraction"

    answer_text = (
        "кондиционеры работают в соответствии с заявленными "
        "характеристиками, работы нужно проводить в утренние часы"
    )
    answered = adapter.handle_text(USER_ID, 1001, 901, answer_text)
    draft = answered.result.intake_result.draft

    assert provider.calls == 2
    assert draft.desired_result == (
        "кондиционеры работают в соответствии с заявленными характеристиками"
    )
    assert "два кондиционера" in draft.specifications
    assert "утренние часы" in draft.specifications
    assert draft.conflicts == []
    assert answered.result.intake_result.status != IntakeStatus.CONFLICT
    assert "Подтвердите изменение поля" not in answered.text
    assert draft.field_states["desired_result"].confirmed is True
    assert draft.field_states["desired_result"].source == "user"
    assert answered.result.intake_result.next_question.field_code != asked_field


def test_countable_goods_smoke_extracts_unit_and_word_deadline_once() -> None:
    raw = RawApprovalExtraction(
        amount_raw="1500 рублей",
        budget_status_raw="budgeted",
        procurement_type_raw="goods",
        item_name_raw="банки для печенья",
        quantity_raw="три",
        unit_raw=None,
        specifications_raw="объём каждой 1 литр",
        desired_delivery_date_raw="через неделю",
        delivery_location_raw="в офисе на Невском",
        category_raw="G09",
        confidence_by_field={
            "amount": 0.99,
            "budget_status": 0.99,
            "procurement_type": 0.99,
            "item_name": 0.99,
            "quantity": 0.99,
            "specifications": 0.99,
            "desired_delivery_date": 0.99,
            "delivery_location": 0.99,
            "category": 0.99,
        },
        evidence_by_field={
            "amount": "1500 рублей",
            "budget_status": "Закупка предусмотрена бюджетом",
            "procurement_type": "купить",
            "item_name": "банки для печенья",
            "quantity": "три банки",
            "specifications": "объём каждой 1 литр",
            "desired_delivery_date": "через неделю",
            "delivery_location": "в офис на Невском",
            "category": "банки для печенья",
        },
    )
    _, provider, adapter = _adapter(raw)

    outcome = adapter.handle_text(
        USER_ID,
        1001,
        902,
        "Нужно купить три банки для печенья на кухню, объём каждой 1 литр, "
        "доставить через неделю в офис на Невском, бюджет 1500 рублей. "
        "Закупка предусмотрена бюджетом.",
    )
    draft = outcome.result.intake_result.draft

    assert provider.calls == 1
    assert draft.procurement_type == "goods"
    assert draft.item_name == "банки для печенья"
    assert draft.quantity == 3
    assert draft.unit == "шт."
    assert "1 литр" in draft.specifications
    assert draft.desired_delivery_date == date(2026, 8, 4)
    assert "Невском" in draft.delivery_location
    assert draft.amount == 1500
    assert draft.budget_status == "budgeted"
    assert draft.category_code == "G09"
    assert outcome.result.intake_result.next_question.field_code not in {
        "quantity",
        "unit",
        "desired_delivery_date",
    }
