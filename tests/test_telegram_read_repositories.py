from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from app.bot.dialog_modes import SupabaseDialogModeRepository
from app.bot.request_history import SupabaseRequestHistoryRepository
from app.rag.answering import RegulationAnswer

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeQuery:
    def __init__(self, data=None) -> None:
        self.data = list(data or [])
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return method

    def execute(self):
        self.calls.append(("execute", (), {}))
        return SimpleNamespace(data=self.data)


class FakeClient:
    def __init__(self, **tables) -> None:
        self.tables = tables

    def table(self, name: str):
        return self.tables[name]


def _request_row(user_id: UUID = USER_ID) -> dict:
    now = datetime(2026, 7, 30, tzinfo=UTC).isoformat()
    return {
        "id": str(REQUEST_ID),
        "request_number": "PR-2026-000015",
        "user_id": str(user_id),
        "request_type": "product",
        "category_code": "G02",
        "title": "Кресла",
        "status": "new",
        "data": {},
        "created_at": now,
        "updated_at": now,
        "registered_at": now,
        "version": 2,
    }


def test_dialog_mode_legacy_state_derives_intake_from_active_request() -> None:
    query = FakeQuery([{"current_intent": None, "active_request_id": str(REQUEST_ID)}])
    repository = SupabaseDialogModeRepository(FakeClient(dialog_states=query))
    assert repository.get_mode(USER_ID) == "intake"


def test_dialog_mode_upsert_changes_only_intent_and_preserves_state_columns() -> None:
    query = FakeQuery()
    repository = SupabaseDialogModeRepository(FakeClient(dialog_states=query))
    repository.set_mode(USER_ID, "regulation_qa")
    upsert = next(call for call in query.calls if call[0] == "upsert")
    assert upsert[1][0] == {
        "user_id": str(USER_ID),
        "current_intent": "regulation_qa",
    }
    assert upsert[2]["default_to_null"] is False
    assert "state_data" not in upsert[1][0]
    assert "active_request_id" not in upsert[1][0]


def test_regulation_replay_log_contains_no_question_or_telegram_id() -> None:
    query = FakeQuery()
    repository = SupabaseDialogModeRepository(FakeClient(message_logs=query))
    result = RegulationAnswer(answer="Ответ", status="answered")
    repository.save_regulation_replay(USER_ID, "safe-key", "fingerprint", result)
    insert = next(call for call in query.calls if call[0] == "insert")[1][0]
    assert insert["user_message"] == "[regulation question]"
    assert insert["assistant_message"] is None
    assert "telegram_id" not in insert
    assert insert["idempotency_result"]["answer"] == "Ответ"


def test_history_repository_filters_by_owner_and_non_draft_status() -> None:
    query = FakeQuery([_request_row()])
    repository = SupabaseRequestHistoryRepository(FakeClient(requests=query))
    request = repository.get_owned(REQUEST_ID, USER_ID)
    assert request is not None
    eq_calls = [call[1] for call in query.calls if call[0] == "eq"]
    assert ("id", str(REQUEST_ID)) in eq_calls
    assert ("user_id", str(USER_ID)) in eq_calls
    assert any(
        call[0] == "neq" and call[1] == ("status", "draft") for call in query.calls
    )


def test_history_list_is_limited_and_ordered_server_side() -> None:
    query = FakeQuery([_request_row()])
    repository = SupabaseRequestHistoryRepository(FakeClient(requests=query))
    results = repository.list_for_user(USER_ID, 5)
    assert len(results) == 1
    assert any(call[0] == "limit" and call[1] == (5,) for call in query.calls)
    assert sum(call[0] == "order" for call in query.calls) == 2
