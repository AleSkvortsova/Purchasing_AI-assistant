from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.dialog_modes import (
    InMemoryDialogModeRepository,
    InMemoryDialogModeStorage,
    SupabaseDialogModeRepository,
)
from app.bot.keyboards import MENU_INSTRUCTION, MENU_REGULATIONS
from app.intake_persistence.exceptions import ActiveDraftNotFoundError
from app.rag.answering import (
    FakeGroundedAnswerProvider,
    GroundedAnswerPayload,
    RegulationQuestionAnsweringService,
)
from app.rag.conversation import RegulationPendingClarification
from scripts.evaluate_retrieval import build_offline_service

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class NoActiveIntake:
    def get_active_session(self, user_id):
        del user_id
        raise ActiveDraftNotFoundError("not found")

    def process_structured_step(self, *args, **kwargs):
        raise AssertionError("intake must not run in regulation mode")


class StatefulSupabaseClient:
    def __init__(self) -> None:
        self.dialog_states: dict[str, dict] = {}
        self.message_logs: list[dict] = []

    def table(self, name: str):
        return StatefulSupabaseQuery(self, name)


class StatefulSupabaseQuery:
    def __init__(self, client: StatefulSupabaseClient, table: str) -> None:
        self.client = client
        self.table = table
        self.filters: list[tuple[str, object]] = []
        self.operation = "select"
        self.payload: dict | None = None

    def select(self, columns: str):
        del columns
        return self

    def eq(self, field: str, value: object):
        self.filters.append((field, value))
        return self

    def limit(self, value: int):
        del value
        return self

    def upsert(self, payload: dict, **kwargs):
        del kwargs
        self.operation = "upsert"
        self.payload = payload
        return self

    def insert(self, payload: dict):
        self.operation = "insert"
        self.payload = payload
        return self

    def execute(self):
        if self.operation == "upsert":
            assert self.payload is not None
            user_id = str(self.payload["user_id"])
            current = self.client.dialog_states.setdefault(user_id, {})
            current.update(self.payload)
            return SimpleNamespace(data=[dict(current)])
        if self.operation == "insert":
            assert self.payload is not None
            self.client.message_logs.append(dict(self.payload))
            return SimpleNamespace(data=[dict(self.payload)])
        rows = (
            list(self.client.dialog_states.values())
            if self.table == "dialog_states"
            else self.client.message_logs
        )
        return SimpleNamespace(
            data=[
                dict(row)
                for row in rows
                if all(row.get(field) == value for field, value in self.filters)
            ]
        )


@pytest.fixture(scope="module")
def qa_service() -> RegulationQuestionAnsweringService:
    provider = FakeGroundedAnswerProvider(
        GroundedAnswerPayload(
            answer="",
            claims=[],
            insufficient_context=True,
            source_conflict=False,
        )
    )
    return RegulationQuestionAnsweringService(build_offline_service(), provider)


def _adapter(
    storage: InMemoryDialogModeStorage,
    qa_service: RegulationQuestionAnsweringService,
) -> TelegramIntakeAdapter:
    return TelegramIntakeAdapter(
        NoActiveIntake(),
        dialog_modes=InMemoryDialogModeRepository(storage),
        regulation_qa=qa_service,
    )


def _result(storage: InMemoryDialogModeStorage, message_id: int):
    key = f"telegram:1001:{message_id}"
    return storage.replays[(USER_ID, key)][1]


def test_approval_clarification_survives_adapter_restart(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    first = _adapter(storage, qa_service)
    first.handle_menu(USER_ID, MENU_REGULATIONS)

    clarification = first.handle_text(
        USER_ID,
        1001,
        1,
        "На закупку нужно 240 тысяч рублей. Кто должен одобрить заявку?",
    )
    pending = storage.pending_regulation[USER_ID]

    assert "предусмотрена ли закупка бюджетом" in clarification.text.casefold()
    assert pending.known_slots.amount == 240000
    assert pending.primary_intent == "approval_route"
    assert pending.missing_slots == ("budget_status",)

    restarted = _adapter(storage, qa_service)
    answer = restarted.handle_text(USER_ID, 1001, 2, "да")
    result = _result(storage, 2)

    assert result.status == "answered"
    assert "финансовый контролёр" in answer.text
    assert result.diagnostics["conversation_slots"] == {
        "amount": "240000",
        "budget_status": "budgeted",
    }
    assert USER_ID not in storage.pending_regulation


@pytest.mark.parametrize(
    ("reply", "budget_status", "answer_term"),
    [
        ("да", "budgeted", "финансовый контролёр"),
        ("предусмотрена", "budgeted", "финансовый контролёр"),
        ("предусмотрена бюджетом", "budgeted", "финансовый контролёр"),
        (
            "закупка предусмотрена бюджетом",
            "budgeted",
            "финансовый контролёр",
        ),
        ("нет", "unbudgeted", "генеральный директор"),
    ],
)
def test_budget_clarification_short_answers(
    qa_service: RegulationQuestionAnsweringService,
    reply: str,
    budget_status: str,
    answer_term: str,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(
        USER_ID,
        1001,
        10,
        "Закупка стоит 240 тысяч рублей. Кто её согласует?",
    )

    outcome = adapter.handle_text(USER_ID, 1001, 11, reply)
    result = _result(storage, 11)

    assert result.status == "answered"
    assert result.diagnostics["conversation_slots"]["budget_status"] == budget_status
    assert answer_term in outcome.text


def test_unknown_budget_returns_controlled_answer_without_loop(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(
        USER_ID,
        1001,
        20,
        "Закупка стоит 240 тысяч рублей. Кто её согласует?",
    )

    outcome = adapter.handle_text(USER_ID, 1001, 21, "не знаю")
    assert _result(storage, 21).status == "clarification_required"
    assert outcome.text == (
        "Без бюджетного статуса нельзя однозначно определить маршрут "
        "согласования. Уточните, предусмотрена ли закупка бюджетом, у "
        "ответственного за бюджет подразделения или финансового контролёра."
    )
    result = _result(storage, 21)
    assert result.diagnostics["conversation_slots"]["budget_status"] == "unknown"
    assert result.refusal_reason == "unknown_budget_status"
    assert USER_ID not in storage.pending_regulation


def test_compound_reply_fills_amount_and_budget(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(USER_ID, 1001, 30, "Кто должен согласовать закупку?")

    outcome = adapter.handle_text(
        USER_ID,
        1001,
        31,
        "180 тыс., предусмотрена",
    )
    result = _result(storage, 31)

    assert result.status == "answered"
    assert "финансовый контролёр" in outcome.text
    assert result.diagnostics["conversation_slots"]["amount"] == "180000"


@pytest.mark.parametrize(
    ("reply", "amount", "budget_status", "answer_term"),
    [
        ("180 тыс., предусмотрена", "180000", "budgeted", "финансовый контролёр"),
        (
            "450 тыс., не предусмотрена",
            "450000",
            "unbudgeted",
            "генеральный директор",
        ),
        (
            "сумма 550 тысяч, деньги в бюджете есть",
            "550000",
            "budgeted",
            "руководитель закупок",
        ),
    ],
)
def test_pending_approval_intent_wins_over_compound_slot_reply(
    qa_service: RegulationQuestionAnsweringService,
    reply: str,
    amount: str,
    budget_status: str,
    answer_term: str,
) -> None:
    storage = InMemoryDialogModeStorage()
    first = _adapter(storage, qa_service)
    first.handle_menu(USER_ID, MENU_REGULATIONS)
    first.handle_text(
        USER_ID,
        1001,
        32,
        "Я не уверена, можно ли понять маршрут согласования?",
    )
    restarted = _adapter(storage, qa_service)
    outcome = restarted.handle_text(USER_ID, 1001, 33, reply)
    result = _result(storage, 33)

    assert result.status == "answered"
    assert result.diagnostics["conversation_primary_intent"] == "approval_route"
    assert result.diagnostics["conversation_slots"] == {
        "amount": amount,
        "budget_status": budget_status,
    }
    assert [source.document_id for source in result.sources] == ["kb-009"]
    assert answer_term in outcome.text
    assert USER_ID not in storage.pending_regulation


def test_compound_reply_survives_real_dialog_repository_round_trip(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    client = StatefulSupabaseClient()
    first = TelegramIntakeAdapter(
        NoActiveIntake(),
        dialog_modes=SupabaseDialogModeRepository(client),  # type: ignore[arg-type]
        regulation_qa=qa_service,
    )
    first.handle_menu(USER_ID, MENU_REGULATIONS)
    first.handle_text(
        USER_ID,
        1001,
        39,
        "Я не уверена, можно ли понять маршрут согласования?",
    )
    persisted_pending = RegulationPendingClarification.model_validate(
        client.dialog_states[str(USER_ID)]["state_data"][
            "regulation_pending_clarification"
        ]
    )
    assert persisted_pending.primary_intent == "approval_route"
    assert persisted_pending.missing_slots == ("amount", "budget_status")

    restarted = TelegramIntakeAdapter(
        NoActiveIntake(),
        dialog_modes=SupabaseDialogModeRepository(client),  # type: ignore[arg-type]
        regulation_qa=qa_service,
    )
    outcome = restarted.handle_text(
        USER_ID,
        1001,
        40,
        "180 тыс., предусмотрена",
    )
    stored = client.message_logs[-1]["idempotency_result"]

    assert stored["status"] == "answered"
    assert stored["sources"][0]["document_id"] == "kb-009"
    assert "финансовый контролёр" in outcome.text
    assert "regulation_pending_clarification" not in client.dialog_states[
        str(USER_ID)
    ]["state_data"]


def test_repeated_clarification_is_stopped(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(USER_ID, 1001, 34, "Кто согласует закупку?")

    first_followup = adapter.handle_text(USER_ID, 1001, 35, "180 тысяч рублей")
    assert "предусмотрена ли закупка бюджетом" in first_followup.text.casefold()
    stopped = adapter.handle_text(USER_ID, 1001, 36, "пока не уточнила")
    result = _result(storage, 36)

    assert result.status == "clarification_required"
    assert result.refusal_reason == "repeated_clarification"
    assert "задайте новый вопрос" in stopped.text.casefold()
    assert USER_ID not in storage.pending_regulation


def test_maximum_clarification_steps_returns_safe_explanation(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(USER_ID, 1001, 37, "Кто согласует закупку?")
    pending = storage.pending_regulation[USER_ID]
    storage.pending_regulation[USER_ID] = pending.model_copy(
        update={
            "clarification_step": 3,
            "last_clarifying_question_fingerprint": "different-question",
        }
    )

    outcome = adapter.handle_text(USER_ID, 1001, 38, "пока не уточнила")
    result = _result(storage, 38)

    assert result.refusal_reason == "clarification_step_limit"
    assert "задайте новый вопрос" in outcome.text.casefold()
    assert USER_ID not in storage.pending_regulation


@pytest.mark.parametrize("reply", ["товар", "услуга"])
def test_required_fields_clarification_uses_purchase_type(
    qa_service: RegulationQuestionAnsweringService,
    reply: str,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    first = adapter.handle_text(USER_ID, 1001, 40, "Что мне указать?")
    assert "товар или услугу" in first.text

    outcome = adapter.handle_text(USER_ID, 1001, 41, reply)
    result = _result(storage, 41)

    assert result.status == "answered"
    assert "общие обязательные сведения" in outcome.text
    assert USER_ID not in storage.pending_regulation


def test_menu_exit_clears_pending_context(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(
        USER_ID,
        1001,
        50,
        "Закупка стоит 240 тысяч рублей. Кто её согласует?",
    )
    assert USER_ID in storage.pending_regulation

    adapter.handle_menu(USER_ID, MENU_INSTRUCTION)

    assert USER_ID not in storage.pending_regulation


def test_new_complete_question_replaces_pending_context(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(
        USER_ID,
        1001,
        60,
        "Закупка стоит 240 тысяч рублей. Кто её согласует?",
    )

    outcome = adapter.handle_text(
        USER_ID,
        1001,
        61,
        "До выставки осталось пять дней. Что писать в заявке?",
    )
    result = _result(storage, 61)

    assert result.status == "answered"
    assert "5 дней" in outcome.text
    assert "P2" in outcome.text
    assert USER_ID not in storage.pending_regulation


def test_expired_pending_context_is_not_reused(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)
    adapter.handle_text(
        USER_ID,
        1001,
        70,
        "Закупка стоит 240 тысяч рублей. Кто её согласует?",
    )
    old = datetime.now(UTC) - timedelta(hours=1)
    storage.pending_regulation[USER_ID] = storage.pending_regulation[
        USER_ID
    ].model_copy(update={"created_at": old})

    adapter.handle_text(USER_ID, 1001, 71, "да")
    result = _result(storage, 71)

    assert result.status != "answered"
    assert result.diagnostics["expired_context"] is True
    renewed = storage.pending_regulation[USER_ID]
    assert renewed.original_question == "да"
    assert renewed.known_slots.amount is None


def test_plan_phrase_is_answered_without_unnecessary_clarification(
    qa_service: RegulationQuestionAnsweringService,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)

    outcome = adapter.handle_text(
        USER_ID,
        1001,
        80,
        "У нас на это есть 240 тысяч в плане. Кто должен одобрить заявку?",
    )

    assert _result(storage, 80).status == "answered"
    assert "финансовый контролёр" in outcome.text
    assert USER_ID not in storage.pending_regulation


@pytest.mark.parametrize(
    ("question", "status", "answer_term", "source_id"),
    [
        (
            "До выставки осталось пять дней. Что писать в заявке?",
            "answered",
            "5 дней",
            "kb-006",
        ),
        (
            "Заявка сейчас у согласующих. Что это значит?",
            "answered",
            "требуется решение",
            "kb-007",
        ),
        (
            "Потребность отпала, но заявку уже передали закупщику. "
            "Её ещё можно убрать?",
            "answered",
            "Принята в работу",
            "kb-001",
        ),
        (
            "осоветуйте надёжную транспортную компанию",
            "insufficient_context",
            "не могу рекомендовать",
            None,
        ),
        (
            "Что мне делать с этой заявкой?",
            "clarification_required",
            "текущий статус заявки",
            None,
        ),
    ],
)
def test_manual_single_turn_smoke_through_telegram_adapter(
    qa_service: RegulationQuestionAnsweringService,
    question: str,
    status: str,
    answer_term: str,
    source_id: str | None,
) -> None:
    storage = InMemoryDialogModeStorage()
    adapter = _adapter(storage, qa_service)
    adapter.handle_menu(USER_ID, MENU_REGULATIONS)

    outcome = adapter.handle_text(USER_ID, 1001, 90, question)
    result = _result(storage, 90)

    assert result.status == status
    assert answer_term.casefold() in outcome.text.casefold()
    assert [source.document_id for source in result.sources] == (
        [source_id] if source_id else []
    )
