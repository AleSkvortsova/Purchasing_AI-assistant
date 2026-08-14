import json
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.categories import DeterministicCategoryClassifier
from app.bot.category_resolution import (
    CategoryClassificationPayload,
    CategoryResolutionService,
    FakeCategoryClassificationProvider,
    build_category_resolution_context,
    category_classification_strict_json_schema,
    category_confirmation_evidence,
    category_draft_context_fingerprint,
    validate_category_classification_schema,
)
from app.intake.field_registry import (
    CATEGORY_DESCRIPTIONS,
    CATEGORY_NAMES,
    CATEGORY_TAXONOMY_VERSION,
)
from app.intake.models import (
    FieldValueState,
    IntakeFieldUpdate,
    RequestDraftData,
    UpdateSource,
)
from app.intake.validators import IntakeFieldValidator
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator

USER_ID = UUID("88888888-8888-4888-8888-888888888888")
GENERALIZATION_DATASET = (
    Path(__file__).parents[1]
    / "data"
    / "evaluation"
    / "category_generalization_cases.json"
)


@pytest.mark.parametrize(
    ("text", "procurement_type", "expected"),
    [
        ("офисное кресло", "goods", "G02"),
        ("ноутбук", "goods", "G03"),
        ("бумага А4", "goods", "G01"),
        ("средства индивидуальной защиты", "goods", "G08"),
        ("уборка офиса", "service", "S02"),
    ],
)
def test_deterministic_exact_does_not_call_category_provider(
    text: str,
    procurement_type: str,
    expected: str,
) -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="unresolved",
            primary_category_code=None,
            alternatives=[],
            confidence="low",
            evidence=None,
            rationale_code="insufficient_context",
        )
    )
    resolver = CategoryResolutionService(
        DeterministicCategoryClassifier(), provider
    )

    result = resolver.resolve(procurement_type, text, text)

    assert result.decision == "deterministic_exact"
    assert result.category_code == expected
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("text", "procurement_type", "code"),
    [
        ("офисные стулья", "goods", "G02"),
        ("подставка под монитор", "goods", "G04"),
        ("промышленный вентилятор", "goods", "G15"),
        ("нестандартная техническая диагностика", "service", "S15"),
    ],
)
def test_unknown_subject_uses_validated_closed_taxonomy_provider(
    text: str,
    procurement_type: str,
    code: str,
) -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code=code,
            alternatives=[],
            confidence="high",
            evidence=text,
            rationale_code="taxonomy_match",
        )
    )
    resolver = CategoryResolutionService(provider=provider)

    result = resolver.resolve(procurement_type, text, text)

    assert result.decision == "llm_exact"
    assert result.category_code is None
    assert result.candidates == (code,)
    assert result.requires_confirmation is True
    assert provider.calls == 1
    request = provider.requests[0]
    assert all(
        item.code.startswith("G" if procurement_type == "goods" else "S")
        for item in request.taxonomy
    )


def test_llm_candidates_are_limited_to_closed_taxonomy() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="candidates",
            primary_category_code=None,
            alternatives=["G04", "G02"],
            confidence="medium",
            evidence="подставка под монитор",
            rationale_code="ambiguous_taxonomy_match",
        )
    )

    result = CategoryResolutionService(provider=provider).resolve(
        "goods", "подставка под монитор", "подставка под монитор"
    )

    assert result.decision == "llm_candidates"
    assert result.candidates == ("G04", "G02")
    assert result.requires_confirmation is True


@pytest.mark.parametrize(
    "payload",
    [
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="S15",
            alternatives=[],
            confidence="high",
            evidence="промышленный вентилятор",
            rationale_code="taxonomy_match",
        ),
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G15",
            alternatives=[],
            confidence="high",
            evidence=None,
            rationale_code="taxonomy_match",
        ),
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G15",
            alternatives=[],
            confidence="high",
            evidence="несуществующий фрагмент",
            rationale_code="taxonomy_match",
        ),
    ],
)
def test_invalid_provider_result_degrades_to_unresolved(
    payload: CategoryClassificationPayload,
) -> None:
    result = CategoryResolutionService(
        provider=FakeCategoryClassificationProvider(payload)
    ).resolve("goods", "промышленный вентилятор", "промышленный вентилятор")

    assert result.decision == "unresolved"
    assert result.category_code is None
    assert result.candidates == ()


@pytest.mark.parametrize(
    ("source_text", "evidence"),
    [
        (
            "Нужна настенная панель для переговорной",
            "настенную панель",
        ),
        ("Нужны офисные стулья", "офисных стульев"),
        (
            "Нужны измерительные приборы",
            "измерительных приборов",
        ),
    ],
)
def test_safe_russian_morphology_is_accepted_for_category_evidence(
    source_text: str,
    evidence: str,
) -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G02",
            alternatives=[],
            confidence="high",
            evidence=evidence,
            rationale_code="taxonomy_match",
        )
    )

    result = CategoryResolutionService(provider=provider).resolve(
        "goods", source_text, source_text
    )

    assert result.decision == "llm_exact"
    assert result.reason_code is None


@pytest.mark.parametrize(
    "evidence",
    [
        "интерактивный дисплей",
        "офисное оборудование",
        "стол для переговорной",
        "оборудование для переговорной",
    ],
)
def test_general_or_semantically_different_evidence_is_rejected(
    evidence: str,
) -> None:
    source_text = "Нужна настенная панель для переговорной"
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G02",
            alternatives=[],
            confidence="high",
            evidence=evidence,
            rationale_code="taxonomy_match",
        )
    )

    result = CategoryResolutionService(provider=provider).resolve(
        "goods", source_text, source_text
    )

    assert result.decision == "unresolved"
    assert result.reason_code == "invalid_evidence"


def test_s01_and_s15_taxonomy_metadata_describes_semantic_boundary() -> None:
    assert CATEGORY_TAXONOMY_VERSION == "intake-categories-v3"
    assert "восстановление работоспособности" in CATEGORY_DESCRIPTIONS["S01"]
    assert "метрологические" in CATEGORY_DESCRIPTIONS["S15"]
    assert "не являются ремонтом" in CATEGORY_DESCRIPTIONS["S15"]


@pytest.mark.parametrize(
    ("description", "expected_code"),
    [
        ("ремонт погрузчика", "S01"),
        ("техническое обслуживание погрузчика", "S01"),
        ("монтаж оборудования", "S01"),
        ("поверка измерительного прибора", "S15"),
        ("калибровка измерительного оборудования", "S15"),
        ("техническая экспертиза", "S15"),
    ],
)
def test_service_boundary_is_expressed_by_closed_taxonomy_metadata(
    description: str,
    expected_code: str,
) -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code=expected_code,
            alternatives=[],
            confidence="high",
            evidence=description,
            rationale_code="taxonomy_match",
        )
    )

    result = CategoryResolutionService(provider=provider).resolve(
        "service", description, description
    )

    assert result.category_code == expected_code or result.candidates == (
        expected_code,
    )
    if result.decision == "deterministic_exact":
        assert provider.calls == 0
    else:
        assert result.decision == "llm_exact"
        request = provider.requests[0]
        descriptions = {item.code: item.description for item in request.taxonomy}
        assert "восстановление работоспособности" in descriptions["S01"]
        assert "метрологические" in descriptions["S15"]


def test_goods_category_from_provider_is_rejected_for_service() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G15",
            alternatives=[],
            confidence="high",
            evidence="нестандартная техническая диагностика",
            rationale_code="taxonomy_match",
        )
    )

    result = CategoryResolutionService(provider=provider).resolve(
        "service",
        "нестандартная техническая диагностика",
        "нестандартная техническая диагностика",
    )

    assert result.decision == "unresolved"
    assert result.reason_code == "invalid_category_code"


def test_provider_failure_degrades_to_unresolved() -> None:
    result = CategoryResolutionService(
        provider=FakeCategoryClassificationProvider(error=RuntimeError("offline"))
    ).resolve("goods", "промышленный вентилятор", "промышленный вентилятор")

    assert result.decision == "unresolved"
    assert result.provider_failed is True


def test_unknown_category_code_is_rejected_by_strict_dto() -> None:
    with pytest.raises(ValidationError):
        CategoryClassificationPayload.model_validate(
            {
                "decision": "exact",
                "primary_category_code": "G99",
                "alternatives": [],
                "confidence": "high",
                "evidence": "неизвестное устройство",
                "rationale_code": "taxonomy_match",
            }
        )


def test_malformed_provider_object_degrades_to_unresolved() -> None:
    provider = Mock()
    provider.classify.return_value = object()

    result = CategoryResolutionService(provider=provider).resolve(
        "goods", "неизвестное устройство", "неизвестное устройство"
    )

    assert result.decision == "unresolved"
    assert result.reason_code == "malformed_provider_result"


def test_category_context_uses_only_category_relevant_canonical_fields() -> None:
    draft = RequestDraftData(
        procurement_type="goods",
        item_name="роутеры",
        description="оборудование для склада",
        specifications="доступ к интернету от провайдера",
        business_justification="обеспечить работу склада",
        amount="80000",
        budget_status="budgeted",
        delivery_location="склад на Невском",
        department="Логистика",
    )
    context = build_category_resolution_context(
        draft,
        IntakeFieldUpdate(),
        "15 сентября",
        include_current_text=False,
    )

    assert context is not None
    assert "роутеры" in context.source_text
    assert "доступ к интернету" in context.source_text
    assert "обеспечить работу склада" in context.source_text
    assert "80000" not in context.source_text
    assert "budgeted" not in context.source_text
    assert "Невском" not in context.source_text
    assert "Логистика" not in context.source_text


def test_category_context_fingerprint_changes_only_for_semantic_context() -> None:
    draft = RequestDraftData(procurement_type="goods", item_name="вентиляторы")
    original = build_category_resolution_context(
        draft,
        IntakeFieldUpdate(),
        "80000р",
        include_current_text=False,
    )
    enriched = build_category_resolution_context(
        draft,
        IntakeFieldUpdate(
            values={"specifications": "для охлаждения производственного помещения"}
        ),
        "для охлаждения производственного помещения",
        include_current_text=True,
    )

    assert original is not None and enriched is not None
    assert original.fingerprint != enriched.fingerprint


def test_taxonomy_covers_it_infrastructure_by_purpose() -> None:
    assert CATEGORY_TAXONOMY_VERSION == "intake-categories-v3"
    assert "инфраструктур" in CATEGORY_DESCRIPTIONS["G03"].casefold()
    assert "сетев" in CATEGORY_DESCRIPTIONS["G03"].casefold()
    assert "рабоч" in CATEGORY_DESCRIPTIONS["G04"].casefold()


def test_taxonomy_distinguishes_engineering_equipment_and_parts() -> None:
    description = CATEGORY_DESCRIPTIONS["G15"].casefold()

    assert "самостоятельн" in description
    assert "оборудован" in description
    assert "запчаст" in description
    assert CATEGORY_NAMES["G15"] == "Инженерное оборудование и запчасти"
    assert "не являющиеся самостоятельным" in CATEGORY_DESCRIPTIONS[
        "G14"
    ].casefold()


@pytest.mark.parametrize(
    ("item_name", "source_text", "provider_code"),
    (
        ("роутер", "роутер для сети офиса", "G03"),
        ("точка доступа", "точка доступа для беспроводной сети", "G03"),
        ("сетевой коммутатор", "сетевой коммутатор инфраструктуры", "G03"),
        ("промышленный вентилятор", "оборудование охлаждения цеха", "G15"),
        ("насос", "самостоятельный промышленный насос", "G15"),
        ("компрессор", "компрессор производственной линии", "G15"),
        ("осушитель воздуха", "осушитель производственного помещения", "G15"),
        ("запасная деталь", "деталь промышленного оборудования", "G15"),
    ),
)
def test_semantic_taxonomy_accepts_infrastructure_and_engineering_classes(
    item_name: str,
    source_text: str,
    provider_code: str,
) -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code=provider_code,
            alternatives=[],
            confidence="high",
            evidence=source_text,
            rationale_code="taxonomy_match",
        )
    )
    result = CategoryResolutionService(provider=provider).resolve(
        "goods", item_name, source_text
    )

    if result.decision == "deterministic_exact":
        assert result.category_code == provider_code
    else:
        assert result.decision == "llm_exact"
        assert result.candidates == (provider_code,)


def test_related_object_does_not_create_deterministic_category_shortcut() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G04",
            alternatives=[],
            confidence="high",
            evidence="док-станция",
            rationale_code="taxonomy_match",
        )
    )
    result = CategoryResolutionService(provider=provider).resolve(
        "goods",
        "док-станция",
        "док-станция для рабочего ноутбука",
    )

    assert provider.calls == 1
    assert result.decision == "llm_exact"
    assert result.candidates == ("G04",)


@pytest.mark.parametrize(
    ("item_name", "source_text", "provider_code", "expected_decision"),
    [
        ("док-станция", "док-станция для ноутбука", "G04", "llm_exact"),
        ("подставка", "подставка под монитор", "G04", "llm_exact"),
        ("чехол", "чехол для ноутбука", None, "unresolved"),
        ("кабель", "кабель для монитора", None, "unresolved"),
    ],
)
def test_related_object_is_not_used_as_subject_category_evidence(
    item_name: str,
    source_text: str,
    provider_code: str | None,
    expected_decision: str,
) -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact" if provider_code else "unresolved",
            primary_category_code=provider_code,
            alternatives=[],
            confidence="high" if provider_code else "low",
            evidence=item_name if provider_code else None,
            rationale_code=(
                "taxonomy_match" if provider_code else "insufficient_context"
            ),
        )
    )

    result = CategoryResolutionService(provider=provider).resolve(
        "goods", item_name, source_text
    )

    assert result.decision == expected_decision
    assert result.category_code not in {"G03", "G04"}
    assert result.candidates == ((provider_code,) if provider_code else ())


def test_category_schema_is_strict_and_closed() -> None:
    schema = category_classification_strict_json_schema()

    assert validate_category_classification_schema(schema) == []
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])
    assert "default" not in str(schema)


def test_artificial_deterministic_conflict_is_rejected() -> None:
    provider = Mock()
    resolver = CategoryResolutionService(provider=provider)

    result = resolver.validate_provider_payload(
        procurement_type="goods",
        source_text="офисное кресло",
        deterministic=DeterministicCategoryClassifier().classify(
            "офисное кресло", "goods"
        ),
        payload=CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G04",
            alternatives=[],
            confidence="high",
            evidence="офисное кресло",
            rationale_code="taxonomy_match",
        ),
    )

    assert result.decision == "unresolved"
    assert result.reason_code == "deterministic_conflict"


def _adapter(
    provider: FakeCategoryClassificationProvider,
    storage: InMemoryIntakeStorage | None = None,
) -> tuple[TelegramIntakeAdapter, InMemoryIntakeStorage]:
    current = storage or InMemoryIntakeStorage()
    adapter = TelegramIntakeAdapter(
        PersistentIntakeOrchestrator(
            InMemoryIntakePersistenceRepository(current)
        ),
        category_resolver=CategoryResolutionService(provider=provider),
    )
    return adapter, current


def test_llm_exact_requires_confirmation_and_survives_reload() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G02",
            alternatives=[],
            confidence="high",
            evidence="офисных стульев",
            rationale_code="taxonomy_match",
        )
    )
    adapter, storage = _adapter(provider)

    proposed = adapter.handle_text(
        USER_ID,
        1001,
        1,
        "Нужно купить 5 офисных стульев для переговорной",
    )

    assert proposed.result is not None
    assert proposed.result.intake_result.draft.category_code is None
    assert "Похоже, подходит категория «Мебель и оснащение (G02)»" in proposed.text
    option = proposed.result.dialog_state.intake_conversation.category_candidates[0]
    assert option.source == "llm_exact"
    assert option.selectable is True
    assert option.readiness_eligible is False

    reloaded, _ = _adapter(provider, storage)
    confirmed = reloaded.handle_text(USER_ID, 1001, 2, "да")

    assert confirmed.result is not None
    draft = confirmed.result.intake_result.draft
    assert draft.category_code == "G02"
    assert draft.field_states["category_code"].confirmed is True
    assert draft.field_states["category_code"].evidence == (
        category_confirmation_evidence(
            "goods",
            draft.item_name or "",
            "G02",
            category_draft_context_fingerprint(draft),
        )
    )
    assert provider.calls == 1


def test_confirmed_dock_station_category_is_not_reclassified_from_related_laptop(
) -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G04",
            alternatives=[],
            confidence="high",
            evidence="док-станция",
            rationale_code="taxonomy_match",
        )
    )
    adapter, storage = _adapter(provider)

    proposed = adapter.handle_text(
        USER_ID,
        1002,
        1,
        "нужна док-станция для ноутбука",
    )

    assert proposed.result is not None
    assert proposed.result.intake_result.draft.category_code is None
    assert "IT-периферия (G04)" in proposed.text
    assert provider.calls == 1

    reloaded, _ = _adapter(provider, storage)
    confirmed = reloaded.handle_text(USER_ID, 1002, 2, "да")

    assert confirmed.result is not None
    draft = confirmed.result.intake_result.draft
    assert draft.category_code == "G04"
    assert draft.field_states["category_code"].confirmed is True
    assert confirmed.result.intake_result.next_question is not None
    assert confirmed.result.intake_result.next_question.field_code != "category_code"
    assert reloaded._category_candidates(confirmed.result) == ()
    assert provider.calls == 1


def test_confirmed_category_is_invalidated_by_changed_semantic_context() -> None:
    draft = RequestDraftData(
        procurement_type="goods",
        item_name="док-станция для ноутбука",
        category_code="G04",
    )
    context_fingerprint = category_draft_context_fingerprint(draft)
    evidence = category_confirmation_evidence(
        "goods",
        draft.item_name,
        "G04",
        context_fingerprint,
    )
    draft.field_states["category_code"] = FieldValueState(
        field_code="category_code",
        value="G04",
        source=UpdateSource.USER,
        evidence=evidence,
        confirmed=True,
    )

    assert "category_code" not in IntakeFieldValidator().validate_draft(draft)

    changed = draft.model_copy(
        deep=True,
        update={"specifications": "серверное инфраструктурное оборудование"},
    )

    assert "category_code" in IntakeFieldValidator().validate_draft(changed)


def test_llm_candidates_show_only_validated_options_and_confirm_selection() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="candidates",
            primary_category_code=None,
            alternatives=["G04", "G02"],
            confidence="medium",
            evidence="аксессуар для рабочего места",
            rationale_code="ambiguous_taxonomy_match",
        )
    )
    adapter, storage = _adapter(provider)

    proposed = adapter.handle_text(
        USER_ID, 1001, 10, "Нужно купить аксессуар для рабочего места"
    )

    assert "1. IT-периферия (G04)" in proposed.text
    assert "2. Мебель и оснащение (G02)" in proposed.text
    assert "G01" not in proposed.text
    reloaded, _ = _adapter(provider, storage)
    selected = reloaded.handle_text(USER_ID, 1001, 11, "2")
    assert selected.result is not None
    assert selected.result.intake_result.draft.category_code == "G02"
    selected_draft = selected.result.intake_result.draft
    assert selected_draft.field_states["category_code"].evidence == (
        category_confirmation_evidence(
            "goods",
            selected_draft.item_name or "",
            "G02",
            category_draft_context_fingerprint(selected_draft),
        )
    )
    assert provider.calls == 1


def test_changed_subject_invalidates_persisted_llm_candidates() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="candidates",
            primary_category_code=None,
            alternatives=["G04", "G02"],
            confidence="medium",
            evidence="аксессуар для рабочего места",
            rationale_code="ambiguous_taxonomy_match",
        )
    )
    adapter, storage = _adapter(provider)
    proposed = adapter.handle_text(
        USER_ID, 1001, 15, "Нужно купить аксессуар для рабочего места"
    )
    assert proposed.result is not None

    changed = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage)
    ).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={"item_name": "другое неизвестное устройство"},
            explicit_correction=True,
        ),
        request_id=proposed.result.request_id,
    )
    reloaded, _ = _adapter(provider, storage)

    assert reloaded._category_candidates(changed) == ()


def test_changed_decomposition_invalidates_persisted_llm_candidates() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="candidates",
            primary_category_code=None,
            alternatives=["G04", "G02"],
            confidence="medium",
            evidence="аксессуар для рабочего места",
            rationale_code="ambiguous_taxonomy_match",
        )
    )
    adapter, _ = _adapter(provider)
    proposed = adapter.handle_text(
        USER_ID, 1001, 16, "Нужно купить аксессуар для рабочего места"
    )
    assert proposed.result is not None
    conversation = proposed.result.dialog_state.intake_conversation
    conversation.category_decomposition_fingerprint = "previous-composition"
    conversation.decomposition_fingerprint = "changed-composition"

    assert adapter._category_candidates(proposed.result) == ()


def test_unresolved_or_failed_provider_never_restores_generic_fallback() -> None:
    provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="unresolved",
            primary_category_code=None,
            alternatives=[],
            confidence="low",
            evidence=None,
            rationale_code="insufficient_context",
        )
    )
    adapter, _ = _adapter(provider)

    outcome = adapter.handle_text(
        USER_ID, 1001, 20, "Нужно купить неизвестное устройство"
    )

    assert outcome.result is not None
    assert outcome.result.intake_result.draft.category_code is None
    assert not outcome.result.dialog_state.intake_conversation.category_candidates
    assert "Не удалось уверенно определить категорию" in outcome.text
    assert all(code not in outcome.text for code in ("G01", "G02", "G03", "G04"))


def test_openai_category_provider_uses_strict_dto_and_shared_client() -> None:
    from types import SimpleNamespace

    from app.bot.category_resolution import (
        CategoryClassificationRequest,
        OpenAICategoryClassificationProvider,
        category_taxonomy,
    )

    parsed = CategoryClassificationPayload(
        decision="exact",
        primary_category_code="G02",
        alternatives=[],
        confidence="high",
        evidence="офисные стулья",
        rationale_code="taxonomy_match",
    )
    client = Mock()
    client.responses.parse.return_value = SimpleNamespace(output_parsed=parsed)
    provider = OpenAICategoryClassificationProvider(
        model="test-model", timeout_seconds=12, client=client
    )
    request = CategoryClassificationRequest(
        procurement_type="goods",
        item_name="офисные стулья",
        source_text="офисные стулья",
        taxonomy_version="test",
        taxonomy=category_taxonomy("goods"),
    )

    assert provider.classify(request) == parsed
    kwargs = client.responses.parse.call_args.kwargs
    assert kwargs["text_format"] is CategoryClassificationPayload
    assert kwargs["store"] is False
    assert kwargs["model"] == "test-model"
    assert kwargs["timeout"] == 12


def test_offline_category_generalization_dataset() -> None:
    cases = json.loads(GENERALIZATION_DATASET.read_text(encoding="utf-8"))
    assert len(cases) >= 7
    assert len({case["case_id"] for case in cases}) == len(cases)

    for case in cases:
        codes = case["provider_codes"]
        decision = case["provider_decision"]
        payload = CategoryClassificationPayload(
            decision=decision,
            primary_category_code=(codes[0] if decision == "exact" else None),
            alternatives=(codes if decision == "candidates" else []),
            confidence=("low" if decision == "unresolved" else "high"),
            evidence=(None if decision == "unresolved" else case["text"]),
            rationale_code=(
                "insufficient_context"
                if decision == "unresolved"
                else "ambiguous_taxonomy_match"
                if decision == "candidates"
                else "taxonomy_match"
            ),
        )
        result = CategoryResolutionService(
            provider=FakeCategoryClassificationProvider(payload)
        ).resolve(case["procurement_type"], case["text"], case["text"])

        assert result.decision == case["expected_decision"], case["case_id"]
        assert result.candidates == tuple(codes), case["case_id"]
