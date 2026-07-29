from dataclasses import dataclass
from uuid import UUID

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

MENU_NEW = "📝 Новая заявка"
MENU_CURRENT = "📋 Текущая заявка"
MENU_EXAMPLES = "💡 Примеры заявок"
MENU_HELP = "ℹ️ Помощь"
MENU_COMMANDS = {MENU_NEW, MENU_CURRENT, MENU_EXAMPLES, MENU_HELP}

_PREFIX = "rq"


@dataclass(frozen=True)
class RequestCallback:
    action: str
    request_id: UUID
    version: int


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_NEW), KeyboardButton(text=MENU_CURRENT)],
            [KeyboardButton(text=MENU_EXAMPLES), KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def ready_actions(request_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _inline(
        ("✅ Подтвердить и отправить", "confirm"),
        ("✏️ Изменить", "edit"),
        ("❌ Отменить", "cancel_ask"),
        request_id=request_id,
        version=version,
    )


def unresolved_actions(request_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _inline(
        ("💰 Уточнить бюджет", "budget"),
        ("✏️ Изменить заявку", "edit"),
        ("❌ Отменить", "cancel_ask"),
        request_id=request_id,
        version=version,
    )


def active_draft_actions(request_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _inline(
        ("Продолжить текущую", "current"),
        ("Отменить и начать новую", "cancel_new_ask"),
        ("Вернуться в меню", "menu"),
        request_id=request_id,
        version=version,
    )


def cancel_confirmation(
    request_id: UUID,
    version: int,
    *,
    start_new: bool = False,
) -> InlineKeyboardMarkup:
    action = "cancel_new_yes" if start_new else "cancel_yes"
    return _inline(
        ("Да, отменить", action),
        ("Нет, вернуться", "current"),
        request_id=request_id,
        version=version,
    )


def budget_choices(request_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _inline(
        ("Да, предусмотрена", "budget_yes"),
        ("Нет, не предусмотрена", "budget_no"),
        ("Не знаю", "budget_unknown"),
        request_id=request_id,
        version=version,
    )


def conflict_actions(request_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _inline(
        ("Подтвердить изменение", "conflict_accept"),
        ("Оставить прежнее значение", "conflict_keep"),
        request_id=request_id,
        version=version,
    )


def new_request_action(request_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _inline(
        ("📝 Создать новую заявку", "new"),
        request_id=request_id,
        version=version,
    )


def encode_callback(action: str, request_id: UUID, version: int) -> str:
    value = f"{_PREFIX}:{action}:{request_id.hex}:{version}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("Telegram callback_data exceeds 64 bytes")
    return value


def parse_callback(value: str | None) -> RequestCallback:
    if not value:
        raise ValueError("Missing callback data")
    prefix, action, raw_id, raw_version = value.split(":", maxsplit=3)
    if prefix != _PREFIX or not action:
        raise ValueError("Unsupported callback data")
    version = int(raw_version)
    if version < 1:
        raise ValueError("Invalid callback version")
    return RequestCallback(action, UUID(hex=raw_id), version)


def _inline(
    *buttons: tuple[str, str],
    request_id: UUID,
    version: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=encode_callback(action, request_id, version),
                )
            ]
            for label, action in buttons
        ]
    )
