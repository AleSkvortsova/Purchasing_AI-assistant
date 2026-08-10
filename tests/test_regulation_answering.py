from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.rag.answering import (
    FakeGroundedAnswerProvider,
    GroundedAnswerPayload,
    GroundedClaim,
    OpenAIGroundedAnswerProvider,
    RegulationQuestionAnsweringService,
)
from app.rag.exceptions import AnswerGenerationError, RetrievalError
from app.rag.models import HybridRetrievalResult


def _chunk(
    chunk_id: str,
    document_id: str,
    title: str,
    content: str = (
        "Матрица согласования определяет согласующих и срок ответа. "
        "Бюджетная закупка до 100000 руб | Руководитель подразделения."
    ),
    document_type: str = "approval_rules",
) -> HybridRetrievalResult:
    return HybridRetrievalResult(
        chunk_id=UUID(chunk_id),
        document_id=document_id,
        source_filename=f"{document_id}.md",
        document_title=title,
        document_type=document_type,
        section_path=f"{title} > Раздел",
        heading="Раздел",
        content=content,
        priority=1,
        semantic_rank=1,
        lexical_rank=1,
        hybrid_score=0.03,
    )


CHUNK_A = _chunk(
    "11111111-1111-4111-8111-111111111111",
    "kb-009",
    "09_Правила_согласования.md",
)
CHUNK_B = _chunk(
    "22222222-2222-4222-8222-222222222222",
    "kb-009",
    "Правила согласования заявок",
)


class FakeRetrieval:
    default_top_k = 5
    default_rrf_k = 60

    def __init__(self, results=None, error: Exception | None = None) -> None:
        self.results = list(results or [])
        self.error = error
        self.calls: list[str] = []

    def search(self, query: str):
        self.calls.append(query)
        if self.error:
            raise self.error
        return [item.model_copy(deep=True) for item in self.results]


class FailingProvider:
    def generate(self, question, chunks):
        raise AnswerGenerationError("timeout")


def _payload(
    answer: str,
    cited_ids: list[str],
    *,
    source_conflict: bool = False,
) -> GroundedAnswerPayload:
    return GroundedAnswerPayload(
        answer=answer,
        claims=[GroundedClaim(text=answer, cited_chunk_ids=cited_ids)],
        insufficient_context=False,
        source_conflict=source_conflict,
    )


def test_grounded_answer_uses_retrieved_chunks_and_deduplicates_sources() -> None:
    retrieval = FakeRetrieval([CHUNK_A, CHUNK_B])
    provider = FakeGroundedAnswerProvider(
        _payload(
            "Согласование определено матрицей.",
            [str(CHUNK_A.chunk_id), str(CHUNK_B.chunk_id)],
        )
    )

    result = RegulationQuestionAnsweringService(retrieval, provider).answer(
        "Кто согласует закупку на 100000 рублей, предусмотренную бюджетом?"
    )

    assert result.status == "answered"
    assert len(retrieval.calls) == 3
    assert "матрица согласования" in retrieval.calls[1]
    assert provider.calls == []
    assert result.diagnostics["deterministic_resolution"] is True
    assert [source.document_id for source in result.sources] == ["kb-009"]
    assert result.sources[0].display_name == "Правила согласования"
    assert result.sources[0].chunk_id is not None
    assert "chunk_id" not in result.model_dump(mode="json")["sources"][0]


def test_empty_question_does_not_call_retrieval() -> None:
    retrieval = FakeRetrieval([CHUNK_A])
    provider = FakeGroundedAnswerProvider()

    result = RegulationQuestionAnsweringService(retrieval, provider).answer("   ")

    assert result.status == "insufficient_context"
    assert retrieval.calls == []
    assert provider.calls == []


def test_too_long_question_does_not_call_retrieval() -> None:
    retrieval = FakeRetrieval([CHUNK_A])
    provider = FakeGroundedAnswerProvider()

    result = RegulationQuestionAnsweringService(retrieval, provider).answer(
        "в" * 1501
    )

    assert result.status == "insufficient_context"
    assert result.refusal_reason == "question_too_long"
    assert retrieval.calls == []
    assert provider.calls == []


def test_no_chunks_returns_insufficient_context_without_provider() -> None:
    retrieval = FakeRetrieval([])
    provider = FakeGroundedAnswerProvider()

    result = RegulationQuestionAnsweringService(retrieval, provider).answer(
        "Вопрос вне базы"
    )

    assert result.status == "insufficient_context"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("retrieval", "provider"),
    [
        (FakeRetrieval(error=RetrievalError("offline")), FakeGroundedAnswerProvider()),
    ],
)
def test_technical_failures_return_unavailable(retrieval, provider) -> None:
    result = RegulationQuestionAnsweringService(retrieval, provider).answer(
        "Кто согласует закупку на 100000 рублей, предусмотренную бюджетом?"
    )
    assert result.status == "unavailable"
    assert result.sources == []


@pytest.mark.parametrize("cited_ids", [[], ["unknown-chunk"]])
def test_invalid_provider_evidence_uses_deterministic_rule(cited_ids) -> None:
    provider = FakeGroundedAnswerProvider(
        _payload("Неподтверждённый ответ", cited_ids)
    )
    result = RegulationQuestionAnsweringService(
        FakeRetrieval([CHUNK_A]), provider
    ).answer("Кто согласует закупку на 100000 рублей, предусмотренную бюджетом?")
    assert result.status == "answered"
    assert result.diagnostics["deterministic_resolution"] is True
    assert provider.calls == []
    assert [source.document_id for source in result.sources] == ["kb-009"]


def test_long_verbatim_source_copy_is_rejected() -> None:
    source = "Правило " * 80
    chunk = CHUNK_A.model_copy(update={"content": source})
    provider = FakeGroundedAnswerProvider(
        _payload(source, [str(chunk.chunk_id)])
    )
    result = RegulationQuestionAnsweringService(
        FakeRetrieval([chunk]), provider
    ).answer("Вопрос")
    assert result.status == "insufficient_context"


def test_undisclosed_source_conflict_is_rejected() -> None:
    provider = FakeGroundedAnswerProvider(
        _payload(
            "Срок составляет пять дней.",
            [str(CHUNK_A.chunk_id)],
            source_conflict=True,
        )
    )

    result = RegulationQuestionAnsweringService(
        FakeRetrieval([CHUNK_A]), provider
    ).answer("Какой срок?")

    assert result.status == "insufficient_context"


def test_questions_keep_separate_retrieval_contexts() -> None:
    retrieval = FakeRetrieval([CHUNK_A])
    provider = FakeGroundedAnswerProvider()
    service = RegulationQuestionAnsweringService(retrieval, provider)

    first = "Кто согласует закупку на 100000 рублей, предусмотренную бюджетом?"
    second = "Кто согласует закупку на 90000 рублей, предусмотренную бюджетом?"
    service.answer(first)
    service.answer(second)

    assert len(retrieval.calls) == 6
    assert provider.calls == []


def test_openai_provider_uses_strict_payload_and_safe_request_contract() -> None:
    client = Mock()
    client.responses.parse.return_value = SimpleNamespace(
        output_parsed=GroundedAnswerPayload(
            answer="Ответ",
            claims=[
                GroundedClaim(
                    text="Ответ",
                    cited_chunk_ids=[str(CHUNK_A.chunk_id)],
                )
            ],
            insufficient_context=False,
            source_conflict=False,
        )
    )
    provider = OpenAIGroundedAnswerProvider(
        api_key="test-only",
        model="test-model",
        timeout_seconds=12,
        client=client,
    )

    provider.generate("Вопрос", [CHUNK_A])

    kwargs = client.responses.parse.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["text_format"] is GroundedAnswerPayload
    assert kwargs["store"] is False
    assert kwargs["timeout"] == 12
    assert "cited_chunk_ids" in kwargs["instructions"]
    assert str(CHUNK_A.chunk_id) in kwargs["input"]


def test_openai_provider_repair_uses_safe_reason_without_original_output() -> None:
    client = Mock()
    client.responses.parse.return_value = SimpleNamespace(
        output_parsed=GroundedAnswerPayload(
            answer="Ответ",
            claims=[],
            insufficient_context=True,
            source_conflict=False,
        )
    )
    provider = OpenAIGroundedAnswerProvider(
        api_key="test-only",
        model="test-model",
        client=client,
    )

    provider.repair("Вопрос", [CHUNK_A], "unknown_chunk_id")

    kwargs = client.responses.parse.call_args.kwargs
    assert "unknown_chunk_id" in kwargs["instructions"]
    assert "Предыдущий структурированный ответ" in kwargs["instructions"]
    assert "API key" not in kwargs["instructions"]


def test_grounded_payload_schema_is_closed_and_all_fields_are_required() -> None:
    schema = GroundedAnswerPayload.model_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])
    assert all("default" not in value for value in schema["properties"].values())
    claim_schema = schema["$defs"]["GroundedClaim"]
    assert claim_schema["additionalProperties"] is False
    assert set(claim_schema["properties"]) == set(claim_schema["required"])


@pytest.mark.parametrize(
    "question",
    [
        "Подскажи рецепт борща на четыре порции.",
        "Какая погода завтра в Москве?",
        "Напиши функцию на Python для сортировки списка.",
    ],
)
def test_outside_domain_is_rejected_before_retrieval(question: str) -> None:
    retrieval = FakeRetrieval([CHUNK_A])
    provider = FakeGroundedAnswerProvider()

    result = RegulationQuestionAnsweringService(retrieval, provider).answer(question)

    assert result.status == "insufficient_context"
    assert result.refusal_reason == "outside_domain"
    assert result.sources == []
    assert result.diagnostics["retrieval_status"] == "not_called"
    assert retrieval.calls == []
    assert provider.calls == []


def test_secondary_intent_claim_cannot_replace_primary_answer() -> None:
    fields = _chunk(
        "33333333-3333-4333-8333-333333333333",
        "kb-005",
        "Матрица полей",
        content="Для заявки обязательно указать описание предмета закупки.",
        document_type="field_matrix",
    )
    retrieval = FakeRetrieval([fields])
    provider = FakeGroundedAnswerProvider(
        _payload(
            "Для заявки обязательно указать описание предмета закупки.",
            [str(fields.chunk_id)],
        )
    )

    result = RegulationQuestionAnsweringService(retrieval, provider).answer(
        "Закупка на 90000 рублей предусмотрена бюджетом. Кто согласует и что указать?"
    )

    assert result.status == "insufficient_context"
    assert result.refusal_reason == "unsupported_answer"
