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

USER_ID = UUID("77777777-7777-4777-8777-777777777777")


def _adapter(storage: InMemoryIntakeStorage) -> TelegramIntakeAdapter:
    return TelegramIntakeAdapter(
        PersistentIntakeOrchestrator(InMemoryIntakePersistenceRepository(storage))
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
) -> None:
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
