from copy import deepcopy
from datetime import date
from uuid import UUID

import pytest

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.formatters import (
    ACTIVE_DRAFT_NOTICE,
    EXAMPLES_TEXT,
    HELP_TEXT,
    NEW_REQUEST_PROMPT,
    WELCOME_TEXT,
    format_intake_result,
)
from app.bot.keyboards import (
    MENU_CURRENT,
    MENU_EXAMPLES,
    MENU_HELP,
    MENU_INSTRUCTION,
    MENU_MY_REQUESTS,
    MENU_NEW,
    MENU_REGULATIONS,
    encode_callback,
    main_menu,
    parse_callback,
)
from app.bot.users import ResolvedTelegramUser
from app.intake.models import IntakeFieldUpdate, IntakeStatus
from app.intake.service import RequestIntakeService
from app.intake_persistence.exceptions import ActiveDraftNotFoundError
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.request_lifecycle.repositories import InMemoryRequestLifecycleRepository
from app.request_lifecycle.service import RequestLifecycleService
from app.rules.repository import InMemoryApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from app.schemas.common import RequestStatus
from scripts.validate_approval_rules import load_rule_seed

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("33333333-3333-4333-8333-333333333333")
REFERENCE_DATE = date(2026, 8, 28)


def _stack():
    storage = InMemoryIntakeStorage()
    _, base, additional = load_rule_seed()
    core = RequestIntakeService(
        ApprovalRuleService(InMemoryApprovalRuleRepository(base, additional))
    )
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    return (
        storage,
        intake,
        lifecycle,
        TelegramIntakeAdapter(intake, lifecycle_service=lifecycle),
    )


def _full_goods(
    *,
    budget_status: str = "budgeted",
    modifier: str = "exact",
) -> IntakeFieldUpdate:
    return IntakeFieldUpdate(
        values={
            "procurement_type": "goods",
            "category_code": "G02",
            "item_name": "Офисные кресла",
            "quantity": "10",
            "unit": "шт.",
            "specifications": "Эргономичные, с регулируемой спинкой",
            "amount": "120000",
            "budget_status": budget_status,
            "desired_delivery_date": REFERENCE_DATE,
            "delivery_location": "Офис",
            "business_justification": "Оснащение рабочих мест",
            "department": "АХО",
            "contact_person": "Александра",
        },
        evidence_by_field={"amount": f"amount_modifier={modifier}"},
    )


def _full_service() -> IntakeFieldUpdate:
    return IntakeFieldUpdate(
        values={
            "procurement_type": "service",
            "category_code": "S02",
            "item_name": "Уборка офиса",
            "description": "Еженедельная уборка",
            "specifications": "По будням после 19:00",
            "amount": "80000",
            "budget_status": "unbudgeted",
            "desired_delivery_date": REFERENCE_DATE,
            "work_on_site": True,
            "delivery_location": "Офис",
            "business_justification": "Поддержание чистоты",
            "department": "АХО",
            "contact_person": "Александра",
        }
    )


def _ready(intake, update=None):
    result = intake.process_structured_step(
        USER_ID,
        update or _full_goods(),
        idempotency_key="ready-intake",
    )
    assert result.intake_result.status == IntakeStatus.READY_FOR_CONFIRMATION
    return result


def _callback(markup, label: str) -> str:
    return next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.text == label
    )


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_main_menu_and_start_do_not_create_draft() -> None:
    _, intake, _, adapter = _stack()
    assert adapter.start_message(USER_ID) == WELCOME_TEXT
    assert "Нужно купить" not in WELCOME_TEXT
    keyboard = main_menu()
    assert [button.text for row in keyboard.keyboard for button in row] == [
        MENU_NEW,
        MENU_CURRENT,
        MENU_MY_REQUESTS,
        MENU_INSTRUCTION,
        MENU_REGULATIONS,
    ]
    try:
        intake.get_active_session(USER_ID)
    except ActiveDraftNotFoundError:
        pass
    else:
        raise AssertionError("/start must not create a draft")


def test_start_with_active_draft_only_adds_notice() -> None:
    _, intake, _, adapter = _stack()
    saved = intake.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"procurement_type": "goods"})
    )
    assert ACTIVE_DRAFT_NOTICE in adapter.start_message(USER_ID)
    assert intake.get_active_session(USER_ID).request_id == saved.request_id


def test_menu_content_never_mutates_active_draft() -> None:
    _, intake, _, adapter = _stack()
    saved = intake.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"procurement_type": "goods"})
    )
    version = saved.request_version
    for command, expected in (
        (MENU_EXAMPLES, EXAMPLES_TEXT),
        (MENU_HELP, HELP_TEXT),
        (MENU_CURRENT, "Следующий шаг"),
    ):
        outcome = adapter.handle_menu(USER_ID, command)
        assert expected in outcome.text
        assert outcome.update.values == {}
        assert intake.get_active_session(USER_ID).request_version == version


def test_new_request_without_active_only_prompts_for_description() -> None:
    _, intake, _, adapter = _stack()
    outcome = adapter.handle_menu(USER_ID, MENU_NEW)
    assert outcome.text == NEW_REQUEST_PROMPT
    try:
        intake.get_active_session(USER_ID)
    except ActiveDraftNotFoundError:
        pass
    else:
        raise AssertionError("menu must not create an empty draft")


def test_new_request_with_active_requires_double_cancel_confirmation() -> None:
    storage, intake, _, adapter = _stack()
    active = intake.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"procurement_type": "goods"})
    )
    menu = adapter.handle_menu(USER_ID, MENU_NEW)
    assert "сначала отменить" in menu.text
    assert storage.requests[active.request_id].status == RequestStatus.DRAFT

    ask = adapter.handle_callback(
        USER_ID,
        "cb-cancel-new-ask",
        _callback(menu.reply_markup, "Отменить и начать новую"),
    )
    assert "Отменить эту заявку?" in ask.text
    assert storage.requests[active.request_id].status == RequestStatus.DRAFT

    cancelled = adapter.handle_callback(
        USER_ID,
        "cb-cancel-new-yes",
        _callback(ask.reply_markup, "Да, отменить"),
    )
    assert NEW_REQUEST_PROMPT in cancelled.text
    assert cancelled.reply_markup is None
    assert storage.requests[active.request_id].status == RequestStatus.CANCELLED
    repeated = adapter.handle_menu(USER_ID, MENU_NEW)
    assert repeated.text == "Можно отправлять описание новой заявки."
    with pytest.raises(ActiveDraftNotFoundError):
        intake.get_active_session(USER_ID)


def test_goods_card_uses_human_labels_modifier_and_resolved_actions() -> None:
    _, intake, _, adapter = _stack()
    ready = _ready(intake, _full_goods(modifier="maximum"))
    outcome = adapter.handle_menu(USER_ID, MENU_CURRENT)
    assert outcome.result.request_id == ready.request_id
    assert "Тип закупки: Товар" in outcome.text
    assert "Категория: Мебель и оснащение (G02)" in outcome.text
    assert "Что требуется: Офисные кресла" in outcome.text
    assert "Количество: 10 шт." in outcome.text
    assert "Характеристики товара:" in outcome.text
    assert "Ориентировочная сумма: не более 120 000 ₽" in outcome.text
    assert "Бюджет: Предусмотрена" in outcome.text
    assert str(ready.request_id) not in outcome.text
    assert "goods" not in outcome.text
    assert "amount_modifier" not in outcome.text
    assert _labels(outcome.reply_markup) == [
        "✅ Подтвердить и отправить",
        "✏️ Изменить",
        "❌ Отменить",
    ]


def test_ready_result_is_automatically_formatted_as_request_card() -> None:
    _, intake, _, _ = _stack()
    ready = _ready(intake)

    text = format_intake_result(ready)

    assert "Проверьте заявку перед отправкой" in text
    assert "Тип закупки: Товар" in text


def test_service_card_uses_service_labels_and_approximate_amount() -> None:
    _, intake, _, adapter = _stack()
    update = _full_service()
    update.evidence_by_field["amount"] = "amount_modifier=approximate"
    _ready(intake, update)
    text = adapter.handle_menu(USER_ID, MENU_CURRENT).text
    assert "Тип закупки: Услуга" in text
    assert "Какая услуга требуется: Уборка офиса" in text
    assert "Объём и требования: Еженедельная уборка" in text
    assert "По будням после 19:00" in text
    assert "Ориентировочная сумма: около 80 000 ₽" in text
    assert "Бюджет: Не предусмотрена" in text
    assert "Характеристики товара" not in text


def test_service_card_removes_item_repetition_from_description() -> None:
    _, intake, _, adapter = _stack()
    update = _full_service()
    update.values["description"] = "Уборка офиса по будням после 19:00"
    update.values["specifications"] = "По будням после 19:00"
    _ready(intake, update)

    text = adapter.handle_menu(USER_ID, MENU_CURRENT).text

    assert text.count("Уборка офиса") == 1
    assert text.count("По будням после 19:00") == 1


def test_service_card_separates_result_and_deduplicates_requirements() -> None:
    _, intake, _, adapter = _stack()
    update = _full_service()
    update.values.update(
        {
            "item_name": "Установка кондиционеров",
            "description": (
                "Кондиционеры обеспечивают охлаждение воздуха, работы проводить утром"
            ),
            "specifications": (
                "Два кондиционера в переговорных комнатах; "
                "работы проводить в утренние часы"
            ),
            "desired_result": "Кондиционеры обеспечивают охлаждение воздуха",
            "category_code": "S01",
        }
    )
    _ready(intake, update)

    text = adapter.handle_menu(USER_ID, MENU_CURRENT).text

    assert "Объём и требования:" in text
    assert "Два кондиционера в переговорных комнатах" in text
    assert text.casefold().count("работы проводить") == 1
    assert "Ожидаемый результат: Кондиционеры обеспечивают охлаждение воздуха" in text
    assert "Срок оказания услуги:" in text


def test_unknown_card_has_warning_without_confirm() -> None:
    _, intake, _, adapter = _stack()
    _ready(intake, _full_goods(budget_status="unknown"))
    outcome = adapter.handle_menu(USER_ID, MENU_CURRENT)
    assert "Бюджет: Требуется уточнение" in outcome.text
    assert "заявку нельзя зарегистрировать" in outcome.text
    labels = _labels(outcome.reply_markup)
    assert "✅ Подтвердить и отправить" not in labels
    assert labels == ["💰 Уточнить бюджет", "✏️ Изменить заявку", "❌ Отменить"]


def test_callback_data_is_short_and_round_trips() -> None:
    request_id = UUID("22222222-2222-4222-8222-222222222222")
    encoded = encode_callback("confirm", request_id, 123456)
    assert len(encoded.encode("utf-8")) <= 64
    parsed = parse_callback(encoded)
    assert parsed.request_id == request_id
    assert parsed.version == 123456
    assert parsed.action == "confirm"


def test_confirm_is_idempotent_and_removes_active_draft() -> None:
    storage, intake, _, adapter = _stack()
    ready = _ready(intake)
    card = adapter.handle_menu(USER_ID, MENU_CURRENT)
    data = _callback(card.reply_markup, "✅ Подтвердить и отправить")

    confirmed = adapter.handle_callback(USER_ID, "confirm-query", data)
    assert "Заявка зарегистрирована" in confirmed.text
    assert "PR-" in confirmed.text
    registered = deepcopy(storage.requests[ready.request_id])
    assert registered.status == RequestStatus.NEW
    try:
        intake.get_active_session(USER_ID)
    except ActiveDraftNotFoundError:
        pass
    else:
        raise AssertionError("registered request must not remain active")

    replay = adapter.handle_callback(USER_ID, "confirm-query", data)
    assert replay.text == "Эта заявка уже зарегистрирована."
    assert storage.requests[ready.request_id].version == registered.version
    assert storage.lifecycle_sequence == 1


def test_registered_request_cannot_be_returned_to_editing() -> None:
    _, intake, _, adapter = _stack()
    _ready(intake)
    card = adapter.handle_menu(USER_ID, MENU_CURRENT)
    confirm_data = _callback(card.reply_markup, "✅ Подтвердить и отправить")
    edit_data = _callback(card.reply_markup, "✏️ Изменить")

    adapter.handle_callback(USER_ID, "confirm-before-edit", confirm_data)
    outcome = adapter.handle_callback(USER_ID, "edit-registered", edit_data)

    assert outcome.text == "Эта заявка уже зарегистрирована."


def test_stale_and_foreign_callbacks_are_safe() -> None:
    _, intake, _, adapter = _stack()
    ready = _ready(intake)
    stale = encode_callback("confirm", ready.request_id, ready.request_version - 1)
    stale_outcome = adapter.handle_callback(USER_ID, "stale", stale)
    assert "актуальную версию" in stale_outcome.text
    assert stale_outcome.reply_markup is not None

    foreign = adapter.handle_callback(OTHER_USER_ID, "foreign", stale)
    assert foreign.text == "Не удалось выполнить действие для этой заявки."
    assert str(ready.request_id) not in foreign.text


def test_return_to_edit_uses_lifecycle_and_allows_explicit_update() -> None:
    _, intake, _, adapter = _stack()
    _ready(intake)
    card = adapter.handle_menu(USER_ID, MENU_CURRENT)
    edited = adapter.handle_callback(
        USER_ID, "edit-query", _callback(card.reply_markup, "✏️ Изменить")
    )
    assert "можно изменить" in edited.text
    active = intake.get_active_session(USER_ID)
    assert active.dialog_state.intake_status == IntakeStatus.EDITING

    changed = adapter.handle_text(USER_ID, 1001, 77, "Нужно купить 5 ноутбуков")
    assert changed.update.explicit_correction is True
    assert changed.result.intake_result.draft.item_name == "ноутбуки"
    assert changed.result.intake_result.draft.quantity == 5


def test_cancel_requires_confirmation_and_repeat_is_safe() -> None:
    storage, intake, _, adapter = _stack()
    ready = _ready(intake)
    card = adapter.handle_menu(USER_ID, MENU_CURRENT)
    ask = adapter.handle_callback(
        USER_ID,
        "cancel-ask",
        _callback(card.reply_markup, "❌ Отменить"),
    )
    assert storage.requests[ready.request_id].status == RequestStatus.DRAFT
    yes = _callback(ask.reply_markup, "Да, отменить")
    cancelled = adapter.handle_callback(USER_ID, "cancel-yes", yes)
    assert "Заявка отменена" in cancelled.text
    assert storage.requests[ready.request_id].status == RequestStatus.CANCELLED
    repeated = adapter.handle_callback(USER_ID, "cancel-again", yes)
    assert repeated.text == "Эта заявка уже отменена."


def test_cancelled_request_cannot_be_confirmed() -> None:
    _, intake, _, adapter = _stack()
    _ready(intake)
    card = adapter.handle_menu(USER_ID, MENU_CURRENT)
    confirm_data = _callback(card.reply_markup, "✅ Подтвердить и отправить")
    ask = adapter.handle_callback(
        USER_ID,
        "cancel-before-confirm",
        _callback(card.reply_markup, "❌ Отменить"),
    )
    adapter.handle_callback(
        USER_ID,
        "cancel-before-confirm-yes",
        _callback(ask.reply_markup, "Да, отменить"),
    )

    outcome = adapter.handle_callback(USER_ID, "confirm-cancelled", confirm_data)

    assert outcome.text == "Эта заявка уже отменена."


def test_unknown_budget_can_be_corrected_through_persistent_callbacks() -> None:
    _, intake, _, adapter = _stack()
    _ready(intake, _full_goods(budget_status="unknown"))
    card = adapter.handle_menu(USER_ID, MENU_CURRENT)
    budget = adapter.handle_callback(
        USER_ID,
        "budget-edit",
        _callback(card.reply_markup, "💰 Уточнить бюджет"),
    )
    assert "предусмотрена" in budget.text
    answer = adapter.handle_callback(
        USER_ID,
        "budget-answer",
        _callback(budget.reply_markup, "Да, предусмотрена"),
    )
    assert answer.result.intake_result.draft.budget_status == "budgeted"
    assert answer.result.intake_result.approval_route.status == "resolved"
    assert "✅ Подтвердить и отправить" in _labels(answer.reply_markup)


def test_new_request_after_registration_is_independent() -> None:
    storage, intake, _, adapter = _stack()
    ready_a = _ready(intake)
    card = adapter.handle_menu(USER_ID, MENU_CURRENT)
    confirmed = adapter.handle_callback(
        USER_ID,
        "confirm-a",
        _callback(card.reply_markup, "✅ Подтвердить и отправить"),
    )
    snapshot_a = deepcopy(storage.requests[ready_a.request_id].data["lifecycle"])
    assert confirmed.reply_markup is None
    assert confirmed.text.endswith(NEW_REQUEST_PROMPT)

    profile = ResolvedTelegramUser(USER_ID, "Александра", "АХО")
    created_b = adapter.handle_text(profile, 1001, 88, "Нужно купить 5 ноутбуков")
    assert created_b.result.request_id != ready_a.request_id
    draft_b = created_b.result.intake_result.draft
    assert draft_b.item_name == "ноутбуки"
    assert draft_b.quantity == 5
    assert draft_b.amount is None
    assert draft_b.budget_status is None
    assert draft_b.contact_person == "Александра"
    assert draft_b.department == "АХО"
    assert storage.requests[ready_a.request_id].data["lifecycle"] == snapshot_a
    current = adapter.handle_menu(USER_ID, MENU_CURRENT)
    assert current.result.request_id == created_b.result.request_id
