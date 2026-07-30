from uuid import UUID

import pytest

from app.rag.answering import (
    FakeGroundedAnswerProvider,
    GroundedAnswerPayload,
    GroundedClaim,
    RegulationQuestionAnsweringService,
)
from app.rag.models import HybridRetrievalResult


class StaticRetrieval:
    default_top_k = 5
    default_rrf_k = 60

    def __init__(self, chunks) -> None:
        self.chunks = list(chunks)
        self.calls: list[str] = []

    def search(self, query: str):
        self.calls.append(query)
        return [item.model_copy(deep=True) for item in self.chunks]


def _result(
    chunk_id: str,
    document_id: str,
    document_type: str,
    title: str,
    section: str,
    content: str,
) -> HybridRetrievalResult:
    return HybridRetrievalResult(
        chunk_id=UUID(chunk_id),
        document_id=document_id,
        source_filename=f"{document_id}.md",
        document_title=title,
        document_type=document_type,
        section_path=section,
        heading=section,
        content=content,
        priority=1,
        hybrid_score=0.03,
    )


APPROVAL = _result(
    "11111111-1111-4111-8111-111111111111",
    "kb-009",
    "approval_rules",
    "Правила согласования заявок",
    "Матрица согласования",
    "Бюджетная закупка 100 001–500 000 руб.: руководитель подразделения "
    "и финансовый контролёр. Срок ответа одного согласующего — один рабочий день.",
)
URGENCY = _result(
    "22222222-2222-4222-8222-222222222222",
    "kb-006",
    "urgency_rules",
    "Правила срочности и приоритета",
    "Нормативные сроки и обязательные данные",
    "Мероприятие: 30 календарных дней. P2 применяется при сроке меньше "
    "нормативного или риске срыва мероприятия. Для срочной заявки указываются "
    "причина, дата возникновения, последствия задержки, крайняя дата, "
    "временная альтернатива и подтверждение руководителя.",
)
TRANSPORT = _result(
    "33333333-3333-4333-8333-333333333333",
    "kb-005",
    "field_matrix",
    "Матрица обязательных полей",
    "Категориальные поля",
    "S03 Транспорт: маршрут, груз, вес или объём, даты и условия погрузки.",
)
TRANSPORT_EXAMPLE = _result(
    "44444444-4444-4444-8444-444444444444",
    "kb-011",
    "examples",
    "Примеры корректных и некорректных заявок",
    "Корректная перевозка",
    "Перевезти 12 паллет весом около 4 тонн со склада в Химках на выставку "
    "в Москве 04.09.2026. Нужна машина с гидробортом. Бюджет 35 000 рублей.",
)
MIXED = _result(
    "55555555-5555-4555-8555-555555555555",
    "kb-015",
    "error_guide",
    "Типовые ошибки при оформлении заявок",
    "В одной заявке объединены разные категории",
    "Товары и услуги разных категорий нужно разделить на отдельные заявки по "
    "однородным категориям.",
)
MIXED_TEMPLATE = _result(
    "66666666-6666-4666-8666-666666666666",
    "kb-002",
    "template",
    "Шаблон заявки на товар",
    "Правила",
    "Несколько позиций объединяются, только если относятся к одной категории.",
)
STATUS = _result(
    "77777777-7777-4777-8777-777777777777",
    "kb-010",
    "faq",
    "FAQ внутренних заказчиков",
    "Что означает «Требует доработки»?",
    "Статус «Требует доработки» означает, что закупщик указал, какие сведения "
    "нужно дополнить.",
)
APPROVAL_HIGH = _result(
    "88888888-8888-4888-8888-888888888888",
    "kb-009",
    "approval_rules",
    "Правила согласования заявок",
    "Матрица согласования",
    "Бюджетная закупка свыше 500 000 руб.: руководитель подразделения, "
    "финансовый блок и руководитель закупок. Срок ответа одного "
    "согласующего — один рабочий день.",
)
URGENCY_THRESHOLD = _result(
    "99999999-9999-4999-8999-999999999999",
    "kb-006",
    "urgency_rules",
    "Правила срочности и приоритета",
    "Нормативные сроки",
    "Мероприятие: 30 календарных дней.",
)
URGENCY_P2 = _result(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "kb-006",
    "urgency_rules",
    "Правила срочности и приоритета",
    "P2 — высокий",
    "P2 применяется при сроке меньше нормативного или риске срыва мероприятия.",
)


def _payload(answer: str, claims: list[tuple[str, list[HybridRetrievalResult]]]):
    return GroundedAnswerPayload(
        answer=answer,
        claims=[
            GroundedClaim(
                text=text,
                cited_chunk_ids=[str(item.chunk_id) for item in cited],
            )
            for text, cited in claims
        ],
        insufficient_context=False,
        source_conflict=False,
    )


def test_budgeted_180000_route_and_deadline_are_answered() -> None:
    answer = (
        "Бюджетную закупку на 180 000 рублей согласуют руководитель "
        "подразделения и финансовый контролёр. Рекомендуемый срок ответа "
        "одного согласующего — один рабочий день."
    )
    claims = [
        (
            "Бюджетную закупку на 180 000 рублей согласуют руководитель "
            "подразделения и финансовый контролёр.",
            [APPROVAL],
        ),
        (
            "Рекомендуемый срок ответа одного согласующего — один рабочий день.",
            [APPROVAL],
        ),
    ]
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([APPROVAL]),
        FakeGroundedAnswerProvider(_payload(answer, claims)),
    ).answer(
        "Закупка на 180 000 рублей предусмотрена бюджетом. "
        "Кто должен её согласовать и за какой срок?"
    )
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-009"]


def test_event_in_two_weeks_uses_urgency_rule_and_required_data() -> None:
    answer = (
        "Для мероприятия нормативный срок составляет 30 календарных дней, "
        "поэтому срок через две недели является основанием для предварительного "
        "P2. Для срочной заявки нужно указать причину, дату возникновения, "
        "последствия задержки, крайнюю дату, временную альтернативу и "
        "подтверждение руководителя."
    )
    claims = [
        (
            "Для мероприятия нормативный срок составляет 30 календарных дней, "
            "поэтому срок через две недели является основанием для "
            "предварительного P2.",
            [URGENCY],
        ),
        (
            "Для срочной заявки нужно указать причину, дату возникновения, "
            "последствия задержки, крайнюю дату, временную альтернативу и "
            "подтверждение руководителя.",
            [URGENCY],
        ),
    ]
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([URGENCY]),
        FakeGroundedAnswerProvider(_payload(answer, claims)),
    ).answer(
        "Мне нужно провести мероприятие через две недели. Будет ли заявка "
        "считаться срочной и что потребуется указать?"
    )
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-006"]


def test_transport_fields_do_not_leak_example_values() -> None:
    answer = "Для перевозки укажите маршрут, груз, вес или объём, даты и погрузку."
    provider = FakeGroundedAnswerProvider(
        _payload(answer, [(answer, [TRANSPORT])])
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([TRANSPORT_EXAMPLE, TRANSPORT]),
        provider,
    ).answer(
        "Какие сведения нужно указать для перевозки 12 паллет со склада "
        "на выставочную площадку?"
    )
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-005"]
    assert [item.document_id for item in provider.calls[0][1]] == ["kb-005"]
    for leaked in ("4 тонн", "Химк", "Москва", "04.09.2026", "гидроборт", "35 000"):
        assert leaked not in result.answer


def test_concrete_value_from_example_is_rejected_even_with_normative_context() -> None:
    answer = "Общий вес составляет около 4 тонн."
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([TRANSPORT_EXAMPLE, TRANSPORT]),
        FakeGroundedAnswerProvider(_payload(answer, [(answer, [TRANSPORT])])),
    ).answer("Какие сведения нужны для перевозки 12 паллет?")
    assert result.status == "insufficient_context"
    assert result.refusal_reason == "unsupported_answer"


def test_mixed_categories_do_not_use_template_as_final_source() -> None:
    answer = "Товары и услуги разных категорий нужно разделить на отдельные заявки."
    provider = FakeGroundedAnswerProvider(_payload(answer, [(answer, [MIXED])]))
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([MIXED_TEMPLATE, MIXED]),
        provider,
    ).answer("Можно ли объединить товары, лицензии и услуги в одной заявке?")
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-015"]
    assert all(item.document_type != "template" for item in provider.calls[0][1])


def test_status_rework_is_answered_from_faq() -> None:
    answer = (
        "Статус «Требует доработки» означает, что закупщик указал, "
        "какие сведения нужно дополнить."
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([STATUS]),
        FakeGroundedAnswerProvider(_payload(answer, [(answer, [STATUS])])),
    ).answer("Что означает статус «Требует доработки»?")
    assert result.status == "answered"


def test_outside_question_refuses_without_provider() -> None:
    provider = FakeGroundedAnswerProvider()
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([]), provider
    ).answer("Какой поставщик предлагает самые дешёвые ноутбуки?")
    assert result.status == "insufficient_context"
    assert result.refusal_reason == "no_chunks"
    assert provider.calls == []


def test_outside_question_refuses_even_with_nearby_supplier_chunk() -> None:
    provider = FakeGroundedAnswerProvider()
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([STATUS]), provider
    ).answer("Какой поставщик сейчас продаёт самые дешёвые ноутбуки?")
    assert result.status == "insufficient_context"
    assert result.refusal_reason == "no_relevant_normative_chunks"
    assert provider.calls == []


def test_ambiguous_approval_question_asks_for_missing_context() -> None:
    retrieval = StaticRetrieval([APPROVAL])
    provider = FakeGroundedAnswerProvider()
    result = RegulationQuestionAnsweringService(retrieval, provider).answer(
        "Кто это согласует?"
    )
    assert result.status == "insufficient_context"
    assert result.refusal_reason == "ambiguous_question"
    assert "сумму закупки" in result.answer
    assert retrieval.calls == []
    assert provider.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "Закупка на 530000 руб предусмотрена бюджетом. Кто её согласует?",
        "Закупка на 530 000 рублей предусмотрена бюджетом. Кто её согласует?",
        "Закупка на 530000 ₽ предусмотрена бюджетом. Кто её согласует?",
    ],
)
def test_equivalent_money_formats_keep_grounded_answer(question: str) -> None:
    claim = (
        "Бюджетная закупка на 530000 руб превышает порог 500000 руб, "
        "поэтому её согласуют руководитель подразделения, финансовый блок и "
        "руководитель закупок."
    )
    answer = (
        "Бюджетную закупку на 530 000 ₽ согласуют руководитель подразделения, "
        "финансовый блок и руководитель закупок, поскольку сумма превышает "
        "порог 500 000 рублей."
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([APPROVAL_HIGH]),
        FakeGroundedAnswerProvider(_payload(answer, [(claim, [APPROVAL_HIGH])])),
    ).answer(question)
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-009"]


@pytest.mark.parametrize(
    "duration",
    ["через 10 дней", "через две недели", "через 14 дней"],
)
def test_equivalent_short_durations_use_urgency_rule(duration: str) -> None:
    claim = (
        f"Срок {duration} меньше нормативных 30 календарных дней, поэтому "
        "это основание для предварительного P2."
    )
    normalized_days = "10 дней" if "10" in duration else "14 дней"
    answer = (
        f"Срок {normalized_days} меньше норматива в 30 календарных дней и "
        "является основанием для предварительного P2."
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([URGENCY]),
        FakeGroundedAnswerProvider(_payload(answer, [(claim, [URGENCY])])),
    ).answer(
        f"Мероприятие нужно провести {duration}. Будет ли заявка срочной?"
    )
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-006"]


@pytest.mark.parametrize(
    ("quantity", "route"),
    [
        ("5", "из офиса на склад"),
        ("12", "со склада на выставочную площадку"),
    ],
)
def test_transport_user_values_survive_validation(
    quantity: str,
    route: str,
) -> None:
    claim = (
        f"Уже указаны {quantity} паллет и маршрут {route}; дополните сведения "
        "о грузе, весе или объёме, датах и погрузке."
    )
    answer = (
        f"Уже указано: {quantity} паллет; маршрут — {route}. Нужно дополнить "
        "сведения о грузе, весе или объёме, датах и погрузке."
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([TRANSPORT]),
        FakeGroundedAnswerProvider(_payload(answer, [(claim, [TRANSPORT])])),
    ).answer(
        f"Какие сведения нужны для перевозки {quantity} паллет {route}?"
    )
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-005"]


def test_unique_normative_source_is_resolved_for_uncited_transport_claim() -> None:
    claim = "Для перевозки укажите маршрут, груз, вес или объём, даты и погрузку."
    payload = GroundedAnswerPayload(
        answer=claim,
        claims=[GroundedClaim(text=claim, cited_chunk_ids=[])],
        insufficient_context=False,
        source_conflict=False,
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([TRANSPORT]),
        FakeGroundedAnswerProvider(payload),
    ).answer("Какие сведения нужны для перевозки 12 паллет?")
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-005"]


def test_uncited_claim_with_ambiguous_support_is_still_rejected() -> None:
    duplicate = TRANSPORT.model_copy(
        update={"chunk_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")}
    )
    claim = "Для перевозки укажите маршрут, груз, вес или объём, даты и погрузку."
    payload = GroundedAnswerPayload(
        answer=claim,
        claims=[GroundedClaim(text=claim, cited_chunk_ids=[])],
        insufficient_context=False,
        source_conflict=False,
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([TRANSPORT, duplicate]),
        FakeGroundedAnswerProvider(payload),
    ).answer("Какие сведения нужны для перевозки 12 паллет?")
    assert result.status == "insufficient_context"
    assert result.diagnostics["validation_rule"] == "claim_without_source"


def test_duration_threshold_support_is_added_deterministically() -> None:
    claim = (
        "Срок через 10 дней меньше нормативного, поэтому это основание для "
        "предварительного P2."
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([URGENCY_P2, URGENCY_THRESHOLD]),
        FakeGroundedAnswerProvider(_payload(claim, [(claim, [URGENCY_P2])])),
    ).answer("Мероприятие нужно провести через 10 дней. Будет ли заявка срочной?")
    assert result.status == "answered"
    assert [source.document_id for source in result.sources] == ["kb-006"]


@pytest.mark.parametrize(
    "duration",
    ["через 10 дней", "через две недели", "через 14 дней"],
)
def test_supported_urgent_event_uses_deterministic_provider_fallback(
    duration: str,
) -> None:
    fields = _result(
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "kb-006",
        "urgency_rules",
        "Правила срочности и приоритета",
        "Обязательные данные для срочной заявки",
        "Обязательные данные для срочной заявки\n- причина срочности;\n"
        "- последствия задержки;\n- подтверждение руководителя.",
    )
    provider_payload = GroundedAnswerPayload(
        answer="",
        claims=[],
        insufficient_context=True,
        source_conflict=False,
    )
    result = RegulationQuestionAnsweringService(
        StaticRetrieval([URGENCY_THRESHOLD, URGENCY_P2, fields]),
        FakeGroundedAnswerProvider(provider_payload),
    ).answer(
        f"Мероприятие нужно провести {duration}. Будет ли заявка срочной "
        "и что потребуется указать?"
    )
    assert result.status == "answered"
    assert result.refusal_reason is None
    assert "30 календарных дней" in result.answer
    assert "предварительного приоритета P2" in result.answer
    if duration == "через две недели":
        assert (
            "До мероприятия осталось две недели, то есть 14 дней. "
            "Это меньше нормативного срока в 30 календарных дней"
            in result.answer
        )
    forbidden_terms = {
        "нормализовано",
        "validation",
        "claim",
        "chunk",
        "retrieval",
        "threshold",
    }
    assert forbidden_terms.isdisjoint(result.answer.casefold().split())
    assert [source.document_id for source in result.sources] == ["kb-006"]
