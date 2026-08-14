import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import app.bot.__main__ as bot_main
from app.bot.category_resolution import (
    CategoryClassificationPayload,
    CategoryClassificationRequest,
    FakeCategoryClassificationProvider,
    category_confirmation_evidence,
    category_draft_context_fingerprint,
)
from app.bot.dialog_modes import InMemoryDialogModeRepository
from app.bot.formatters import format_request_card
from app.bot.handlers import handle_text_message
from app.bot.keyboards import MENU_REGULATIONS
from app.bot.normalization import NaturalDateParser
from app.bot.parser import DeterministicIntakeParser
from app.core.config import Settings
from app.extraction.intake import TelegramIntakeExtractionService
from app.extraction.models import RawApprovalExtraction
from app.extraction.provider import FakeApprovalExtractionProvider
from app.intake.card import RequestCardBuilder
from app.intake.models import RequestDraftData
from app.intake.service import RequestIntakeService
from app.intake_persistence.repositories import (
    InMemoryIntakePersistenceRepository,
)
from app.rag.answering import (
    FakeGroundedAnswerProvider,
    GroundedAnswerPayload,
    GroundedClaim,
)
from app.rag.models import HybridRetrievalResult
from app.schemas.user import UserRead

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
REFERENCE_DATE = date(2026, 7, 29)


class FakeMessage:
    def __init__(self, text: str, message_id: int) -> None:
        self.text = text
        self.message_id = message_id
        self.chat = SimpleNamespace(id=1001)
        self.from_user = SimpleNamespace(
            id=7001,
            username="test_user",
            first_name="Тест",
            last_name="Пользователь",
        )
        self.answers: list[str] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append(text)


class FakeUserRepository:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.user = UserRead(
            id=USER_ID,
            telegram_id=7001,
            full_name="Тест Пользователь",
            department=None,
            role="requester",
            is_active=True,
            created_at=now,
            updated_at=now,
        )

    def get_by_telegram_id(self, telegram_id: int) -> UserRead | None:
        return self.user if telegram_id == 7001 else None

    def create(self, user):
        return self.user


class SequenceProvider:
    def __init__(self, results: list[RawApprovalExtraction]) -> None:
        self.results = results
        self.calls = 0
        self.last_metadata = {"provider": "production-wiring-fake"}

    def extract(self, text: str) -> RawApprovalExtraction:
        result = self.results[self.calls]
        self.calls += 1
        return result.model_copy(deep=True)


class SequenceCategoryProvider:
    def __init__(self, results: list[CategoryClassificationPayload]) -> None:
        self.results = results
        self.calls = 0
        self.requests: list[CategoryClassificationRequest] = []

    def classify(
        self, request: CategoryClassificationRequest
    ) -> CategoryClassificationPayload:
        self.requests.append(request)
        result = self.results[self.calls]
        self.calls += 1
        return result.model_copy(deep=True)


def _build(
    monkeypatch,
    provider: SequenceProvider,
    category_provider: FakeCategoryClassificationProvider | None = None,
):
    repository = InMemoryIntakePersistenceRepository()
    monkeypatch.setattr(bot_main, "create_client", lambda *_: object())
    monkeypatch.setattr(
        bot_main,
        "SupabaseTelegramUserRepository",
        lambda _client: FakeUserRepository(),
    )
    monkeypatch.setattr(
        bot_main,
        "SupabaseIntakePersistenceRepository",
        lambda _client: repository,
    )
    monkeypatch.setattr(
        bot_main,
        "SupabaseApprovalRuleRepository",
        lambda _client: object(),
    )
    monkeypatch.setattr(bot_main, "ApprovalRuleService", lambda _repo: None)
    monkeypatch.setattr(
        bot_main,
        "SupabaseRequestLifecycleRepository",
        lambda _client: object(),
    )
    monkeypatch.setattr(
        bot_main,
        "RequestLifecycleService",
        lambda _repo, _intake: None,
    )
    monkeypatch.setattr(
        bot_main,
        "OpenAIApprovalExtractionProvider",
        lambda **_kwargs: provider,
    )
    current_category_provider = category_provider or (
        FakeCategoryClassificationProvider(
            CategoryClassificationPayload(
                decision="unresolved",
                primary_category_code=None,
                alternatives=[],
                confidence="low",
                evidence=None,
                rationale_code="insufficient_context",
            )
        )
    )
    monkeypatch.setattr(
        bot_main,
        "OpenAICategoryClassificationProvider",
        lambda **_kwargs: current_category_provider,
    )
    monkeypatch.setattr(
        bot_main,
        "SupabaseDialogModeRepository",
        lambda _client: InMemoryDialogModeRepository(),
    )
    settings = Settings(
        _env_file=None,
        supabase_url="https://example.invalid",
        supabase_service_role_key="test-only",
        openai_api_key="test-only",
        approval_extraction_model="test-model",
        telegram_extraction_mode="hybrid",
        telegram_extraction_debug=True,
    )
    dependencies = bot_main.build_dependencies(
        settings,
        date_parser=NaturalDateParser(today_provider=lambda: REFERENCE_DATE),
    )
    return dependencies, repository


def test_unknown_category_uses_shared_production_wiring_and_confirmation(
    monkeypatch,
) -> None:
    text = "Нужно купить 5 офисных стульев для переговорной"
    raw = _raw(
        procurement_type_raw="goods",
        item_name_raw="офисные стулья",
        quantity_raw="5",
        unit_raw="шт.",
        category_raw="G02",
        evidence_by_field={
            "procurement_type": "купить",
            "item_name": "офисных стульев",
            "quantity": "5 офисных стульев",
            "unit": "5 офисных стульев",
            "category": "офисных стульев",
        },
    )
    category_provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G02",
            alternatives=[],
            confidence="high",
            evidence="офисных стульев",
            rationale_code="taxonomy_match",
        )
    )
    dependencies, _ = _build(
        monkeypatch,
        SequenceProvider([raw]),
        category_provider,
    )
    initial = FakeMessage(text, 900)

    asyncio.run(handle_text_message(initial, dependencies))

    proposed = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    assert proposed.intake_result.draft.category_code is None
    assert "Похоже, подходит категория" in initial.answers[0]
    confirmed_message = FakeMessage("да", 901)
    asyncio.run(handle_text_message(confirmed_message, dependencies))
    confirmed = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    assert confirmed.intake_result.draft.category_code == "G02"
    assert confirmed.intake_result.draft.field_states[
        "category_code"
    ].evidence == (
        category_confirmation_evidence(
            "goods",
            confirmed.intake_result.draft.item_name or "",
            "G02",
            category_draft_context_fingerprint(confirmed.intake_result.draft),
        )
    )
    assert category_provider.calls == 1


def test_dock_station_confirmation_survives_production_wiring(monkeypatch) -> None:
    raw = _raw(
        procurement_type_raw="goods",
        item_name_raw="док-станция для ноутбука",
        evidence_by_field={
            "procurement_type": "нужна док-станция",
            "item_name": "док-станция для ноутбука",
        },
    )
    category_provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G04",
            alternatives=[],
            confidence="high",
            evidence="док-станция",
            rationale_code="taxonomy_match",
        )
    )
    dependencies, _ = _build(
        monkeypatch,
        SequenceProvider([raw]),
        category_provider,
    )

    initial = FakeMessage("нужна док-станция для ноутбука", 905)
    asyncio.run(handle_text_message(initial, dependencies))
    assert "IT-периферия (G04)" in initial.answers[0]

    confirmation = FakeMessage("да", 906)
    asyncio.run(handle_text_message(confirmation, dependencies))
    confirmed = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)

    assert confirmed.intake_result.draft.category_code == "G04"
    assert confirmed.intake_result.draft.field_states["category_code"].confirmed
    assert confirmed.intake_result.next_question is not None
    assert confirmed.intake_result.next_question.field_code != "category_code"
    assert category_provider.calls == 1


def test_router_type_reply_resolves_category_from_accumulated_subject(
    monkeypatch,
) -> None:
    provider = SequenceProvider(
        [
            _raw(
                item_name_raw="роутеры",
                quantity_raw="2",
                evidence_by_field={
                    "item_name": "роутера",
                    "quantity": "2 роутера",
                },
            ),
            _raw(
                procurement_type_raw="goods",
                evidence_by_field={"procurement_type": "товар"},
            ),
        ]
    )
    category_provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="exact",
            primary_category_code="G04",
            alternatives=[],
            confidence="high",
            evidence="роутеры",
            rationale_code="taxonomy_match",
        )
    )
    dependencies, _ = _build(monkeypatch, provider, category_provider)

    first = FakeMessage("нужны 2 роутера для установки на складе", 910)
    asyncio.run(handle_text_message(first, dependencies))
    initial = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    assert "роутер" in (initial.intake_result.draft.item_name or "")
    assert initial.intake_result.draft.procurement_type is None
    assert initial.intake_result.next_question.field_code == "procurement_type"

    second = FakeMessage("товар", 911)
    asyncio.run(handle_text_message(second, dependencies))
    resolved = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    request = category_provider.requests[0]
    assert resolved.intake_result.draft.procurement_type == "goods"
    assert "роутер" in (resolved.intake_result.draft.item_name or "")
    assert request.procurement_type == "goods"
    assert "роутер" in request.item_name
    assert "роутер" in request.source_text
    assert request.source_text != "товар"
    assert "Похоже, подходит категория" in second.answers[0]


def test_unresolved_category_retries_when_subject_context_becomes_richer(
    monkeypatch,
) -> None:
    extraction = SequenceProvider(
        [
            _raw(
                procurement_type_raw="goods",
                item_name_raw="промышленные вентиляторы",
                evidence_by_field={
                    "procurement_type": "купите",
                    "item_name": "промышленных вентилятора",
                },
            ),
            _raw(
                specifications_raw="для охлаждения помещения в производственном цеху",
                evidence_by_field={
                    "specifications": (
                        "для охлаждения помещения в производственном цеху"
                    )
                },
            ),
        ]
    )
    unresolved = CategoryClassificationPayload(
        decision="unresolved",
        primary_category_code=None,
        alternatives=[],
        confidence="low",
        evidence=None,
        rationale_code="insufficient_context",
    )
    exact = CategoryClassificationPayload(
        decision="exact",
        primary_category_code="G15",
        alternatives=[],
        confidence="high",
        evidence="для охлаждения помещения",
        rationale_code="taxonomy_match",
    )
    category_provider = SequenceCategoryProvider([unresolved, exact])
    dependencies, _ = _build(monkeypatch, extraction, category_provider)

    first = FakeMessage("купите 3 промышленных вентилятора в производственный цех", 920)
    asyncio.run(handle_text_message(first, dependencies))
    after_first = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    first_fingerprint = (
        after_first.dialog_state.intake_conversation.category_context_fingerprint
    )
    second = FakeMessage(
        "промышленные вентиляторы для охлаждения помещения в производственном цеху",
        921,
    )
    asyncio.run(handle_text_message(second, dependencies))
    after_second = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)

    assert category_provider.calls == 2
    assert first_fingerprint
    assert (
        after_second.dialog_state.intake_conversation.category_context_fingerprint
        != first_fingerprint
    )
    assert "для охлаждения помещения" in category_provider.requests[1].source_text
    assert "Похоже, подходит категория" in second.answers[0]


def test_scalar_replies_do_not_retry_unresolved_category_provider(monkeypatch) -> None:
    extraction = SequenceProvider(
        [
            _raw(
                procurement_type_raw="goods",
                item_name_raw="промышленные вентиляторы",
                evidence_by_field={
                    "procurement_type": "купите",
                    "item_name": "промышленных вентилятора",
                },
            )
        ]
    )
    category_provider = FakeCategoryClassificationProvider(
        CategoryClassificationPayload(
            decision="unresolved",
            primary_category_code=None,
            alternatives=[],
            confidence="low",
            evidence=None,
            rationale_code="insufficient_context",
        )
    )
    dependencies, _ = _build(monkeypatch, extraction, category_provider)
    asyncio.run(
        handle_text_message(
            FakeMessage("купите 3 промышленных вентилятора", 930), dependencies
        )
    )

    replies = ("80000р", "да, предусмотрена", "15 сентября")
    for message_id, text in enumerate(replies, 931):
        message = FakeMessage(text, message_id)
        asyncio.run(handle_text_message(message, dependencies))

    assert category_provider.calls == 1


def test_monitor_structured_goods_survives_merge_and_persistence(monkeypatch) -> None:
    text = "нужны 7 мониторов для установки в новом офисе"
    provider = SequenceProvider(
        [
            _raw(
                procurement_type_raw="goods",
                item_name_raw="мониторы",
                quantity_raw="7",
                evidence_by_field={
                    "procurement_type": "мониторов",
                    "item_name": "мониторов",
                    "quantity": "7 мониторов",
                },
            )
        ]
    )
    dependencies, repository = _build(monkeypatch, provider)
    message = FakeMessage(text, 940)

    asyncio.run(handle_text_message(message, dependencies))
    persisted = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)

    assert persisted.intake_result.draft.procurement_type == "goods"
    assert persisted.intake_result.draft.item_name == "мониторы"
    assert persisted.intake_result.next_question.field_code != "procurement_type"
    reloaded_dependencies, _ = _build(monkeypatch, SequenceProvider([]))
    reloaded_dependencies.intake_adapter._orchestrator.repository = repository
    reloaded = reloaded_dependencies.intake_adapter._orchestrator.get_active_session(
        USER_ID
    )
    assert reloaded.intake_result.draft.procurement_type == "goods"
    assert reloaded.intake_result.draft.item_name == "мониторы"


def _raw(**values) -> RawApprovalExtraction:
    confidence = {
        field_name.removesuffix("_raw"): 0.99
        for field_name, value in values.items()
        if field_name.endswith("_raw") and value is not None
    }
    evidence = values.pop("evidence_by_field", {})
    return RawApprovalExtraction(
        **values,
        confidence_by_field=confidence,
        evidence_by_field=evidence,
    )


def _application_result(
    text: str,
    raw: RawApprovalExtraction,
) -> RequestDraftData:
    dates = NaturalDateParser(today_provider=lambda: REFERENCE_DATE)
    deterministic = DeterministicIntakeParser(date_parser=dates).parse(text)
    resolution = TelegramIntakeExtractionService(
        FakeApprovalExtractionProvider(raw),
        date_parser=dates,
    ).resolve_message(
        text,
        RequestDraftData(),
        None,
        deterministic,
        source_kind="initial_description",
        merge_deterministic=True,
        fallback_on_error=True,
    )
    assert resolution.update is not None
    return (
        RequestIntakeService().process_step(RequestDraftData(), resolution.update).draft
    )


def _assert_canonical_parity(actual: RequestDraftData, expected: RequestDraftData):
    for field_name in (
        "procurement_type",
        "item_name",
        "specifications",
        "quantity",
        "unit",
        "desired_delivery_date",
        "amount",
        "budget_status",
        "delivery_location",
    ):
        assert getattr(actual, field_name) == getattr(expected, field_name)


def test_regulation_mode_uses_production_wiring_without_intake_or_extra_clients(
    monkeypatch,
) -> None:
    supabase_client = object()
    openai_client = object()
    intake_repository = InMemoryIntakePersistenceRepository()
    retrieval_calls: list[str] = []
    constructor_clients: list[object] = []
    chunk = HybridRetrievalResult(
        chunk_id=UUID("99999999-9999-4999-8999-999999999999"),
        document_id="kb-009",
        source_filename="09_Правила_согласования.md",
        document_title="Правила согласования заявок",
        document_type="approval_rules",
        section_path="Матрица согласования",
        content=(
            "Согласование определено матрицей.\n"
            "| Бюджетная закупка до 100 000 руб. | Руководитель |"
        ),
        priority=1,
        hybrid_score=0.03,
    )

    class Retrieval:
        default_top_k = 5
        default_rrf_k = 60

        def search(self, query):
            retrieval_calls.append(query)
            return [chunk]

    answer_provider = FakeGroundedAnswerProvider(
        GroundedAnswerPayload(
            answer="Согласование определено матрицей.",
            claims=[
                GroundedClaim(
                    text="Согласование определено матрицей.",
                    cited_chunk_ids=[str(chunk.chunk_id)],
                )
            ],
            insufficient_context=False,
            source_conflict=False,
        )
    )
    monkeypatch.setattr(bot_main, "create_client", lambda *_: supabase_client)
    monkeypatch.setattr(bot_main, "OpenAI", lambda **_kwargs: openai_client)
    monkeypatch.setattr(
        bot_main,
        "SupabaseTelegramUserRepository",
        lambda client: FakeUserRepository(),
    )
    monkeypatch.setattr(
        bot_main,
        "SupabaseIntakePersistenceRepository",
        lambda client: intake_repository,
    )
    monkeypatch.setattr(
        bot_main, "SupabaseApprovalRuleRepository", lambda client: object()
    )
    monkeypatch.setattr(bot_main, "ApprovalRuleService", lambda repository: None)
    monkeypatch.setattr(
        bot_main, "SupabaseRequestLifecycleRepository", lambda client: object()
    )
    monkeypatch.setattr(
        bot_main, "RequestLifecycleService", lambda repository, intake: None
    )
    monkeypatch.setattr(
        bot_main,
        "OpenAIApprovalExtractionProvider",
        lambda **_kwargs: SequenceProvider([]),
    )
    monkeypatch.setattr(
        bot_main,
        "SupabaseDialogModeRepository",
        lambda client: InMemoryDialogModeRepository(),
    )
    monkeypatch.setattr(
        bot_main, "SupabaseRequestHistoryRepository", lambda client: object()
    )
    monkeypatch.setattr(
        bot_main,
        "SupabaseKnowledgeRepository",
        lambda client: constructor_clients.append(client) or object(),
    )
    monkeypatch.setattr(
        bot_main,
        "OpenAIEmbeddingProvider",
        lambda **kwargs: constructor_clients.append(kwargs["client"]) or object(),
    )
    monkeypatch.setattr(
        bot_main, "KnowledgeRetrievalService", lambda *_args, **_kwargs: Retrieval()
    )
    monkeypatch.setattr(
        bot_main,
        "OpenAIGroundedAnswerProvider",
        lambda **kwargs: (
            constructor_clients.append(kwargs["client"]) or answer_provider
        ),
    )
    settings = Settings(
        _env_file=None,
        supabase_url="https://example.invalid",
        supabase_service_role_key="test-only",
        openai_api_key="test-only",
        approval_extraction_model="test-model",
        rag_answer_model="test-model",
        telegram_extraction_mode="hybrid",
    )
    dependencies = bot_main.build_dependencies(settings)

    asyncio.run(handle_text_message(FakeMessage(MENU_REGULATIONS, 900), dependencies))
    question = FakeMessage(
        "Кто согласует закупку на 100000 рублей, предусмотренную бюджетом?",
        901,
    )
    asyncio.run(handle_text_message(question, dependencies))

    assert len(retrieval_calls) == 3
    assert "матрица согласования" in retrieval_calls[1]
    assert answer_provider.calls == []
    assert intake_repository.storage.requests == {}
    assert "Источники:" in question.answers[0]
    assert constructor_clients == [supabase_client, openai_client, openai_client]


def test_goods_scenario_uses_production_wiring_and_persists_inferred_unit(
    monkeypatch,
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    text = (
        "Нужно купить 5 лампочек на случай замены перегоревших в офисе "
        "на Невском в срок через неделю"
    )
    raw = _raw(
        procurement_type_raw="goods",
        item_name_raw="лампочки",
        specifications_raw="для замены перегоревших",
        category_raw="G14",
        delivery_location_raw="офис на Невском",
        evidence_by_field={
            "procurement_type": "купить",
            "item_name": "лампочек",
            "specifications": "на случай замены перегоревших",
            "category": "лампочек",
            "delivery_location": "офисе на Невском",
        },
    )
    provider = SequenceProvider([raw])
    dependencies, _ = _build(monkeypatch, provider)
    initial = FakeMessage(text, 100)

    asyncio.run(handle_text_message(initial, dependencies))
    session = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    draft = session.intake_result.draft

    assert len(initial.answers) == 1
    assert draft.procurement_type == "goods"
    assert draft.quantity == Decimal("5")
    assert draft.unit == "шт."
    assert draft.desired_delivery_date == date(2026, 8, 5)
    assert draft.delivery_location == "офис на Невском"
    assert session.intake_result.next_question.field_code == "amount"
    _assert_canonical_parity(draft, _application_result(text, raw))

    amount = FakeMessage("500р", 101)
    asyncio.run(handle_text_message(amount, dependencies))
    persisted = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)

    assert len(amount.answers) == 1
    assert persisted.intake_result.draft.amount == Decimal("500")
    assert persisted.intake_result.next_question.field_code != "amount"


def test_service_volume_and_deadline_survive_production_persistence(
    monkeypatch,
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    text = (
        "Нужно организовать заправку четырех картриджей для офисных "
        "принтеров, офис на Гражданском, не позднее 10 августа. "
        "В бюджете учтено"
    )
    raw = _raw(
        procurement_type_raw="service",
        item_name_raw="заправка картриджей",
        specifications_raw="для офисных принтеров",
        category_raw="S15",
        budget_status_raw="budgeted",
        delivery_location_raw="офис на Гражданском",
        evidence_by_field={
            "procurement_type": "заправку",
            "item_name": "заправку четырех картриджей",
            "specifications": "для офисных принтеров",
            "category": "заправку",
            "budget_status": "В бюджете учтено",
            "delivery_location": "офис на Гражданском",
        },
    )
    provider = SequenceProvider([raw])
    dependencies, _ = _build(monkeypatch, provider)
    message = FakeMessage(text, 200)

    asyncio.run(handle_text_message(message, dependencies))
    persisted = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    draft = persisted.intake_result.draft

    assert len(message.answers) == 1
    assert draft.procurement_type == "service"
    assert draft.quantity is None
    assert draft.unit is None
    assert "четырех картриджей" in draft.specifications
    assert "офисных принтеров" in draft.specifications
    assert draft.desired_delivery_date == date(2026, 8, 10)
    assert draft.delivery_location == "офис на Гражданском"
    assert draft.budget_status == "budgeted"
    assert persisted.intake_result.next_question.field_code != "desired_delivery_date"
    _assert_canonical_parity(draft, _application_result(text, raw))


def test_full_location_and_one_response_per_message_in_production_wiring(
    monkeypatch,
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    text = (
        "Нужно помыть окна в переговорной на Гражданском до 3 августа, "
        "в бюджете, 5 тыс. руб."
    )
    initial_raw = _raw(
        procurement_type_raw="service",
        item_name_raw="мойка окон",
        category_raw="S02",
        budget_status_raw="budgeted",
        delivery_location_raw="в переговорной",
        evidence_by_field={
            "procurement_type": "помыть",
            "item_name": "помыть окна",
            "category": "помыть окна",
            "budget_status": "в бюджете",
            "delivery_location": "в переговорной",
        },
    )
    provider = SequenceProvider(
        [
            initial_raw,
            _raw(
                specifications_raw="мыть лучше после окончания рабочего дня",
                evidence_by_field={
                    "specifications": "мыть лучше после окончания рабочего дня",
                },
            ),
        ]
    )
    dependencies, _ = _build(monkeypatch, provider)
    initial = FakeMessage(text, 300)

    asyncio.run(handle_text_message(initial, dependencies))
    asyncio.run(handle_text_message(initial, dependencies))
    persisted = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)

    assert len(initial.answers) == 1
    assert persisted.intake_result.draft.delivery_location == (
        "в переговорной на Гражданском"
    )
    assert persisted.intake_result.draft.amount == Decimal("5000")
    assert persisted.intake_result.draft.desired_delivery_date == date(2026, 8, 3)
    _assert_canonical_parity(
        persisted.intake_result.draft,
        _application_result(text, initial_raw),
    )

    answer = FakeMessage(
        "чистые окна, мыть лучше после окончания рабочего дня",
        301,
    )
    asyncio.run(handle_text_message(answer, dependencies))
    updated = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)

    assert len(answer.answers) == 1
    assert provider.calls == 2
    assert updated.intake_result.draft.conflicts == []
    assert "после окончания рабочего дня" in (
        updated.intake_result.draft.specifications or ""
    )


def test_budgeted_service_requirements_use_production_wiring(
    monkeypatch,
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    text = (
        "Нужно заказать сервисное обслуживание на три принтера в офисе "
        "на Невском, забюджетировано, 10 тыс. р., до 12 августа"
    )
    provider = SequenceProvider(
        [
            _raw(
                procurement_type_raw="service",
                item_name_raw="сервисное обслуживание",
                specifications_raw="три принтера",
                category_raw="S01",
                delivery_location_raw="офис на Невском",
                evidence_by_field={
                    "procurement_type": "сервисное обслуживание",
                    "item_name": "сервисное обслуживание",
                    "specifications": "три принтера",
                    "category": "сервисное обслуживание",
                    "delivery_location": "офисе на Невском",
                },
            ),
            _raw(
                desired_result_raw="проведено регулярное",
                specifications_raw=("мастер должен работать в рамках рабочего дня"),
                evidence_by_field={
                    "desired_result": "проведено регулярное",
                    "specifications": ("мастер должен работать в рамках рабочего дня"),
                },
            ),
        ]
    )
    dependencies, _ = _build(monkeypatch, provider)
    initial = FakeMessage(text, 400)

    asyncio.run(handle_text_message(initial, dependencies))
    first = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)

    assert len(initial.answers) == 1
    assert first.intake_result.draft.procurement_type == "service"
    assert first.intake_result.draft.item_name == "сервисное обслуживание"
    assert "три принтера" in first.intake_result.draft.specifications
    assert first.intake_result.draft.amount == Decimal("10000")
    assert first.intake_result.draft.budget_status == "budgeted"
    assert first.intake_result.draft.desired_delivery_date == date(2026, 8, 12)
    assert first.intake_result.draft.delivery_location == "офис на Невском"
    assert first.intake_result.next_question.field_code != "budget_status"

    answer = FakeMessage(
        "проведено регулярное сервисное обслуживание, мастер должен "
        "работать в рамках рабочего дня",
        401,
    )
    asyncio.run(handle_text_message(answer, dependencies))
    persisted = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    card = RequestCardBuilder().build(persisted.intake_result.draft)
    fields = {
        field.code: field.display_value
        for section in card.sections
        for field in section.fields
    }

    assert len(answer.answers) == 1
    assert persisted.intake_result.draft.conflicts == []
    assert persisted.intake_result.next_question is None or (
        persisted.intake_result.next_question.field_code != "budget_status"
    )
    assert "Проведено регулярное" not in fields["specifications"]
    assert "три принтера" in fields["specifications"].casefold()
    assert "рабочего дня" in fields["specifications"].casefold()
    assert fields["specifications"].count("три принтера") == 1
    assert "desired_result" not in fields


def test_countable_goods_infers_piece_unit_through_production_wiring(
    monkeypatch,
    freeze_intake_today,
) -> None:
    freeze_intake_today(REFERENCE_DATE)
    text = (
        "закажите семь тарелок на кухню в офис на Гражданском, "
        "до 4000 руб, не забюджетировано, к 3 августа"
    )
    dates = NaturalDateParser(today_provider=lambda: REFERENCE_DATE)
    deterministic = DeterministicIntakeParser(date_parser=dates).parse(text)
    raw = _raw(
        procurement_type_raw="goods",
        item_name_raw="тарелки",
        quantity_raw="семь",
        unit_raw=None,
        category_raw="G01",
        delivery_location_raw="в офис на Гражданском",
        evidence_by_field={
            "procurement_type": "тарелок",
            "item_name": "тарелок",
            "quantity": "семь тарелок",
            "category": "тарелок",
            "delivery_location": "в офис на Гражданском",
        },
    )
    structured_service = TelegramIntakeExtractionService(
        FakeApprovalExtractionProvider(raw),
        date_parser=dates,
    )
    resolution = structured_service.resolve_message(
        text,
        RequestDraftData(),
        None,
        deterministic,
        source_kind="initial_description",
        merge_deterministic=True,
        fallback_on_error=True,
    )
    assert resolution.structured is not None
    assert resolution.update is not None
    assert deterministic.values["quantity"] == Decimal("7")
    assert deterministic.values["unit"] == "шт."
    assert "unit" not in resolution.structured.update.values
    assert resolution.update.values["unit"] == "шт."

    dependencies, _ = _build(monkeypatch, SequenceProvider([raw]))
    message = FakeMessage(text, 500)

    asyncio.run(handle_text_message(message, dependencies))
    persisted = dependencies.intake_adapter._orchestrator.get_active_session(USER_ID)
    draft = persisted.intake_result.draft
    completeness = persisted.intake_result.completeness
    card = RequestCardBuilder().build(draft)
    card_view = persisted.model_copy(deep=True)
    card_view.intake_result.request_card = card
    rendered = format_request_card(card_view)

    assert len(message.answers) == 1
    assert draft.procurement_type == "goods"
    assert draft.item_name == "тарелки"
    assert draft.quantity == Decimal("7")
    assert draft.unit == "шт."
    assert draft.amount == Decimal("4000")
    assert draft.budget_status == "unbudgeted"
    assert draft.desired_delivery_date == date(2026, 8, 3)
    assert draft.delivery_location == "в офис на Гражданском"
    assert "unit" in completeness.completed_fields
    assert "unit" not in completeness.missing_fields
    assert persisted.intake_result.next_question is None or (
        persisted.intake_result.next_question.field_code != "unit"
    )
    assert "7 шт." in rendered
