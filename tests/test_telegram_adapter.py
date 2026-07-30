import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.bot.__main__ import TelegramBotConfigurationError, main
from app.bot.adapter import TelegramIntakeAdapter, TelegramIntakeOutcome
from app.bot.formatters import (
    READY_TEXT,
    TELEGRAM_MESSAGE_LIMIT,
    WELCOME_TEXT,
    format_question,
)
from app.bot.handlers import (
    ERROR_TEXT,
    TelegramHandlerDependencies,
    handle_callback_query,
    handle_start,
    handle_text_message,
)
from app.bot.keyboards import LEGACY_MENU_EXAMPLES, encode_callback
from app.bot.parser import DeterministicIntakeParser
from app.bot.users import (
    ResolvedTelegramUser,
    TelegramUserProfile,
    TelegramUserRepositoryError,
    TelegramUserResolver,
)
from app.core.config import Settings
from app.intake.models import (
    CompletenessResult,
    FieldConflict,
    IntakeFieldUpdate,
    IntakeStatus,
    IntakeStepResult,
    NextQuestion,
    RequestDraftData,
)
from app.intake_persistence.exceptions import ActiveDraftNotFoundError
from app.intake_persistence.models import (
    PersistentDialogState,
    PersistentIntakeStepResult,
)
from app.schemas.user import UserCreate, UserRead

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeMessage:
    def __init__(self, text: str, *, chat_id: int = 1001, message_id: int = 42):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.from_user = SimpleNamespace(
            id=7001,
            username="test_user",
            first_name="Тест",
            last_name="Пользователь",
        )
        self.answers: list[str] = []
        self.reply_markups: list[object | None] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append(text)
        self.reply_markups.append(reply_markup)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object | None]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        self.messages.append((chat_id, text, reply_markup))


class FakeCallback:
    def __init__(self) -> None:
        self.id = "callback-1"
        self.data = f"rq:menu:{REQUEST_ID.hex}:1"
        self.message = None
        self.bot = FakeBot()
        self.from_user = SimpleNamespace(
            id=7001,
            username="test_user",
            first_name="Тест",
            last_name="Пользователь",
        )
        self.answer_calls = 0

    async def answer(self) -> None:
        self.answer_calls += 1


class FakeResolver:
    def __init__(self) -> None:
        self.profiles: list[TelegramUserProfile] = []

    def resolve(self, profile: TelegramUserProfile) -> UUID:
        self.profiles.append(profile)
        return USER_ID

    def resolve_user(self, profile: TelegramUserProfile) -> ResolvedTelegramUser:
        self.profiles.append(profile)
        return ResolvedTelegramUser(user_id=USER_ID, full_name="", department=None)


class FakeOrchestrator:
    def __init__(
        self,
        active: PersistentIntakeStepResult | None = None,
        result: PersistentIntakeStepResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.active = active
        self.result = result or persistent_result(
            IntakeStatus.COLLECTING,
            question("procurement_type", "Это товар, услуга или работа?", "choice"),
        )
        self.error = error
        self.process_calls: list[dict] = []

    def get_active_session(self, user_id):
        if self.error is not None:
            raise self.error
        if self.active is None:
            raise ActiveDraftNotFoundError("not found")
        return self.active

    def process_structured_step(
        self,
        user_id,
        update,
        request_id=None,
        incoming_message=None,
        idempotency_key=None,
    ):
        if self.error is not None:
            raise self.error
        self.process_calls.append(
            {
                "user_id": user_id,
                "update": update,
                "request_id": request_id,
                "incoming_message": incoming_message,
                "idempotency_key": idempotency_key,
            }
        )
        return self.result


def question(
    field_code: str,
    text: str,
    question_type: str = "free_text",
    options: list[str] | None = None,
) -> NextQuestion:
    return NextQuestion(
        field_code=field_code,
        text=text,
        question_type=question_type,
        options=options or [],
        reason="required",
        priority=1,
    )


def persistent_result(
    status: IntakeStatus,
    next_question: NextQuestion | None = None,
    draft: RequestDraftData | None = None,
) -> PersistentIntakeStepResult:
    intake = IntakeStepResult(
        status=status,
        draft=draft or RequestDraftData(),
        completeness=CompletenessResult(
            is_complete=status == IntakeStatus.READY_FOR_CONFIRMATION,
            required_fields=[],
            completed_fields=[],
            missing_fields=[],
            invalid_fields=[],
            blocked_fields=[],
            completion_ratio=Decimal("1")
            if status == IntakeStatus.READY_FOR_CONFIRMATION
            else Decimal("0"),
        ),
        next_question=next_question,
    )
    return PersistentIntakeStepResult(
        request_id=REQUEST_ID,
        user_id=USER_ID,
        request_version=1,
        intake_result=intake,
        dialog_state=PersistentDialogState(
            user_id=USER_ID,
            request_id=REQUEST_ID,
            intake_status=status,
            awaiting_field_code=next_question.field_code if next_question else None,
            next_question=next_question,
            state_version=1,
        ),
        persistence_status="saved",
    )


def dependencies(orchestrator: FakeOrchestrator):
    return TelegramHandlerDependencies(
        user_resolver=FakeResolver(),
        intake_adapter=TelegramIntakeAdapter(orchestrator),
    )


def test_start_without_active_draft_does_not_create_one() -> None:
    orchestrator = FakeOrchestrator()
    message = FakeMessage("/start")

    asyncio.run(handle_start(message, dependencies(orchestrator)))

    assert message.answers == [WELCOME_TEXT]
    assert orchestrator.process_calls == []


def test_start_with_collecting_draft_repeats_question() -> None:
    expected = question("amount", "Укажите общую сумму закупки.", "decimal")
    active = persistent_result(IntakeStatus.COLLECTING, expected)
    orchestrator = FakeOrchestrator(active=active)
    message = FakeMessage("/start")

    asyncio.run(handle_start(message, dependencies(orchestrator)))

    assert "незавершённая заявка" in message.answers[0]
    assert "Текущая заявка" in message.answers[0]
    assert orchestrator.process_calls == []


def test_start_with_ready_draft_does_not_confirm() -> None:
    orchestrator = FakeOrchestrator(
        active=persistent_result(IntakeStatus.READY_FOR_CONFIRMATION)
    )
    message = FakeMessage("/start")

    asyncio.run(handle_start(message, dependencies(orchestrator)))

    assert "незавершённая заявка" in message.answers[0]
    assert orchestrator.process_calls == []


def test_menu_message_is_routed_before_intake_parser() -> None:
    orchestrator = FakeOrchestrator()
    message = FakeMessage(LEGACY_MENU_EXAMPLES)

    asyncio.run(handle_text_message(message, dependencies(orchestrator)))

    assert "Пример товара" in message.answers[0]
    assert orchestrator.process_calls == []


def test_callback_query_is_always_answered() -> None:
    class CallbackAdapter:
        def handle_callback(self, user, callback_query_id, data):
            return TelegramIntakeOutcome(
                text="Действие обработано.",
                idempotency_key=callback_query_id,
                update=IntakeFieldUpdate(),
            )

    callback = FakeCallback()
    deps = TelegramHandlerDependencies(
        user_resolver=FakeResolver(),
        intake_adapter=CallbackAdapter(),  # type: ignore[arg-type]
    )

    asyncio.run(handle_callback_query(callback, deps))  # type: ignore[arg-type]

    assert callback.answer_calls == 1
    assert callback.bot.messages[0][1] == "Действие обработано."


def test_first_text_creates_minimal_update_and_returns_next_question() -> None:
    orchestrator = FakeOrchestrator()
    message = FakeMessage("Нужно купить 10 офисных кресел", chat_id=1001, message_id=51)

    asyncio.run(handle_text_message(message, dependencies(orchestrator)))

    call = orchestrator.process_calls[0]
    assert call["update"].values == {
        "procurement_type": "goods",
        "item_name": "офисные кресла",
        "quantity": Decimal("10"),
        "unit": "шт.",
        "category_code": "G02",
    }
    assert call["request_id"] is None
    assert call["idempotency_key"] == "telegram:1001:51"
    assert "Это товар или услуга?" in message.answers[0]


def test_answer_to_amount_is_normalized_and_orchestrator_called_once() -> None:
    active = persistent_result(
        IntakeStatus.COLLECTING,
        question("amount", "Укажите сумму.", "decimal"),
    )
    orchestrator = FakeOrchestrator(active=active)
    adapter = TelegramIntakeAdapter(orchestrator)

    outcome = adapter.handle_text(USER_ID, 1001, 52, "180 000")

    assert outcome.update.values == {"amount": Decimal("180000")}
    assert len(orchestrator.process_calls) == 1
    assert orchestrator.process_calls[0]["request_id"] == REQUEST_ID


@pytest.mark.parametrize(
    ("reply", "resolution"),
    [
        ("оставить", "keep"),
        ("оставить прежнее", "keep"),
        ("не менять", "keep"),
        ("подтвердить", "accept"),
        ("изменить", "accept"),
        ("применить", "accept"),
    ],
)
def test_pending_conflict_reply_is_resolved_without_new_field_value(
    reply: str,
    resolution: str,
) -> None:
    draft = RequestDraftData(
        desired_result="установить кондиционеры",
        conflicts=[
            FieldConflict(
                id="conflict-1",
                field_code="desired_result",
                current_value="установить кондиционеры",
                proposed_value="кондиционеры работают",
                message="Подтвердите изменение поля.",
            )
        ],
    )
    active = persistent_result(
        IntakeStatus.CONFLICT,
        question("desired_result", "Подтвердите изменение.", "confirmation"),
        draft,
    )
    result = persistent_result(
        IntakeStatus.COLLECTING,
        question("amount", "Укажите сумму.", "decimal"),
        RequestDraftData(desired_result="кондиционеры работают"),
    )
    orchestrator = FakeOrchestrator(active=active, result=result)
    adapter = TelegramIntakeAdapter(orchestrator)

    outcome = adapter.handle_text(USER_ID, 1001, 601, reply)

    assert len(orchestrator.process_calls) == 1
    update = orchestrator.process_calls[0]["update"]
    assert update.values == {}
    assert update.resolve_conflict_id == "conflict-1"
    assert update.conflict_resolution == resolution
    assert "сумму закупки" in outcome.text


def test_unknown_pending_conflict_reply_does_not_create_nested_conflict() -> None:
    conflict = FieldConflict(
        id="conflict-1",
        field_code="desired_result",
        current_value="установить кондиционеры",
        proposed_value="кондиционеры работают",
        message="Подтвердите изменение поля.",
    )
    active = persistent_result(
        IntakeStatus.CONFLICT,
        question("desired_result", "Подтвердите изменение.", "confirmation"),
        RequestDraftData(conflicts=[conflict]),
    )
    orchestrator = FakeOrchestrator(active=active)
    adapter = TelegramIntakeAdapter(orchestrator)

    first = adapter.handle_text(USER_ID, 1001, 602, "пока не уверен")
    replayed = adapter.handle_text(USER_ID, 1001, 602, "пока не уверен")

    assert orchestrator.process_calls == []
    assert first.text == replayed.text
    assert first.replayed is False
    assert replayed.replayed is True
    assert "подтвердить" in first.text
    assert "оставить" in first.text
    assert first.result.intake_result.draft.conflicts == [conflict]
    callback_data = [
        button.callback_data
        for row in first.reply_markup.inline_keyboard
        for button in row
    ]
    assert encode_callback("conflict_accept", REQUEST_ID, 1) in callback_data
    assert encode_callback("conflict_keep", REQUEST_ID, 1) in callback_data


@pytest.mark.parametrize(
    ("action", "resolution"),
    [("conflict_accept", "accept"), ("conflict_keep", "keep")],
)
def test_conflict_inline_callback_uses_the_same_resolution_contract(
    action: str,
    resolution: str,
) -> None:
    conflict = FieldConflict(
        id="conflict-1",
        field_code="desired_result",
        current_value="установить кондиционеры",
        proposed_value="кондиционеры работают",
        message="Подтвердите изменение поля.",
    )
    active = persistent_result(
        IntakeStatus.CONFLICT,
        question("desired_result", "Подтвердите изменение.", "confirmation"),
        RequestDraftData(conflicts=[conflict]),
    )
    result = persistent_result(
        IntakeStatus.COLLECTING,
        question("amount", "Укажите сумму.", "decimal"),
    )
    orchestrator = FakeOrchestrator(active=active, result=result)
    adapter = TelegramIntakeAdapter(orchestrator)

    outcome = adapter.handle_callback(
        USER_ID,
        "callback-conflict-1",
        encode_callback(action, REQUEST_ID, 1),
    )

    assert len(orchestrator.process_calls) == 1
    update = orchestrator.process_calls[0]["update"]
    assert update.resolve_conflict_id == "conflict-1"
    assert update.conflict_resolution == resolution
    assert outcome.result == result


def test_invalid_amount_returns_soft_hint_without_intake_call() -> None:
    active = persistent_result(
        IntakeStatus.COLLECTING,
        question("amount", "Укажите сумму.", "decimal"),
    )
    orchestrator = FakeOrchestrator(active=active)
    message = FakeMessage("примерно много")

    asyncio.run(handle_text_message(message, dependencies(orchestrator)))

    assert orchestrator.process_calls == []
    assert "не более 120 тыс." in message.answers[0]


def test_unknown_budget_is_acknowledged_and_moves_to_next_question() -> None:
    active = persistent_result(
        IntakeStatus.COLLECTING,
        question("budget_status", "Бюджет?", "choice"),
    )
    following = question("desired_delivery_date", "Дата?", "date")
    result = persistent_result(
        IntakeStatus.COLLECTING,
        following,
        RequestDraftData(procurement_type="goods", budget_status="unknown"),
    )
    orchestrator = FakeOrchestrator(active=active, result=result)
    adapter = TelegramIntakeAdapter(orchestrator)

    outcome = adapter.handle_text(USER_ID, 1001, 54, "не знаю")

    assert outcome.update.values == {"budget_status": "unknown"}
    assert "бюджет нужно уточнить" in outcome.text
    assert "К какой дате" in outcome.text
    assert "предусмотрена в утверждённом бюджете" not in outcome.text


@pytest.mark.parametrize("raw", ["31 февраля", "через пару недель"])
def test_invalid_date_returns_hint_without_intake_call(raw: str) -> None:
    active = persistent_result(
        IntakeStatus.COLLECTING,
        question("desired_delivery_date", "Укажите дату.", "date"),
    )
    orchestrator = FakeOrchestrator(active=active)
    message = FakeMessage(raw)

    asyncio.run(handle_text_message(message, dependencies(orchestrator)))

    assert orchestrator.process_calls == []
    assert "20 августа" in message.answers[0]


def test_category_option_is_reduced_to_code() -> None:
    parser = DeterministicIntakeParser()
    update = parser.parse(
        "G02 — Мебель и оснащение",
        question("category_code", "Выберите категорию.", "choice"),
    )
    assert update.values == {"category_code": "G02"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("да", True), ("нет", False), ("yes", True), ("false", False)],
)
def test_boolean_answers_use_existing_intake_validator(raw, expected) -> None:
    parser = DeterministicIntakeParser()
    update = parser.parse(
        raw,
        question("single_supplier", "Единственный поставщик?", "boolean"),
    )
    assert update.values == {"single_supplier": expected}


def test_awaiting_field_code_works_without_serialized_question() -> None:
    parser = DeterministicIntakeParser()
    update = parser.parse("12,5", awaiting_field_code="quantity")
    assert update.values == {"quantity": Decimal("12.5")}


def test_same_message_has_same_idempotency_key() -> None:
    first = TelegramIntakeAdapter.idempotency_key(1001, 53)
    second = TelegramIntakeAdapter.idempotency_key(1001, 53)
    assert first == second == "telegram:1001:53"


def test_question_formatter_respects_telegram_message_limit() -> None:
    options = [f"G{index:02d} — " + "Категория " * 100 for index in range(50)]
    rendered = format_question(
        question("custom_field", "Выберите значение.", "choice", options)
    )
    assert len(rendered) <= TELEGRAM_MESSAGE_LIMIT
    assert rendered.endswith("…")


def test_ready_for_confirmation_does_not_call_lifecycle() -> None:
    ready = persistent_result(IntakeStatus.READY_FOR_CONFIRMATION)
    orchestrator = FakeOrchestrator(result=ready)
    message = FakeMessage("Нужны офисные кресла")

    asyncio.run(handle_text_message(message, dependencies(orchestrator)))

    assert message.answers == [READY_TEXT]
    assert len(orchestrator.process_calls) == 1


def test_text_for_already_ready_draft_does_not_mutate_intake() -> None:
    ready = persistent_result(IntakeStatus.READY_FOR_CONFIRMATION)
    orchestrator = FakeOrchestrator(active=ready)
    message = FakeMessage("Подтверждаю")

    asyncio.run(handle_text_message(message, dependencies(orchestrator)))

    assert message.answers == [READY_TEXT]
    assert orchestrator.process_calls == []


def test_unexpected_backend_error_is_logged_and_hidden(caplog) -> None:
    orchestrator = FakeOrchestrator(error=RuntimeError("backend failed"))
    message = FakeMessage("Текст, который не должен попасть в лог")

    with caplog.at_level("ERROR"):
        asyncio.run(handle_text_message(message, dependencies(orchestrator)))

    assert message.answers == [ERROR_TEXT]
    assert "Текст, который не должен попасть в лог" not in caplog.text
    assert "Traceback" not in message.answers[0]


def test_extraction_debug_masks_telegram_identifiers(caplog) -> None:
    adapter = TelegramIntakeAdapter(FakeOrchestrator(), extraction_debug=True)

    with caplog.at_level("INFO"):
        adapter._debug_event(
            "telegram:987654321:12345",
            "deterministic_parse",
            candidate_fields=["amount"],
        )

    assert "message_ref=" in caplog.text
    assert "telegram:987654321:12345" not in caplog.text
    assert "987654321" not in caplog.text


class FakeUserRepository:
    def __init__(self, existing: UserRead | None = None) -> None:
        self.existing = existing
        self.created: list[UserCreate] = []

    def get_by_telegram_id(self, telegram_id: int) -> UserRead | None:
        if self.existing and self.existing.telegram_id == telegram_id:
            return self.existing
        return None

    def create(self, user: UserCreate) -> UserRead:
        self.created.append(user)
        now = datetime.now(UTC)
        self.existing = UserRead(
            **user.model_dump(),
            id=USER_ID,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        return self.existing


def test_user_resolver_returns_existing_user_by_numeric_telegram_id() -> None:
    now = datetime.now(UTC)
    existing = UserRead(
        id=USER_ID,
        telegram_id=7001,
        full_name="Существующий пользователь",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    repository = FakeUserRepository(existing)
    resolver = TelegramUserResolver(repository)

    resolved = resolver.resolve(
        TelegramUserProfile(telegram_id=7001, username="changed_username")
    )

    assert resolved == USER_ID
    assert repository.created == []


def test_user_resolver_creates_new_user_once_and_username_is_not_identity() -> None:
    repository = FakeUserRepository()
    resolver = TelegramUserResolver(repository)

    first = resolver.resolve(
        TelegramUserProfile(
            telegram_id=7001,
            username="first_username",
            first_name="Тест",
            last_name="Пользователь",
        )
    )
    second = resolver.resolve(
        TelegramUserProfile(telegram_id=7001, username="renamed_username")
    )

    assert first == second == USER_ID
    assert len(repository.created) == 1
    assert repository.created[0].telegram_id == 7001
    assert repository.created[0].full_name == "Тест Пользователь"


def test_user_resolver_recovers_from_concurrent_unique_insert() -> None:
    class ConcurrentRepository(FakeUserRepository):
        def create(self, user: UserCreate) -> UserRead:
            now = datetime.now(UTC)
            self.existing = UserRead(
                **user.model_dump(),
                id=USER_ID,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            raise TelegramUserRepositoryError("unique conflict")

    resolver = TelegramUserResolver(ConcurrentRepository())
    assert resolver.resolve(TelegramUserProfile(telegram_id=7001)) == USER_ID


def test_bot_configuration_does_not_require_token_during_import() -> None:
    settings = Settings(_env_file=None, telegram_bot_token=None)
    with pytest.raises(
        TelegramBotConfigurationError,
        match="TELEGRAM_BOT_TOKEN is required",
    ):
        asyncio.run(main(settings))


def test_invalid_bot_token_is_rejected_before_supabase_wiring() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="invalid token",
        supabase_url="https://unused.example",
        supabase_service_role_key="unused",
    )
    with pytest.raises(
        TelegramBotConfigurationError,
        match="invalid format",
    ):
        asyncio.run(main(settings))


def test_initial_schema_has_unique_numeric_telegram_identity() -> None:
    sql = open("scripts/sql/001_initial_schema.sql", encoding="utf-8").read()
    assert "telegram_id bigint unique" in sql.casefold()
