import asyncio
import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.keyboards import MENU_COMMANDS, main_menu
from app.bot.parser import TelegramParseError
from app.bot.users import TelegramUserProfile, TelegramUserResolver

logger = logging.getLogger(__name__)
ERROR_TEXT = "Не удалось обработать сообщение. Попробуйте ещё раз."


@dataclass(frozen=True)
class TelegramHandlerDependencies:
    user_resolver: TelegramUserResolver
    intake_adapter: TelegramIntakeAdapter


async def handle_start(
    message: Message,
    dependencies: TelegramHandlerDependencies,
) -> None:
    try:
        user = await asyncio.to_thread(
            dependencies.user_resolver.resolve_user,
            _profile(message),
        )
        response = await asyncio.to_thread(
            dependencies.intake_adapter.start_message,
            user.user_id,
        )
    except Exception:
        logger.exception("Failed to handle Telegram /start")
        response = ERROR_TEXT
    await message.answer(response, reply_markup=main_menu())


async def handle_text_message(
    message: Message,
    dependencies: TelegramHandlerDependencies,
) -> None:
    try:
        user = await asyncio.to_thread(
            dependencies.user_resolver.resolve_user,
            _profile(message),
        )
        if message.text in MENU_COMMANDS:
            outcome = await asyncio.to_thread(
                dependencies.intake_adapter.handle_menu,
                user,
                message.text,
            )
        else:
            outcome = await asyncio.to_thread(
                dependencies.intake_adapter.handle_text,
                user,
                message.chat.id,
                message.message_id,
                message.text or "",
            )
        response = outcome.text
        reply_markup = outcome.reply_markup or main_menu()
        if outcome.replayed:
            logger.info(
                "Telegram duplicate response suppressed message_id=%s handler=text",
                message.message_id,
            )
            return
    except TelegramParseError as exc:
        response = str(exc)
        reply_markup = main_menu()
    except Exception:
        logger.exception("Failed to handle Telegram text message")
        response = ERROR_TEXT
        reply_markup = main_menu()
    await message.answer(response, reply_markup=reply_markup)


async def handle_callback_query(
    callback: CallbackQuery,
    dependencies: TelegramHandlerDependencies,
) -> None:
    try:
        user = await asyncio.to_thread(
            dependencies.user_resolver.resolve_user,
            _profile_from_callback(callback),
        )
        outcome = await asyncio.to_thread(
            dependencies.intake_adapter.handle_callback,
            user,
            callback.id,
            callback.data,
        )
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer(
                outcome.text,
                reply_markup=outcome.reply_markup or main_menu(),
            )
        else:
            await callback.bot.send_message(
                callback.from_user.id,
                outcome.text,
                reply_markup=outcome.reply_markup or main_menu(),
            )
    except Exception:
        logger.exception("Failed to handle Telegram callback")
        try:
            await callback.bot.send_message(
                callback.from_user.id,
                "Не удалось выполнить действие. Попробуйте ещё раз немного позже.",
                reply_markup=main_menu(),
            )
        except TelegramBadRequest:
            pass
    finally:
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass


def create_router(dependencies: TelegramHandlerDependencies) -> Router:
    router = Router(name="procurement_intake_telegram")

    async def start(message: Message) -> None:
        await handle_start(message, dependencies)

    async def text_message(message: Message) -> None:
        await handle_text_message(message, dependencies)

    async def lifecycle_callback(callback: CallbackQuery) -> None:
        await handle_callback_query(callback, dependencies)

    router.message.register(start, CommandStart())
    router.message.register(text_message, F.text, ~F.text.startswith("/"))
    router.callback_query.register(lifecycle_callback, F.data.startswith("rq:"))
    return router


def _profile(message: Message) -> TelegramUserProfile:
    if message.from_user is None:
        raise ValueError("Telegram message has no user")
    return TelegramUserProfile(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )


def _profile_from_callback(callback: CallbackQuery) -> TelegramUserProfile:
    user = callback.from_user
    return TelegramUserProfile(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
