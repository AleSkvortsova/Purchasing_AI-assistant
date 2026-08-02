from dataclasses import dataclass, field
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID

from app.rag.answering import RegulationAnswer
from app.rag.conversation import RegulationPendingClarification
from supabase import Client

DialogMode = Literal["idle", "intake", "regulation_qa"]
_PENDING_KEY = "regulation_pending_clarification"


class DialogModePersistenceError(RuntimeError):
    pass


class DialogReplayConflictError(DialogModePersistenceError):
    pass


class DialogModeRepository(Protocol):
    def get_mode(self, user_id: UUID) -> DialogMode: ...

    def set_mode(self, user_id: UUID, mode: DialogMode) -> None: ...

    def get_pending_regulation(
        self,
        user_id: UUID,
    ) -> RegulationPendingClarification | None: ...

    def save_pending_regulation(
        self,
        user_id: UUID,
        pending: RegulationPendingClarification,
    ) -> None: ...

    def clear_pending_regulation(self, user_id: UUID) -> None: ...

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
    pending_regulation: dict[UUID, RegulationPendingClarification] = field(
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
            if mode != "regulation_qa":
                self.storage.pending_regulation.pop(user_id, None)

    def get_pending_regulation(
        self,
        user_id: UUID,
    ) -> RegulationPendingClarification | None:
        with self.storage.lock:
            pending = self.storage.pending_regulation.get(user_id)
            return pending.model_copy(deep=True) if pending is not None else None

    def save_pending_regulation(
        self,
        user_id: UUID,
        pending: RegulationPendingClarification,
    ) -> None:
        with self.storage.lock:
            self.storage.pending_regulation[user_id] = pending.model_copy(deep=True)

    def clear_pending_regulation(self, user_id: UUID) -> None:
        with self.storage.lock:
            self.storage.pending_regulation.pop(user_id, None)

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
        payload: dict[str, object] = {
            "user_id": str(user_id),
            "current_intent": mode,
        }
        if mode != "regulation_qa":
            state_data = self._read_state_data(user_id)
            if _PENDING_KEY in state_data:
                state_data.pop(_PENDING_KEY, None)
                payload["state_data"] = state_data
        try:
            (
                self._client.table("dialog_states")
                .upsert(
                    payload,
                    on_conflict="user_id",
                    default_to_null=False,
                )
                .execute()
            )
        except Exception as exc:
            raise DialogModePersistenceError(
                "Failed to save Telegram dialog mode"
            ) from exc

    def get_pending_regulation(
        self,
        user_id: UUID,
    ) -> RegulationPendingClarification | None:
        payload = self._read_state_data(user_id).get(_PENDING_KEY)
        if payload is None:
            return None
        try:
            return RegulationPendingClarification.model_validate(payload)
        except Exception as exc:
            raise DialogModePersistenceError(
                "Stored regulation clarification state is invalid"
            ) from exc

    def save_pending_regulation(
        self,
        user_id: UUID,
        pending: RegulationPendingClarification,
    ) -> None:
        state_data = self._read_state_data(user_id)
        state_data[_PENDING_KEY] = pending.model_dump(mode="json")
        self._write_state_data(user_id, state_data)

    def clear_pending_regulation(self, user_id: UUID) -> None:
        state_data = self._read_state_data(user_id)
        if _PENDING_KEY not in state_data:
            return
        state_data.pop(_PENDING_KEY, None)
        self._write_state_data(user_id, state_data)

    def _read_state_data(self, user_id: UUID) -> dict:
        try:
            response = (
                self._client.table("dialog_states")
                .select("state_data")
                .eq("user_id", str(user_id))
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise DialogModePersistenceError(
                "Failed to read regulation clarification state"
            ) from exc
        if not response.data:
            return {}
        value = response.data[0].get("state_data")
        return dict(value) if isinstance(value, dict) else {}

    def _write_state_data(self, user_id: UUID, state_data: dict) -> None:
        try:
            (
                self._client.table("dialog_states")
                .upsert(
                    {
                        "user_id": str(user_id),
                        "current_intent": "regulation_qa",
                        "state_data": state_data,
                    },
                    on_conflict="user_id",
                    default_to_null=False,
                )
                .execute()
            )
        except Exception as exc:
            raise DialogModePersistenceError(
                "Failed to save regulation clarification state"
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
