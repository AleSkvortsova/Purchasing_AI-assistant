from datetime import date
from uuid import UUID

import pytest

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.categories import DeterministicCategoryClassifier
from app.bot.normalization import NaturalDateParser
from app.bot.parser import DeterministicIntakeParser
from app.intake.models import IntakeFieldUpdate, IntakeStatus
from app.intake_persistence.models import (
    CategoryCandidateOption,
    IntakeConversationState,
)
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator

USER_ID = UUID("77777777-7777-4777-8777-777777777777")
REFERENCE_DATE = date(2026, 7, 29)


def _adapter(storage: InMemoryIntakeStorage) -> TelegramIntakeAdapter:
    return TelegramIntakeAdapter(
        PersistentIntakeOrchestrator(InMemoryIntakePersistenceRepository(storage)),
        parser=DeterministicIntakeParser(
            date_parser=NaturalDateParser(today_provider=lambda: REFERENCE_DATE)
        ),
    )


@pytest.mark.parametrize(
    ("phrase", "kind", "codes"),
    [
        ("компьютерная техника", "exact", ("G03",)),
        ("компьютеры", "exact", ("G03",)),
        ("компьютер", "exact", ("G03",)),
        ("компьютерная мышь", "exact", ("G04",)),
        ("IT-оборудование", "multiple", ("G03", "G04")),
        ("оргтехника", "multiple", ("G03", "G04")),
        ("офисная мебель", "exact", ("G02",)),
        ("столы", "exact", ("G02",)),
        ("транспорт", "exact", ("S03",)),
        ("полиграфия", "multiple", ("G11", "S13")),
        ("услуги разработки", "exact", ("S05",)),
    ],
)
def test_natural_category_aliases(
    phrase: str,
    kind: str,
    codes: tuple[str, ...],
) -> None:
    result = DeterministicCategoryClassifier().classify(phrase)

    assert result.kind == kind
    actual = (
        (result.category_code,)
        if result.category_code is not None
        else result.candidates
    )
    assert actual == codes


@pytest.mark.parametrize(
    ("text", "procurement_type", "expected"),
    [
        ("офисное кресло", "goods", "G02"),
        ("ноутбук", "goods", "G03"),
        ("офисная бумага", "goods", "G01"),
        ("средства индивидуальной защиты", "goods", "G08"),
        ("уборка офиса", "service", "S02"),
        ("ремонт и обслуживание оборудования", "service", "S01"),
        ("перевозка груза", "service", "S03"),
    ],
)
def test_strong_category_subjects_have_positive_classifier_support(
    text: str,
    procurement_type: str,
    expected: str,
) -> None:
    result = DeterministicCategoryClassifier().classify(text, procurement_type)

    assert result.kind == "exact"
    assert result.category_code == expected


def test_generic_category_fallback_is_not_presented_as_strong_evidence() -> None:
    storage = InMemoryIntakeStorage()
    seeded = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage)
    ).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={
                "procurement_type": "goods",
                "item_name": "промышленный вентилятор",
            }
        ),
    )
    assert seeded.intake_result.next_question is not None
    assert seeded.intake_result.next_question.field_code == "category_code"

    outcome = _adapter(storage).handle_text(
        USER_ID,
        1001,
        500,
        "какие варианты",
    )

    assert "Не удалось уверенно определить категорию" in outcome.text
    assert all(code not in outcome.text for code in ("G01", "G02", "G03", "G04"))
    assert outcome.result is not None
    assert not outcome.result.dialog_state.intake_conversation.category_candidates


def test_generic_fallback_candidate_is_explicitly_weak() -> None:
    option = CategoryCandidateOption(
        code="G01",
        label="Офисные принадлежности",
        source="generic_fallback",
        selectable=False,
        readiness_eligible=False,
    )

    assert option.source == "generic_fallback"
    assert option.selectable is False
    assert option.readiness_eligible is False


def test_strong_candidate_provenance_survives_state_round_trip() -> None:
    option = CategoryCandidateOption(
        code="G02",
        label="Мебель и оснащение",
        source="classifier_exact",
        selectable=True,
        readiness_eligible=True,
    )

    restored = CategoryCandidateOption.model_validate(option.model_dump())

    assert restored == option
    assert restored.source == "classifier_exact"
    assert restored.readiness_eligible is True


def test_legacy_candidate_without_provenance_is_weak_by_default() -> None:
    restored = CategoryCandidateOption.model_validate(
        {"code": "G02", "label": "Мебель и оснащение"}
    )

    assert restored.source == "generic_fallback"
    assert restored.selectable is False
    assert restored.readiness_eligible is False


@pytest.mark.parametrize(
    "selection",
    [
        "компьютер",
        "давайте компьютер",
        "только компьютер",
        "начнём с компьютерной техники",
    ],
)
def test_multi_category_description_requires_split_and_narrows_after_reload(
    selection: str,
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    storage = InMemoryIntakeStorage()

    split = _adapter(storage).handle_text(
        USER_ID,
        1001,
        1,
        "Мне нужно закупить компьютер и стол на склад в Шушары к 5 августа",
    )

    assert split.reason_code == "multi_category_split_required"
    assert "компьютерная техника и мебель" in split.text
    assert split.result is not None
    assert split.result.intake_result.draft.item_name is None
    assert "склад в Шушары" in (
        split.result.intake_result.draft.delivery_location or ""
    )
    assert split.result.intake_result.draft.desired_delivery_date == date(2026, 8, 5)
    state = split.result.dialog_state.intake_conversation
    assert [item.category_code for item in state.item_candidates] == ["G03", "G02"]

    selected = _adapter(storage).handle_text(
        USER_ID,
        1001,
        2,
        selection,
    )

    assert selected.result is not None
    draft = selected.result.intake_result.draft
    assert draft.item_name == "компьютер"
    assert draft.category_code == "G03"
    assert "склад в Шушары" in (draft.delivery_location or "")
    assert draft.desired_delivery_date == date(2026, 8, 5)
    assert (
        "стол"
        not in " ".join(
            value or "" for value in (draft.item_name, draft.description, draft.title)
        ).casefold()
    )
    assert selected.result.dialog_state.intake_conversation.is_empty


def test_natural_category_phrase_is_recognized() -> None:
    storage = InMemoryIntakeStorage()
    orchestrator = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage)
    )
    initial = orchestrator.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={"procurement_type": "goods", "item_name": "оборудование"}
        ),
    )
    assert initial.intake_result.next_question is not None
    assert initial.intake_result.next_question.field_code == "category_code"

    outcome = TelegramIntakeAdapter(orchestrator).handle_text(
        USER_ID,
        1001,
        3,
        "к категории, предусмотренной для компьютерной техники",
    )

    assert outcome.result is not None
    assert outcome.result.intake_result.draft.category_code == "G03"


def test_category_candidates_are_persisted_repeated_and_selected_by_number() -> None:
    storage = InMemoryIntakeStorage()
    first = _adapter(storage).handle_text(
        USER_ID,
        1001,
        10,
        "Нужно купить IT-оборудование",
    )
    assert first.result is not None
    options = first.result.dialog_state.intake_conversation.category_candidates
    assert [option.code for option in options] == ["G03", "G04"]
    assert "1. IT-оборудование" in first.text
    assert "2. IT-периферия" in first.text

    help_outcome = _adapter(storage).handle_text(
        USER_ID,
        1001,
        11,
        "дай список подходящих категорий",
    )
    assert "Подходящие категории:" in help_outcome.text
    assert "1. IT-оборудование" in help_outcome.text
    assert "2. IT-периферия" in help_outcome.text

    selected = _adapter(storage).handle_text(USER_ID, 1001, 12, "2")
    assert selected.result is not None
    assert selected.result.intake_result.draft.category_code == "G04"
    assert selected.result.dialog_state.intake_conversation.is_empty


def test_invalid_category_reply_shows_saved_options_and_breaks_repeat_loop() -> None:
    storage = InMemoryIntakeStorage()
    _adapter(storage).handle_text(
        USER_ID,
        1001,
        30,
        "Нужно купить IT-оборудование",
    )

    invalid = _adapter(storage).handle_text(USER_ID, 1001, 31, "что-то другое")

    assert invalid.reason_code == "repeated_category_clarification"
    assert "Подходящие категории:" in invalid.text
    assert "1. IT-оборудование" in invalid.text
    assert "вернитесь к описанию предмета" in invalid.text
    assert invalid.result is not None
    assert invalid.result.intake_result.draft.category_code is None


def test_two_items_of_same_category_do_not_trigger_split() -> None:
    outcome = _adapter(InMemoryIntakeStorage()).handle_text(
        USER_ID,
        1001,
        20,
        "Нужно купить два офисных стола и три тумбы",
    )

    assert outcome.reason_code != "multi_category_split_required"
    assert outcome.result is not None
    assert outcome.result.intake_result.draft.category_code == "G02"


def test_rehearsal_goods_plus_service_requires_split_before_scalar_draft(
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    storage = InMemoryIntakeStorage()

    outcome = _adapter(storage).handle_text(
        USER_ID,
        1001,
        200,
        "Нужно закупить 20 светодиодных потолочных светильников для "
        "производственного помещения и выполнить их монтаж вместо старых "
        "светильников до 5 сентября. Работы и поставка нужны на площадке "
        "по адресу улица Салова, 56.",
    )

    assert outcome.reason_code == "multi_category_split_required"
    assert outcome.result is not None
    assert "две отдельные потребности" in outcome.text
    state = outcome.result.dialog_state.intake_conversation
    assert [item.procurement_type for item in state.item_candidates] == [
        "goods",
        "service",
    ]
    assert "светильник" in state.item_candidates[0].item_name
    assert "монтаж" in state.item_candidates[1].item_name
    draft = outcome.result.intake_result.draft
    assert draft.item_name is None
    assert draft.category_code is None
    assert outcome.result.intake_result.request_card is None
    assert outcome.result.intake_result.status != IntakeStatus.READY_FOR_CONFIRMATION

    selected = _adapter(storage).handle_text(
        USER_ID,
        1001,
        201,
        "Начнём с монтажа",
    )
    assert selected.result is not None
    selected_draft = selected.result.intake_result.draft
    assert selected_draft.procurement_type == "service"
    assert selected_draft.category_code == "S01"
    assert "монтаж" in (selected_draft.item_name or "")
    assert selected_draft.quantity is None
    assert selected_draft.desired_delivery_date == date(2026, 9, 5)
    assert "Салова" in (selected_draft.delivery_location or "")
    reloaded = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage)
    ).get_active_session(USER_ID)
    assert reloaded.intake_result.draft.quantity is None
    assert reloaded.intake_result.draft.procurement_type == "service"


@pytest.mark.parametrize(
    "text",
    [
        "Купить кондиционер и установить его в переговорной",
        "Закупить напольное покрытие и выполнить его укладку",
        "Поставить стеллажи и собрать их на складе",
        "Купить оборудование и выполнить монтаж",
        "Закупить новые картриджи и заправить используемые картриджи",
    ],
)
def test_goods_plus_service_paraphrases_require_split(text: str) -> None:
    outcome = _adapter(InMemoryIntakeStorage()).handle_text(
        USER_ID,
        1001,
        210,
        text,
    )

    assert outcome.reason_code == "multi_category_split_required"
    assert outcome.result is not None
    assert {
        item.procurement_type
        for item in outcome.result.dialog_state.intake_conversation.item_candidates
    } == {"goods", "service"}
    assert outcome.result.intake_result.draft.category_code is None


@pytest.mark.parametrize(
    "text",
    [
        "Сборка 3 шкафов и 2 столов",
        "Ремонт 5 офисных кресел",
        "Перевозка 12 паллет",
        "Уборка склада с мойкой окон",
        "Нужно купить бумагу с доставкой товара поставщиком",
        "Нужно купить два офисных стола и три тумбы",
    ],
)
def test_single_need_phrases_do_not_trigger_goods_service_split(text: str) -> None:
    outcome = _adapter(InMemoryIntakeStorage()).handle_text(
        USER_ID,
        1001,
        220,
        text,
    )

    state = (
        outcome.result.dialog_state.intake_conversation
        if outcome.result is not None
        else None
    )
    types = (
        {item.procurement_type for item in state.item_candidates}
        if state
        else set()
    )
    assert types != {"goods", "service"}


@pytest.mark.parametrize(
    ("selected_type", "stale_code", "forbidden_prefix"),
    [("goods", "S01", "S"), ("service", "G03", "G")],
)
def test_category_candidates_are_invalidated_after_type_change_and_reload(
    selected_type: str,
    stale_code: str,
    forbidden_prefix: str,
) -> None:
    storage = InMemoryIntakeStorage()
    orchestrator = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage)
    )
    seeded = orchestrator.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "неопределённая потребность"}),
        intake_conversation=IntakeConversationState(
            category_candidates=[
                CategoryCandidateOption(
                    code=stale_code,
                    label="устаревший кандидат",
                )
            ],
            category_step_id="stale-category-step",
        ),
    )
    assert seeded.intake_result.next_question is not None
    assert seeded.intake_result.next_question.field_code == "procurement_type"

    _adapter(storage).handle_text(
        USER_ID,
        1001,
        230,
        "товар" if selected_type == "goods" else "услуга",
    )
    reloaded = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage)
    ).get_active_session(USER_ID)

    codes = [
        option.code
        for option in reloaded.dialog_state.intake_conversation.category_candidates
    ]
    assert all(not code.startswith(forbidden_prefix) for code in codes)

    selected = _adapter(storage).handle_text(USER_ID, 1001, 231, stale_code)
    assert selected.result is not None
    assert selected.result.intake_result.draft.category_code != stale_code
