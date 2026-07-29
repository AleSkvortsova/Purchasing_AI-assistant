from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.schemas.user import UserCreate, UserRead
from supabase import Client


class TelegramUserRepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramUserProfile:
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


@dataclass(frozen=True)
class ResolvedTelegramUser:
    user_id: UUID
    full_name: str
    department: str | None = None


class TelegramUserRepository(Protocol):
    def get_by_telegram_id(self, telegram_id: int) -> UserRead | None: ...

    def create(self, user: UserCreate) -> UserRead: ...


class SupabaseTelegramUserRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get_by_telegram_id(self, telegram_id: int) -> UserRead | None:
        try:
            response = (
                self._client.table("users")
                .select("*")
                .eq("telegram_id", telegram_id)
                .limit(1)
                .execute()
            )
            return self._parse_optional(response.data)
        except Exception as exc:
            raise TelegramUserRepositoryError(
                "Failed to resolve Telegram user"
            ) from exc

    def create(self, user: UserCreate) -> UserRead:
        try:
            response = (
                self._client.table("users")
                .insert(user.model_dump(mode="json"))
                .execute()
            )
            created = self._parse_optional(response.data)
            if created is None:
                raise TelegramUserRepositoryError(
                    "Supabase returned no created Telegram user"
                )
            return created
        except TelegramUserRepositoryError:
            raise
        except Exception as exc:
            raise TelegramUserRepositoryError(
                "Failed to create Telegram user"
            ) from exc

    @staticmethod
    def _parse_optional(data: list[dict[str, Any]]) -> UserRead | None:
        return UserRead.model_validate(data[0]) if data else None


class TelegramUserResolver:
    def __init__(self, repository: TelegramUserRepository) -> None:
        self._repository = repository

    def resolve(self, profile: TelegramUserProfile) -> UUID:
        return self.resolve_user(profile).user_id

    def resolve_user(self, profile: TelegramUserProfile) -> ResolvedTelegramUser:
        existing = self._repository.get_by_telegram_id(profile.telegram_id)
        if existing is not None:
            return _resolved(existing)
        try:
            created = self._repository.create(
                UserCreate(
                    telegram_id=profile.telegram_id,
                    full_name=_display_name(profile),
                )
            )
            return _resolved(created)
        except TelegramUserRepositoryError:
            # The unique telegram_id constraint makes a concurrent create safe.
            concurrent = self._repository.get_by_telegram_id(profile.telegram_id)
            if concurrent is not None:
                return _resolved(concurrent)
            raise


def _display_name(profile: TelegramUserProfile) -> str:
    full_name = " ".join(
        part.strip() for part in (profile.first_name, profile.last_name) if part
    )
    if full_name:
        return full_name
    if profile.username:
        return f"@{profile.username.lstrip('@')}"
    return "Пользователь Telegram"


def _resolved(user: UserRead) -> ResolvedTelegramUser:
    return ResolvedTelegramUser(
        user_id=user.id,
        full_name=user.full_name,
        department=user.department,
    )
