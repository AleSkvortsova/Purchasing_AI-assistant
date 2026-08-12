import asyncio

from aiogram import Bot, Dispatcher
from aiogram.utils.token import TokenValidationError, validate_token
from openai import OpenAI

from app.bot.adapter import TelegramIntakeAdapter
from app.bot.categories import DeterministicCategoryClassifier
from app.bot.category_resolution import (
    CategoryResolutionService,
    OpenAICategoryClassificationProvider,
)
from app.bot.dialog_modes import SupabaseDialogModeRepository
from app.bot.handlers import TelegramHandlerDependencies, create_router
from app.bot.normalization import NaturalDateParser
from app.bot.parser import DeterministicIntakeParser
from app.bot.request_history import (
    RequestHistoryService,
    SupabaseRequestHistoryRepository,
)
from app.bot.users import SupabaseTelegramUserRepository, TelegramUserResolver
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.extraction.intake import TelegramIntakeExtractionService
from app.extraction.provider import OpenAIApprovalExtractionProvider
from app.intake.service import RequestIntakeService
from app.intake_persistence.repositories import SupabaseIntakePersistenceRepository
from app.intake_persistence.service import PersistentIntakeOrchestrator
from app.rag.answering import (
    OpenAIGroundedAnswerProvider,
    RegulationQuestionAnsweringService,
)
from app.rag.embeddings import OpenAIEmbeddingProvider
from app.rag.repository import SupabaseKnowledgeRepository
from app.rag.retrieval_service import KnowledgeRetrievalService
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
    category_provider = None
    regulation_qa = None
    openai_client = (
        OpenAI(
            api_key=settings.openai_api_key,
            timeout=max(
                settings.approval_extraction_timeout_seconds,
                settings.rag_answer_timeout_seconds,
            ),
            max_retries=0,
        )
        if settings.openai_configured
        else None
    )
    if extraction_mode in {"openai", "hybrid"}:
        assert openai_client is not None
        structured_extractor = TelegramIntakeExtractionService(
            OpenAIApprovalExtractionProvider(
                api_key=settings.openai_api_key,
                model=settings.approval_extraction_model,
                timeout_seconds=settings.approval_extraction_timeout_seconds,
                max_retries=settings.approval_extraction_max_retries,
                client=openai_client,
            ),
            date_parser=dates,
            min_confidence=settings.approval_extraction_min_confidence,
        )
        category_provider = OpenAICategoryClassificationProvider(
            model=settings.approval_extraction_model,
            timeout_seconds=settings.approval_extraction_timeout_seconds,
            client=openai_client,
        )
    if openai_client is not None:
        retrieval = KnowledgeRetrievalService(
            SupabaseKnowledgeRepository(client),
            OpenAIEmbeddingProvider(
                api_key=settings.openai_api_key,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                batch_size=settings.embedding_batch_size,
                client=openai_client,
            ),
            default_top_k=settings.rag_top_k,
            default_threshold=settings.rag_similarity_threshold,
            default_mode=settings.rag_retrieval_mode,
            default_semantic_candidate_count=(settings.rag_semantic_candidate_count),
            default_lexical_candidate_count=settings.rag_lexical_candidate_count,
            default_rrf_k=settings.rag_rrf_k,
            default_semantic_weight=settings.rag_semantic_weight,
            default_lexical_weight=settings.rag_lexical_weight,
        )
        regulation_qa = RegulationQuestionAnsweringService(
            retrieval,
            OpenAIGroundedAnswerProvider(
                api_key=settings.openai_api_key,
                model=settings.rag_answer_model,
                timeout_seconds=settings.rag_answer_timeout_seconds,
                client=openai_client,
            ),
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
            SupabaseDialogModeRepository(client),
            RequestHistoryService(SupabaseRequestHistoryRepository(client)),
            regulation_qa,
            category_resolver=CategoryResolutionService(
                categories,
                category_provider,
            ),
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
