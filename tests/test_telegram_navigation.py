from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.dialog_modes import (
    InMemoryDialogModeRepository,
    InMemoryDialogModeStorage,
)
from app.bot.formatters import INSTRUCTION_TEXT, REGULATION_INTRO_TEXT
from app.bot.keyboards import (
    LEGACY_MENU_EXAMPLES,
    LEGACY_MENU_HELP,
    MENU_CURRENT,
    MENU_INSTRUCTION,
    MENU_MY_REQUESTS,
    MENU_NEW,
    MENU_REGULATIONS,
    encode_navigation_callback,
    main_menu,
)
from app.bot.request_history import (
    InMemoryRequestHistoryRepository,
    RequestHistoryService,
)
from app.intake.card import RequestCardBuilder
from app.intake.models import (
    CompletenessResult,
    IntakeStatus,
    IntakeStepResult,
    RequestDraftData,
)
from app.intake_persistence.exceptions import ActiveDraftNotFoundError
from app.intake_persistence.models import (
    PersistentDialogState,
    PersistentIntakeStepResult,
)
from app.intake_persistence.repositories import InMemoryIntakeStorage
from app.rag.answering import RegulationAnswer, RegulationSource
from app.schemas.common import RequestStatus, RequestType
from app.schemas.request import RequestRead

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")
REQUEST_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


class FakeOrchestrator:
    def __init__(self, active: PersistentIntakeStepResult | None = None) -> None:
        self.active = active
        self.get_calls = 0
        self.process_calls = 0

    def get_active_session(self, user_id):
        self.get_calls += 1
        if self.active is None:
            raise ActiveDraftNotFoundError("not found")
        return self.active.model_copy(deep=True)

    def process_structured_step(self, *args, **kwargs):
        self.process_calls += 1
        raise AssertionError("intake must not run in regulation mode")


class FailingParser:
    def parse(self, *args, **kwargs):
        raise AssertionError("parser must not run in regulation mode")


class FakeRegulationService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def answer(self, question: str) -> RegulationAnswer:
        self.calls.append(question)
        return RegulationAnswer(
            answer="Согласование определено матрицей.",
            status="answered",
            sources=[
                RegulationSource(
                    document_id="kb-009",
                    display_name="Правила согласования заявок",
                )
            ],
            diagnostics={
                "retrieval_status": "found",
                "chunk_count": 2,
                "source_count": 1,
                "duration_ms": 3,
            },
        )


def _active() -> PersistentIntakeStepResult:
    draft = RequestDraftData(procurement_type="goods", item_name="Мониторы")
    intake = IntakeStepResult(
        status=IntakeStatus.COLLECTING,
        draft=draft,
        completeness=CompletenessResult(
            is_complete=False,
            required_fields=["amount"],
            completed_fields=["procurement_type", "item_name"],
            missing_fields=["amount"],
            invalid_fields=[],
            blocked_fields=[],
            completion_ratio=Decimal("0.5"),
        ),
    )
    return PersistentIntakeStepResult(
        request_id=REQUEST_ID,
        user_id=USER_ID,
        request_version=2,
        intake_result=intake,
        dialog_state=PersistentDialogState(
            user_id=USER_ID,
            request_id=REQUEST_ID,
            intake_status=IntakeStatus.COLLECTING,
            state_version=2,
        ),
        persistence_status="saved",
    )


def _request(
    *,
    request_id: UUID | None = None,
    user_id: UUID = USER_ID,
    number: str = "PR-2026-000015",
    status: RequestStatus = RequestStatus.NEW,
    created_at: datetime = NOW,
) -> RequestRead:
    draft = RequestDraftData(
        procurement_type="goods",
        category_code="G02",
        item_name="Офисные кресла",
        quantity=Decimal("5"),
        unit="шт.",
        amount=Decimal("75000"),
        budget_status="budgeted",
        desired_delivery_date=date(2026, 8, 15),
        delivery_location="офис на Невском",
        business_justification="Оснащение рабочих мест",
        department="АХО",
    )
    card = RequestCardBuilder().build(draft)
    draft_payload = draft.model_dump(
        mode="json",
        exclude={"request_id", "requester_id", "field_states", "conflicts", "warnings"},
    )
    data = {
        "schema_version": 1,
        "intake": {
            "draft": draft_payload,
            "field_states": {},
            "conflicts": [],
            "warnings": [],
        },
        "lifecycle": {"final_request_card": card.model_dump(mode="json")},
    }
    return RequestRead(
        id=request_id or uuid4(),
        request_number=number,
        user_id=user_id,
        request_type=RequestType.PRODUCT,
        category_code="G02",
        title="Офисные кресла",
        status=status,
        data=data,
        created_at=created_at,
        updated_at=created_at,
        registered_at=created_at if status == RequestStatus.NEW else None,
        version=3,
    )


def _history_adapter(storage: InMemoryIntakeStorage) -> TelegramIntakeAdapter:
    return TelegramIntakeAdapter(
        FakeOrchestrator(),
        request_history=RequestHistoryService(
            InMemoryRequestHistoryRepository(storage)
        ),
    )


def _button_labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_main_menu_contains_exactly_five_new_items() -> None:
    labels = [button.text for row in main_menu().keyboard for button in row]
    assert labels == [
        MENU_NEW,
        MENU_CURRENT,
        MENU_MY_REQUESTS,
        MENU_INSTRUCTION,
        MENU_REGULATIONS,
    ]
    assert LEGACY_MENU_HELP not in labels
    assert LEGACY_MENU_EXAMPLES not in labels


@pytest.mark.parametrize(
    "action",
    [MENU_INSTRUCTION, LEGACY_MENU_HELP, LEGACY_MENU_EXAMPLES],
)
def test_instruction_combines_rules_examples_and_navigation(action: str) -> None:
    outcome = TelegramIntakeAdapter(FakeOrchestrator()).handle_menu(USER_ID, action)
    assert outcome.text == INSTRUCTION_TEXT
    assert "Пример товара" in outcome.text
    assert "Пример услуги" in outcome.text
    assert "Бюджет требуется уточнить" in outcome.text
    assert _button_labels(outcome.reply_markup) == [
        "📝 Новая заявка",
        "📚 Спросить по регламенту",
        "Главное меню",
    ]


@pytest.mark.parametrize("legacy_action", ["help", "examples"])
def test_legacy_navigation_callbacks_open_instruction(legacy_action: str) -> None:
    adapter = TelegramIntakeAdapter(FakeOrchestrator())
    data = encode_navigation_callback(legacy_action)
    first = adapter.handle_navigation_callback(USER_ID, "callback-1", data)
    replay = adapter.handle_navigation_callback(USER_ID, "callback-1", data)
    assert first.text == INSTRUCTION_TEXT
    assert replay.replayed is True


def test_history_empty_state() -> None:
    outcome = _history_adapter(InMemoryIntakeStorage()).handle_menu(
        USER_ID, MENU_MY_REQUESTS
    )
    assert outcome.text == "У вас пока нет зарегистрированных заявок."
    assert _button_labels(outcome.reply_markup) == [
        "📝 Новая заявка",
        "Главное меню",
    ]


def test_history_sorts_limits_and_excludes_drafts() -> None:
    storage = InMemoryIntakeStorage()
    for index in range(7):
        item = _request(
            number=f"PR-2026-{index + 1:06d}",
            created_at=NOW + timedelta(days=index),
        )
        storage.requests[item.id] = item
    draft = _request(status=RequestStatus.DRAFT, number="DRAFT")
    storage.requests[draft.id] = draft

    outcome = _history_adapter(storage).handle_menu(USER_ID, MENU_MY_REQUESTS)

    assert outcome.text.count("PR-2026-") == 5
    assert "PR-2026-000007" in outcome.text
    assert "PR-2026-000001" not in outcome.text
    assert "DRAFT" not in outcome.text
    labels = _button_labels(outcome.reply_markup)
    assert labels[0] == "Открыть PR-2026-000007"


def test_history_formats_canonical_status_and_fixed_date() -> None:
    storage = InMemoryIntakeStorage()
    cancelled = _request(
        status=RequestStatus.CANCELLED,
        number=None,
        created_at=NOW,
    )
    storage.requests[cancelled.id] = cancelled

    outcome = _history_adapter(storage).handle_menu(USER_ID, MENU_MY_REQUESTS)

    assert "Без номера" in outcome.text
    assert "Статус: Отменена" in outcome.text
    assert "Создана: 30 июля 2026" in outcome.text


def test_history_card_reuses_canonical_card_and_hides_internal_fields() -> None:
    storage = InMemoryIntakeStorage()
    request = _request(request_id=REQUEST_ID)
    storage.requests[request.id] = request
    adapter = _history_adapter(storage)

    outcome = adapter.handle_navigation_callback(
        USER_ID,
        "callback-card",
        encode_navigation_callback("request", REQUEST_ID),
    )

    assert "Заявка PR-2026-000015" in outcome.text
    assert "Передана в отдел закупок" in outcome.text
    assert "Офисные кресла" in outcome.text
    assert "5 шт." in outcome.text
    assert "75 000 ₽" in outcome.text
    assert str(REQUEST_ID) not in outcome.text
    assert "version" not in outcome.text
    assert _button_labels(outcome.reply_markup) == [
        "Назад к моим заявкам",
        "Главное меню",
    ]


@pytest.mark.parametrize("owner", [OTHER_USER_ID, USER_ID])
def test_history_ownership_and_missing_request_are_indistinguishable(
    owner: UUID,
) -> None:
    storage = InMemoryIntakeStorage()
    request = _request(request_id=REQUEST_ID, user_id=owner)
    storage.requests[request.id] = request
    requested_id = REQUEST_ID if owner == OTHER_USER_ID else uuid4()
    outcome = _history_adapter(storage).handle_navigation_callback(
        USER_ID,
        "callback-private",
        encode_navigation_callback("request", requested_id),
    )
    assert outcome.text == "Заявка не найдена или недоступна."


def test_regulation_mode_is_persistent_isolated_and_preserves_draft() -> None:
    mode_storage = InMemoryDialogModeStorage()
    modes = InMemoryDialogModeRepository(mode_storage)
    orchestrator = FakeOrchestrator(_active())
    qa = FakeRegulationService()
    first_adapter = TelegramIntakeAdapter(
        orchestrator,
        parser=FailingParser(),
        dialog_modes=modes,
        regulation_qa=qa,
    )

    intro = first_adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    assert intro.text == REGULATION_INTRO_TEXT
    assert _button_labels(intro.reply_markup) == ["⬅️ Главное меню"]
    assert modes.get_mode(USER_ID) == "regulation_qa"

    restarted = TelegramIntakeAdapter(
        orchestrator,
        parser=FailingParser(),
        dialog_modes=InMemoryDialogModeRepository(mode_storage),
        regulation_qa=qa,
    )
    answer = restarted.handle_text(USER_ID, 1001, 55, "Кто согласует закупку?")
    replay = restarted.handle_text(USER_ID, 1001, 55, "Кто согласует закупку?")

    assert qa.calls == ["Кто согласует закупку?"]
    assert "Источники:" in answer.text
    assert replay.replayed is True
    assert orchestrator.process_calls == 0
    assert orchestrator.active.intake_result.draft.item_name == "Мониторы"

    current = restarted.handle_menu(USER_ID, MENU_CURRENT)
    assert modes.get_mode(USER_ID) == "intake"
    assert "Мониторы" in current.text


def test_regulation_exit_and_new_request_switch_modes_without_rag_call() -> None:
    modes = InMemoryDialogModeRepository()
    qa = FakeRegulationService()
    adapter = TelegramIntakeAdapter(
        FakeOrchestrator(), dialog_modes=modes, regulation_qa=qa
    )
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)

    exited = adapter.handle_navigation_callback(
        USER_ID,
        "callback-end",
        encode_navigation_callback("regulations_end"),
    )
    assert modes.get_mode(USER_ID) == "idle"
    assert "главном меню" in exited.text

    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    created = adapter.handle_menu(USER_ID, MENU_NEW)
    assert modes.get_mode(USER_ID) == "intake"
    assert "Опишите новую потребность" in created.text
    assert qa.calls == []


def test_legacy_regulation_end_callback_still_returns_to_menu() -> None:
    modes = InMemoryDialogModeRepository()
    adapter = TelegramIntakeAdapter(FakeOrchestrator(), dialog_modes=modes)
    modes.set_mode(USER_ID, "regulation_qa")

    outcome = adapter.handle_navigation_callback(
        USER_ID,
        "legacy-regulations-end",
        encode_navigation_callback("regulations_end"),
    )

    assert modes.get_mode(USER_ID) == "idle"
    assert "главном меню" in outcome.text
