from dataclasses import dataclass, field
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID

from app.rag.answering import RegulationAnswer
from supabase import Client

DialogMode = Literal["idle", "intake", "regulation_qa"]


class DialogModePersistenceError(RuntimeError):
    pass


class DialogReplayConflictError(DialogModePersistenceError):
    pass


class DialogModeRepository(Protocol):
    def get_mode(self, user_id: UUID) -> DialogMode: ...

    def set_mode(self, user_id: UUID, mode: DialogMode) -> None: ...

    def find_regulation_replay(
        self,
        user_id: UUID,
        key: str,
        fingerprint: str,
    ) -> RegulationAnswer | None: ...

    def save_regulation_replay(
        self,
        user_id: UUID,
        key: str,
        fingerprint: str,
        result: RegulationAnswer,
    ) -> None: ...


@dataclass
class InMemoryDialogModeStorage:
    modes: dict[UUID, DialogMode] = field(default_factory=dict)
    replays: dict[tuple[UUID, str], tuple[str, RegulationAnswer]] = field(
        default_factory=dict
    )
    lock: RLock = field(default_factory=RLock)


class InMemoryDialogModeRepository:
    def __init__(self, storage: InMemoryDialogModeStorage | None = None) -> None:
        self.storage = storage or InMemoryDialogModeStorage()

    def get_mode(self, user_id: UUID) -> DialogMode:
        with self.storage.lock:
            return self.storage.modes.get(user_id, "idle")

    def set_mode(self, user_id: UUID, mode: DialogMode) -> None:
        with self.storage.lock:
            self.storage.modes[user_id] = mode

    def find_regulation_replay(
        self,
        user_id: UUID,
        key: str,
        fingerprint: str,
    ) -> RegulationAnswer | None:
        with self.storage.lock:
            stored = self.storage.replays.get((user_id, key))
            if stored is None:
                return None
            stored_fingerprint, result = stored
            if stored_fingerprint != fingerprint:
                raise DialogReplayConflictError(
                    "Message id was already used for another regulation question"
                )
            return result.model_copy(deep=True)

    def save_regulation_replay(
        self,
        user_id: UUID,
        key: str,
        fingerprint: str,
        result: RegulationAnswer,
    ) -> None:
        with self.storage.lock:
            existing = self.storage.replays.get((user_id, key))
            if existing is not None and existing[0] != fingerprint:
                raise DialogReplayConflictError(
                    "Message id was already used for another regulation question"
                )
            self.storage.replays[(user_id, key)] = (
                fingerprint,
                result.model_copy(deep=True),
            )


class SupabaseDialogModeRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get_mode(self, user_id: UUID) -> DialogMode:
        try:
            response = (
                self._client.table("dialog_states")
                .select("current_intent,active_request_id")
                .eq("user_id", str(user_id))
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise DialogModePersistenceError(
                "Failed to read Telegram dialog mode"
            ) from exc
        if not response.data:
            return "idle"
        row = response.data[0]
        mode = row.get("current_intent")
        if mode in {"idle", "intake", "regulation_qa"}:
            return mode
        return "intake" if row.get("active_request_id") else "idle"

    def set_mode(self, user_id: UUID, mode: DialogMode) -> None:
        try:
            (
                self._client.table("dialog_states")
                .upsert(
                    {"user_id": str(user_id), "current_intent": mode},
                    on_conflict="user_id",
                    default_to_null=False,
                )
                .execute()
            )
        except Exception as exc:
            raise DialogModePersistenceError(
                "Failed to save Telegram dialog mode"
            ) from exc

    def find_regulation_replay(
        self,
        user_id: UUID,
        key: str,
        fingerprint: str,
    ) -> RegulationAnswer | None:
        try:
            response = (
                self._client.table("message_logs")
                .select("idempotency_fingerprint,idempotency_result")
                .eq("user_id", str(user_id))
                .eq("direction", "incoming")
                .eq("idempotency_key", key)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise DialogModePersistenceError(
                "Failed to read regulation message replay"
            ) from exc
        if not response.data:
            return None
        row = response.data[0]
        if row.get("idempotency_fingerprint") != fingerprint:
            raise DialogReplayConflictError(
                "Message id was already used for another regulation question"
            )
        payload = row.get("idempotency_result")
        return RegulationAnswer.model_validate(payload) if payload else None

    def save_regulation_replay(
        self,
        user_id: UUID,
        key: str,
        fingerprint: str,
        result: RegulationAnswer,
    ) -> None:
        payload = {
            "user_id": str(user_id),
            "user_message": "[regulation question]",
            "assistant_message": None,
            "intent": "regulation_qa",
            "direction": "incoming",
            "message_type": "regulation_question",
            "idempotency_key": key,
            "idempotency_fingerprint": fingerprint,
            "idempotency_result": result.model_dump(mode="json"),
            "payload": {
                "status": result.status,
                "source_count": len(result.sources),
            },
            "metadata": {"contains_user_text": False, "contains_secrets": False},
        }
        try:
            self._client.table("message_logs").insert(payload).execute()
        except Exception as exc:
            replay = self.find_regulation_replay(user_id, key, fingerprint)
            if replay is not None:
                return
            raise DialogModePersistenceError(
                "Failed to save regulation message replay"
            ) from exc
