import asyncio

from aiogram import Bot, Dispatcher
from aiogram.utils.token import TokenValidationError, validate_token

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.categories import DeterministicCategoryClassifier
from app.bot.handlers import TelegramHandlerDependencies, create_router
from app.bot.normalization import NaturalDateParser
from app.bot.parser import DeterministicIntakeParser
from app.bot.users import SupabaseTelegramUserRepository, TelegramUserResolver
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.extraction.intake import TelegramIntakeExtractionService
from app.extraction.provider import OpenAIApprovalExtractionProvider
from app.intake.service import RequestIntakeService
from app.intake_persistence.repositories import SupabaseIntakePersistenceRepository
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.request_lifecycle.repositories import SupabaseRequestLifecycleRepository
from app.request_lifecycle.service import RequestLifecycleService
from app.rules.repository import SupabaseApprovalRuleRepository
from app.rules.service import ApprovalRuleService
from supabase import create_client

logger = get_logger(__name__)


class TelegramBotConfigurationError(RuntimeError):
    pass


def build_dependencies(
    settings: Settings,
    *,
    date_parser: NaturalDateParser | None = None,
) -> TelegramHandlerDependencies:
    if not settings.supabase_configured:
        raise TelegramBotConfigurationError(
            "Supabase configuration is required to run the Telegram bot"
        )
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    client = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    user_resolver = TelegramUserResolver(SupabaseTelegramUserRepository(client))
    rule_service = ApprovalRuleService(SupabaseApprovalRuleRepository(client))
    intake_service = RequestIntakeService(rule_service)
    intake = PersistentIntakeOrchestrator(
        SupabaseIntakePersistenceRepository(client), intake_service
    )
    lifecycle = RequestLifecycleService(
        SupabaseRequestLifecycleRepository(client), intake_service
    )
    categories = DeterministicCategoryClassifier()
    dates = date_parser or NaturalDateParser(settings.app_timezone)
    parser = DeterministicIntakeParser(
        category_classifier=categories,
        date_parser=dates,
    )
    extraction_mode = settings.resolved_telegram_extraction_mode
    structured_extractor = None
    if extraction_mode in {"openai", "hybrid"}:
        structured_extractor = TelegramIntakeExtractionService(
            OpenAIApprovalExtractionProvider(
                api_key=settings.openai_api_key,
                model=settings.approval_extraction_model,
                timeout_seconds=settings.approval_extraction_timeout_seconds,
                max_retries=settings.approval_extraction_max_retries,
            ),
            date_parser=dates,
            min_confidence=settings.approval_extraction_min_confidence,
        )
    logger.info("Telegram extraction mode selected: %s", extraction_mode)
    return TelegramHandlerDependencies(
        user_resolver=user_resolver,
        intake_adapter=TelegramIntakeAdapter(
            intake,
            parser,
            categories,
            lifecycle,
            structured_extractor,
            extraction_mode,
            settings.telegram_extraction_debug,
        ),
    )


async def main(settings: Settings | None = None) -> None:
    current = settings or get_settings()
    if not current.telegram_bot_token:
        raise TelegramBotConfigurationError(
            "TELEGRAM_BOT_TOKEN is required to run the Telegram bot"
        )
    try:
        validate_token(current.telegram_bot_token)
    except TokenValidationError as exc:
        raise TelegramBotConfigurationError(
            "TELEGRAM_BOT_TOKEN has an invalid format"
        ) from exc
    dependencies = build_dependencies(current)
    bot = Bot(token=current.telegram_bot_token)
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(create_router(dependencies))
    try:
        await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        await bot.session.close()


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(main(settings))
    except TelegramBotConfigurationError as exc:
        logger.error("Telegram bot configuration error: %s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    run()
