from datetime import date
from uuid import UUID

import pytest

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.categories import DeterministicCategoryClassifier
from app.intake.models import IntakeFieldUpdate
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator

USER_ID = UUID("88888888-8888-4888-8888-888888888888")


def _orchestrator(storage: InMemoryIntakeStorage) -> PersistentIntakeOrchestrator:
    return PersistentIntakeOrchestrator(InMemoryIntakePersistenceRepository(storage))


def _adapter(storage: InMemoryIntakeStorage) -> TelegramIntakeAdapter:
    return TelegramIntakeAdapter(_orchestrator(storage))


def _seed_service_category_question(
    storage: InMemoryIntakeStorage,
    *,
    item_name: str = "установка готового ПО, лицензии уже куплены",
    description: str = "нужна только настройка программы для сотрудников",
) -> None:
    result = _orchestrator(storage).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={
                "procurement_type": "service",
                "item_name": item_name,
                "description": description,
            }
        ),
    )
    assert result.intake_result.next_question is not None
    assert result.intake_result.next_question.field_code == "category_code"


def test_ambiguous_software_installation_clarifies_and_preserves_details() -> None:
    storage = InMemoryIntakeStorage()
    first = _adapter(storage).handle_text(
        USER_ID,
        1001,
        1,
        "необходимо установить лицензионное ПО для работы логистов "
        "на новые компьютеры не позднее 19 августа",
    )

    assert first.result is not None
    first_draft = first.result.intake_result.draft
    assert first.reason_code == "software_scope_clarification_required"
    assert "лицензии уже приобретены" in first.text
    assert first_draft.procurement_type is None
    assert first_draft.category_code is None
    assert first_draft.desired_delivery_date == date(2026, 8, 19)
    assert "логист" in (first_draft.item_name or "").casefold()
    assert "компьютер" in (first_draft.item_name or "").casefold()
    pending = first.result.dialog_state.intake_conversation
    assert pending.category_clarification_kind == "software_acquisition_scope"
    assert pending.category_question_fingerprint
    assert [option.code for option in pending.category_candidates] == ["G05", "S05"]

    resolved = _adapter(storage).handle_text(
        USER_ID,
        1001,
        2,
        "лицензии уже есть, нужна только установка",
    )
    assert resolved.result is not None
    assert resolved.result.intake_result.draft.procurement_type == "service"
    assert resolved.result.intake_result.draft.category_code == "S05"

    second = _adapter(storage).handle_text(
        USER_ID,
        1001,
        3,
        "работающее ПО для каждого компьютера, работы можно будет начать "
        "с 16 августа, когда компьютеры поступят на склад",
    )

    assert second.result is not None
    draft = second.result.intake_result.draft
    assert draft.category_code == "S05"
    assert draft.desired_delivery_date == date(2026, 8, 19)
    assert "16 августа" in (draft.description or "")
    assert "поступят на склад" in (draft.description or "")
    assert "компьютер" in (draft.item_name or "").casefold()
    assert second.result.intake_result.next_question is not None
    assert second.result.intake_result.next_question.field_code != "category_code"


@pytest.mark.parametrize(
    ("text", "expected_type", "expected_category"),
    [
        (
            "Нужно закупить лицензии для новых компьютеров до 19 августа",
            "goods",
            "G05",
        ),
        (
            "Нужно установить ПО до 19 августа, лицензии уже куплены",
            "service",
            "S05",
        ),
    ],
)
def test_unambiguous_software_need_selects_category_directly(
    text: str,
    expected_type: str,
    expected_category: str,
) -> None:
    outcome = _adapter(InMemoryIntakeStorage()).handle_text(
        USER_ID,
        1001,
        5,
        text,
    )

    assert outcome.result is not None
    draft = outcome.result.intake_result.draft
    assert draft.procurement_type == expected_type
    assert draft.category_code == expected_category
    assert draft.desired_delivery_date == date(2026, 8, 19)


@pytest.mark.parametrize(
    "answer",
    ["лицензии уже есть", "куплены", "нужна только установка", "только настроить"],
)
def test_software_scope_service_answers_select_s05_after_reload(answer: str) -> None:
    storage = InMemoryIntakeStorage()
    _adapter(storage).handle_text(
        USER_ID,
        1001,
        60,
        "Нужно установить лицензионное ПО до 19 августа",
    )

    resolved = _adapter(storage).handle_text(USER_ID, 1001, 61, answer)

    assert resolved.result is not None
    draft = resolved.result.intake_result.draft
    assert draft.procurement_type == "service"
    assert draft.category_code == "S05"
    assert draft.desired_delivery_date == date(2026, 8, 19)


@pytest.mark.parametrize(
    "answer",
    ["нужно купить лицензии", "лицензий ещё нет", "нужна подписка", "закупить ПО"],
)
def test_software_scope_product_answers_select_g05_after_reload(answer: str) -> None:
    storage = InMemoryIntakeStorage()
    _adapter(storage).handle_text(
        USER_ID,
        1001,
        70,
        "Нужно установить лицензионное ПО до 19 августа",
    )

    resolved = _adapter(storage).handle_text(USER_ID, 1001, 71, answer)

    assert resolved.result is not None
    draft = resolved.result.intake_result.draft
    assert draft.procurement_type == "goods"
    assert draft.category_code == "G05"
    assert draft.desired_delivery_date == date(2026, 8, 19)


@pytest.mark.parametrize(
    "text",
    [
        "Нужно купить лицензии и установить ПО",
        "Нужны лицензии вместе с установкой",
        "Нужно и приобрести, и настроить программное обеспечение",
    ],
)
def test_mixed_software_need_requires_split(text: str) -> None:
    outcome = _adapter(InMemoryIntakeStorage()).handle_text(
        USER_ID,
        1001,
        80,
        text,
    )

    assert outcome.reason_code == "multi_category_split_required"
    assert "отдельными заявками" in outcome.text
    assert "G05" in outcome.text
    assert "S05" in outcome.text
    assert outcome.result is not None
    state = outcome.result.dialog_state.intake_conversation
    assert state.split_required
    assert [item.category_code for item in state.item_candidates] == ["G05", "S05"]


def test_mixed_software_split_selection_keeps_only_chosen_need() -> None:
    storage = InMemoryIntakeStorage()
    _adapter(storage).handle_text(
        USER_ID,
        1001,
        90,
        "Нужно купить лицензии и установить ПО до 19 августа",
    )

    selected = _adapter(storage).handle_text(
        USER_ID,
        1001,
        91,
        "начнем с лицензий",
    )

    assert selected.result is not None
    draft = selected.result.intake_result.draft
    assert draft.procurement_type == "goods"
    assert draft.category_code == "G05"
    assert draft.item_name == "лицензии на ПО"
    assert draft.desired_delivery_date == date(2026, 8, 19)
    assert selected.result.dialog_state.intake_conversation.is_empty


def test_licensed_software_reply_alone_does_not_select_s05() -> None:
    storage = InMemoryIntakeStorage()
    _seed_service_category_question(
        storage,
        item_name="программное обеспечение для сотрудников",
        description="нужно рабочее ПО",
    )

    selected = _adapter(storage).handle_text(
        USER_ID,
        1001,
        10,
        "это лицензионное программное обеспечение",
    )

    assert selected.result is not None
    assert selected.result.intake_result.draft.category_code == "G05"
    assert selected.result.intake_result.draft.category_code != "S05"
    assert selected.result.intake_result.draft.procurement_type == "goods"


@pytest.mark.parametrize(
    "selection",
    ["1", "IT-разработка и поддержка", "разработка и поддержка"],
)
def test_service_category_selects_from_persisted_candidates_after_reload(
    selection: str,
) -> None:
    storage = InMemoryIntakeStorage()
    _seed_service_category_question(storage)

    shown = _adapter(storage).handle_text(
        USER_ID,
        1001,
        20,
        "какие есть подходящие варианты?",
    )
    assert shown.result is not None
    assert "Подходящие категории:" in shown.text
    assert "1. IT-разработка и поддержка (S05)" in shown.text
    stored = shown.result.dialog_state.intake_conversation.category_candidates
    assert [(option.code, option.label) for option in stored] == [
        ("S05", "IT-разработка и поддержка")
    ]
    step_id = shown.result.dialog_state.intake_conversation.category_step_id
    assert step_id

    selected = _adapter(storage).handle_text(USER_ID, 1001, 21, selection)
    assert selected.result is not None
    assert selected.result.intake_result.draft.category_code == "S05"
    assert selected.result.dialog_state.intake_conversation.is_empty


def test_service_category_help_repeats_same_persisted_list() -> None:
    storage = InMemoryIntakeStorage()
    _seed_service_category_question(storage)

    first = _adapter(storage).handle_text(
        USER_ID,
        1001,
        30,
        "я не знаю номеров категорий, назови варианты",
    )
    second = _adapter(storage).handle_text(
        USER_ID,
        1001,
        31,
        "покажи категории",
    )

    assert first.text == second.text
    assert first.result is not None
    assert second.result is not None
    first_state = first.result.dialog_state.intake_conversation
    second_state = second.result.dialog_state.intake_conversation
    assert first_state.category_candidates == second_state.category_candidates
    assert first_state.category_step_id == second_state.category_step_id


@pytest.mark.parametrize(
    "help_text",
    [
        "какие есть подходящие варианты?",
        "покажи категории",
        "я не знаю номеров",
        "назови варианты",
        "что можно выбрать?",
    ],
)
def test_every_service_category_help_phrase_shows_candidates(
    help_text: str,
) -> None:
    storage = InMemoryIntakeStorage()
    _seed_service_category_question(storage)

    outcome = _adapter(storage).handle_text(USER_ID, 1001, 35, help_text)

    assert "Подходящие категории:" in outcome.text
    assert "1. IT-разработка и поддержка (S05)" in outcome.text
    assert "Напишите номер или название категории." in outcome.text


def test_two_unknown_service_category_replies_do_not_loop() -> None:
    storage = InMemoryIntakeStorage()
    _seed_service_category_question(storage)

    first = _adapter(storage).handle_text(USER_ID, 1001, 40, "что-то другое")
    second = _adapter(storage).handle_text(USER_ID, 1001, 41, "всё равно не знаю")

    assert first.reason_code == "repeated_category_clarification"
    assert second.reason_code == "repeated_category_clarification"
    assert "Подходящие категории:" in first.text
    assert "Подходящие категории:" in second.text
    assert "вернитесь к описанию предмета" in second.text
    assert second.result is not None
    assert second.result.intake_result.draft.category_code is None
    assert (
        second.result.dialog_state.intake_conversation.category_clarification_repeats
        == 2
    )


@pytest.mark.parametrize(
    ("text", "kind", "codes"),
    [
        ("лицензия на готовое ПО", "exact", ("G05",)),
        ("подписка SaaS", "exact", ("G05",)),
        ("установка готового ПО", "exact", ("S05",)),
        ("настройка готового программного обеспечения", "exact", ("S05",)),
        ("разработка новой программы", "exact", ("S05",)),
        ("доработка системы", "exact", ("S05",)),
        ("интеграция систем", "exact", ("S05",)),
        ("ПО для сотрудников", "multiple", ("G05", "S05")),
        ("программы и лицензии", "exact", ("G05",)),
    ],
)
def test_real_software_category_distinctions(
    text: str,
    kind: str,
    codes: tuple[str, ...],
) -> None:
    classification = DeterministicCategoryClassifier().classify(text)

    assert classification.kind == kind
    actual = (
        (classification.category_code,)
        if classification.category_code is not None
        else classification.candidates
    )
    assert actual == codes
