import json
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.rag.exceptions import (
    AnswerGenerationError,
    AnswerProviderUnavailableError,
    MalformedAnswerResponseError,
    RagError,
)
from app.rag.models import SearchResult
from app.rag.regulation_queries import (
    RegulationQueryPlan,
    build_regulation_query_plan,
    fuse_regulation_results,
    normalize_regulation_query,
    select_relevant_regulation_chunks,
    source_kind,
)
from app.rag.retrieval_service import KnowledgeRetrievalService
from app.rag.value_normalization import (
    duration_below_threshold,
    normalize_money_amount,
    normalize_regulation_text,
    parse_duration_days,
    parse_money_ranges,
    value_in_range,
)

_PROMPT_PATH = Path(__file__).with_name("prompts") / "regulation_qa.md"
_INSUFFICIENT = (
    "В регламентирующих документах не найдено достаточно информации для "
    "уверенного ответа. Попробуйте уточнить вопрос или обратитесь в отдел "
    "закупок."
)
_UNAVAILABLE = (
    "Сейчас не удалось обратиться к базе регламентов. "
    "Попробуйте повторить вопрос позже."
)
_MAX_QUESTION_LENGTH = 1500
_MAX_ANSWER_LENGTH = 3000
_LONG_QUOTE_LENGTH = 240
_AMBIGUOUS = (
    "Уточните, пожалуйста, сумму закупки, предусмотрена ли она бюджетом "
    "и о каком типе закупки идёт речь."
)
_TECHNICAL_USER_LANGUAGE = re.compile(
    r"\b(?:нормализ\w*|validation|claim|chunk|retrieval|threshold)\b",
    re.IGNORECASE,
)


class RegulationSource(BaseModel):
    document_id: str
    display_name: str
    section: str | None = None
    chunk_id: str | None = Field(default=None, exclude=True)


class RegulationAnswer(BaseModel):
    answer: str
    sources: list[RegulationSource] = Field(default_factory=list)
    status: Literal["answered", "insufficient_context", "unavailable"]
    refusal_reason: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict, exclude=True)


class GroundedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    cited_chunk_ids: list[str]


class GroundedAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    claims: list[GroundedClaim]
    insufficient_context: bool
    source_conflict: bool


@dataclass(frozen=True)
class RegulationRetrievalOutcome:
    plan: RegulationQueryPlan
    candidates: tuple[SearchResult, ...]
    chunks: tuple[SearchResult, ...]
    reason_code: str | None = None


class GroundedAnswerProvider(Protocol):
    def generate(
        self,
        question: str,
        chunks: Sequence[SearchResult],
    ) -> GroundedAnswerPayload: ...


class OpenAIGroundedAnswerProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 30,
        client: OpenAI | None = None,
    ) -> None:
        if not api_key:
            raise AnswerGenerationError(
                "OPENAI_API_KEY is not configured for regulation answers"
            )
        if not model or not model.strip():
            raise AnswerGenerationError("RAG_ANSWER_MODEL is not configured")
        self.model = model.strip()
        self._timeout_seconds = timeout_seconds
        self._client = client or OpenAI(api_key=api_key, timeout=timeout_seconds)

    def generate(
        self,
        question: str,
        chunks: Sequence[SearchResult],
    ) -> GroundedAnswerPayload:
        context = [
            {
                "chunk_id": str(chunk.chunk_id),
                "document": chunk.document_title,
                "document_type": chunk.document_type,
                "source_kind": source_kind(chunk.document_type),
                "section": chunk.section_path,
                "content": chunk.content,
            }
            for chunk in chunks
        ]
        try:
            response = self._client.responses.parse(
                model=self.model,
                instructions=_PROMPT_PATH.read_text(encoding="utf-8"),
                input=json.dumps(
                    {"question": question, "context": context},
                    ensure_ascii=False,
                ),
                text_format=GroundedAnswerPayload,
                store=False,
                timeout=self._timeout_seconds,
            )
        except ValidationError as exc:
            raise MalformedAnswerResponseError(
                "Grounded answer provider returned malformed structured output"
            ) from exc
        except Exception as exc:
            raise AnswerProviderUnavailableError(
                "Grounded answer provider is unavailable"
            ) from exc
        payload = getattr(response, "output_parsed", None)
        if payload is None:
            raise MalformedAnswerResponseError(
                "Grounded answer provider returned no structured output"
            )
        return payload


class FakeGroundedAnswerProvider:
    def __init__(self, payload: GroundedAnswerPayload | None = None) -> None:
        self.payload = payload
        self.calls: list[tuple[str, list[SearchResult]]] = []

    def generate(
        self,
        question: str,
        chunks: Sequence[SearchResult],
    ) -> GroundedAnswerPayload:
        copied = [chunk.model_copy(deep=True) for chunk in chunks]
        self.calls.append((question, copied))
        if self.payload is not None:
            return self.payload.model_copy(deep=True)
        return GroundedAnswerPayload(
            answer="Ответ подтверждён регламентом.",
            claims=(
                [
                    GroundedClaim(
                        text="Ответ подтверждён регламентом.",
                        cited_chunk_ids=[str(chunks[0].chunk_id)],
                    )
                ]
                if chunks
                else []
            ),
            insufficient_context=not chunks,
            source_conflict=False,
        )


class RegulationQuestionAnsweringService:
    def __init__(
        self,
        retrieval: KnowledgeRetrievalService,
        provider: GroundedAnswerProvider,
    ) -> None:
        self._retrieval = retrieval
        self._provider = provider

    def answer(self, question: str) -> RegulationAnswer:
        started = time.perf_counter()
        normalized = question.strip()
        if not normalized:
            return RegulationAnswer(
                answer="Напишите вопрос о регламенте закупок.",
                status="insufficient_context",
                refusal_reason="empty_question",
                diagnostics={"retrieval_status": "not_called", "duration_ms": 0},
            )
        if len(normalized) > _MAX_QUESTION_LENGTH:
            return RegulationAnswer(
                answer="Вопрос слишком длинный. Сформулируйте его короче.",
                status="insufficient_context",
                refusal_reason="question_too_long",
                diagnostics={"retrieval_status": "not_called", "duration_ms": 0},
            )
        plan = build_regulation_query_plan(normalized)
        if plan.ambiguous:
            return RegulationAnswer(
                answer=_AMBIGUOUS,
                status="insufficient_context",
                refusal_reason="ambiguous_question",
                diagnostics={"retrieval_status": "not_called", "duration_ms": 0},
            )
        try:
            retrieval = self.retrieve(plan)
        except RagError as exc:
            return _unavailable(started, type(exc).__name__, "retrieval_failed")
        if not retrieval.chunks:
            return RegulationAnswer(
                answer=_INSUFFICIENT,
                status="insufficient_context",
                refusal_reason=retrieval.reason_code or "no_chunks",
                diagnostics=_diagnostics(
                    started,
                    "empty" if not retrieval.candidates else "filtered",
                    len(retrieval.candidates),
                    0,
                ),
            )
        try:
            payload = self._provider.generate(normalized, retrieval.chunks)
            fallback = _deterministic_urgency_payload(plan, retrieval.chunks)
            if payload.insufficient_context and fallback is not None:
                payload = fallback
            try:
                return self._validate(
                    payload,
                    retrieval.chunks,
                    plan,
                    started,
                )
            except ValueError:
                if fallback is None or payload is fallback:
                    raise
                return self._validate(
                    fallback,
                    retrieval.chunks,
                    plan,
                    started,
                )
        except MalformedAnswerResponseError as exc:
            return _unavailable(started, type(exc).__name__, "malformed_output")
        except AnswerProviderUnavailableError as exc:
            return _unavailable(started, type(exc).__name__, "provider_unavailable")
        except AnswerGenerationError as exc:
            return _unavailable(started, type(exc).__name__, "provider_unavailable")
        except ValueError as exc:
            return _safe_refusal(
                started,
                "unsupported_answer",
                len(retrieval.chunks),
                type(exc).__name__,
                _validation_rule(exc),
            )

    def retrieve(
        self,
        question_or_plan: str | RegulationQueryPlan,
    ) -> RegulationRetrievalOutcome:
        plan = (
            build_regulation_query_plan(question_or_plan)
            if isinstance(question_or_plan, str)
            else question_or_plan
        )
        ranked = [self._retrieval.search(query) for query in plan.variants]
        candidates = fuse_regulation_results(
            ranked,
            rrf_k=self._retrieval.default_rrf_k,
        )
        chunks = select_relevant_regulation_chunks(
            plan,
            candidates,
            limit=self._retrieval.default_top_k,
        )
        reason = None
        if not candidates:
            reason = "no_chunks"
        elif not chunks:
            reason = "no_relevant_normative_chunks"
        return RegulationRetrievalOutcome(
            plan=plan,
            candidates=tuple(candidates),
            chunks=tuple(chunks),
            reason_code=reason,
        )

    def _validate(
        self,
        payload: GroundedAnswerPayload,
        chunks: Sequence[SearchResult],
        plan: RegulationQueryPlan,
        started: float,
    ) -> RegulationAnswer:
        if payload.insufficient_context:
            return RegulationAnswer(
                answer=_INSUFFICIENT,
                status="insufficient_context",
                refusal_reason="no_relevant_normative_chunks",
                diagnostics=_diagnostics(started, "found", len(chunks), 0),
            )
        answer = payload.answer.strip()
        if not answer or len(answer) > _MAX_ANSWER_LENGTH:
            raise ValueError("invalid answer length")
        if _TECHNICAL_USER_LANGUAGE.search(answer):
            raise ValueError("technical terminology in user answer")
        if payload.source_conflict and "противореч" not in answer.casefold():
            raise ValueError("source conflict is not disclosed in the answer")
        by_chunk_id = {str(chunk.chunk_id): chunk for chunk in chunks}
        if not payload.claims:
            raise ValueError("answer has no grounded claims")
        cited_ids: list[str] = []
        for claim in payload.claims:
            claim_text = claim.text.strip()
            if not claim_text or not _claim_present_in_answer(claim_text, answer):
                raise ValueError("grounded claim is absent from final answer")
            if any(item not in by_chunk_id for item in claim.cited_chunk_ids):
                raise ValueError("claim cites unsupported context")
            claim_chunks, resolved_ids = _resolve_claim_sources(
                claim_text,
                claim.cited_chunk_ids,
                chunks,
                by_chunk_id,
                plan,
            )
            if not any(
                _claim_supported_by_chunk(claim_text, item)
                for item in claim_chunks
            ):
                raise ValueError("claim text is not supported by cited context")
            if not _claim_answers_intent(claim_text, plan):
                raise ValueError("claim does not answer the question")
            claim_chunks = _add_deterministic_support(
                claim_text,
                plan.original_query,
                claim_chunks,
                chunks,
            )
            _validate_concrete_values(claim_text, plan.original_query, claim_chunks)
            _validate_deterministic_relations(
                claim_text,
                plan.original_query,
                claim_chunks,
            )
            has_example_source = any(
                source_kind(item.document_type) in {"example", "template"}
                for item in claim_chunks
            )
            if has_example_source:
                if not plan.asks_for_example:
                    raise ValueError("example or template cannot support a user fact")
                if (
                    "пример" not in claim_text.casefold()
                    and "например" not in claim_text.casefold()
                ):
                    raise ValueError("example content is not labelled as an example")
            cited_ids.extend(resolved_ids)
            cited_ids.extend(str(item.chunk_id) for item in claim_chunks)
        cited_ids = list(dict.fromkeys(cited_ids))
        if not cited_ids or any(item not in by_chunk_id for item in cited_ids):
            raise ValueError("answer cites unsupported context")
        cited = [by_chunk_id[item] for item in cited_ids]
        if any(_contains_long_quote(answer, chunk.content) for chunk in cited):
            raise ValueError("answer contains an excessive source quotation")
        sources = _unique_sources(cited)
        return RegulationAnswer(
            answer=answer,
            sources=sources,
            status="answered",
            diagnostics=_diagnostics(
                started,
                "found",
                len(chunks),
                len(sources),
            ),
        )


def _unique_sources(chunks: Sequence[SearchResult]) -> list[RegulationSource]:
    sources: list[RegulationSource] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.document_id in seen:
            continue
        seen.add(chunk.document_id)
        sources.append(
            RegulationSource(
                document_id=chunk.document_id,
                display_name=_display_name(chunk),
                section=chunk.section_path or chunk.heading,
                chunk_id=str(chunk.chunk_id),
            )
        )
    return sources


def _deterministic_urgency_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    if plan.intent != "urgency" or "мероприят" not in plan.normalized_query:
        return None
    duration = parse_duration_days(plan.original_query)
    threshold_chunk = _normative_duration_support(plan.original_query, chunks)
    if duration is None or threshold_chunk is None:
        return None
    threshold = _normative_duration_threshold(
        plan.original_query,
        [threshold_chunk],
    )
    if threshold is None or not duration_below_threshold(duration, threshold):
        return None
    priority_chunk = next(
        (
            chunk
            for chunk in chunks
            if chunk.document_type == "urgency_rules"
            and re.search(r"\bp2\b", chunk.content, re.IGNORECASE)
            and "меньше нормативного" in chunk.content.casefold()
        ),
        None,
    )
    fields_chunk = next(
        (
            chunk
            for chunk in chunks
            if chunk.document_type == "urgency_rules"
            and "обязательные данные для срочной заявки"
            in chunk.content.casefold()
        ),
        None,
    )
    if priority_chunk is None or fields_chunk is None:
        return None
    fields = [
        match.group(1).strip()
        for line in fields_chunk.content.splitlines()
        if (match := re.match(r"^\s*-\s+(.+?)[;.]?\s*$", line))
    ]
    if not fields:
        return None
    if re.search(r"\bдве\s+недел", plan.original_query.casefold()):
        duration_phrase = "До мероприятия осталось две недели, то есть 14 дней."
    else:
        duration_phrase = f"До мероприятия осталось {duration} дней."
    priority_claim = (
        f"{duration_phrase} Это меньше нормативного срока в {threshold} "
        "календарных дней, поэтому это основание для предварительного "
        "приоритета P2."
    )
    fields_claim = (
        "Для срочной заявки укажите: " + "; ".join(fields) + "."
    )
    answer = " ".join((priority_claim, fields_claim))
    return GroundedAnswerPayload(
        answer=answer,
        claims=[
            GroundedClaim(
                text=priority_claim,
                cited_chunk_ids=[
                    str(threshold_chunk.chunk_id),
                    str(priority_chunk.chunk_id),
                ],
            ),
            GroundedClaim(
                text=fields_claim,
                cited_chunk_ids=[str(fields_chunk.chunk_id)],
            ),
        ],
        insufficient_context=False,
        source_conflict=False,
    )


def _display_name(chunk: SearchResult) -> str:
    title = str(chunk.metadata.get("display_name") or chunk.document_title).strip()
    if not title:
        title = chunk.source_filename
    title = re.sub(r"\.md$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^\d{2}[_\s-]+", "", title)
    return title.replace("_", " ").strip()


def _contains_long_quote(answer: str, source: str) -> bool:
    normalized_answer = " ".join(answer.split()).casefold()
    normalized_source = " ".join(source.split()).casefold()
    if len(normalized_answer) < _LONG_QUOTE_LENGTH:
        return False
    return any(
        normalized_answer[index : index + _LONG_QUOTE_LENGTH] in normalized_source
        for index in range(
            0,
            len(normalized_answer) - _LONG_QUOTE_LENGTH + 1,
        )
    )


def _claim_supported_by_chunk(claim: str, chunk: SearchResult) -> bool:
    claim_terms = _support_terms(claim)
    source_terms = _support_terms(
        " ".join(
            (
                chunk.document_title,
                chunk.section_path,
                chunk.heading or "",
                chunk.content,
            )
        )
    )
    return bool(claim_terms & source_terms)


def _claim_present_in_answer(claim: str, answer: str) -> bool:
    claim_terms = Counter(_comparison_terms(claim))
    answer_terms = Counter(_comparison_terms(answer))
    return bool(claim_terms) and all(
        answer_terms[term] >= count for term, count in claim_terms.items()
    )


def _resolve_claim_sources(
    claim: str,
    cited_chunk_ids: Sequence[str],
    chunks: Sequence[SearchResult],
    by_chunk_id: dict[str, SearchResult],
    plan: RegulationQueryPlan,
) -> tuple[list[SearchResult], list[str]]:
    if cited_chunk_ids:
        return (
            [by_chunk_id[item] for item in cited_chunk_ids],
            list(cited_chunk_ids),
        )
    candidates = [
        chunk
        for chunk in chunks
        if source_kind(chunk.document_type) not in {"example", "template"}
        and _claim_supported_by_chunk(claim, chunk)
        and _claim_answers_intent(claim, plan)
    ]
    if len(candidates) != 1:
        raise ValueError("grounded claim has no source")
    selected = candidates[0]
    return [selected], [str(selected.chunk_id)]


def _add_deterministic_support(
    claim: str,
    question: str,
    claim_chunks: Sequence[SearchResult],
    available_chunks: Sequence[SearchResult],
) -> list[SearchResult]:
    resolved = list(claim_chunks)
    duration = parse_duration_days(question)
    if duration is None or not re.search(r"сроч|p2|меньше норматив", claim.casefold()):
        return resolved
    if _normative_duration_threshold(question, resolved) is not None:
        return resolved
    support = _normative_duration_support(question, available_chunks)
    if support is not None and all(
        support.chunk_id != item.chunk_id for item in resolved
    ):
        resolved.append(support)
    return resolved


def _claim_answers_intent(claim: str, plan: RegulationQueryPlan) -> bool:
    normalized = normalize_regulation_query(claim)
    patterns = {
        "approval": r"соглас|руковод|финанс|срок|день",
        "urgency": r"сроч|приоритет|срок|причин|последств|данн|указать",
        "transport_fields": r"маршрут|груз|вес|объем|дат|погруз|перевоз|указать",
        "mixed_categories": r"категор|отдельн|объедин|раздел",
        "status": r"статус|доработ|сведен|закупщик",
        "channel": r"устн|официальн|регистрац|ассистент|заявк",
        "outside_kb": r"$^",
        "generic": r"[a-zа-я0-9]",
    }
    return bool(re.search(patterns[plan.intent], normalized))


def _validate_concrete_values(
    claim: str,
    question: str,
    chunks: Sequence[SearchResult],
) -> None:
    question_values = _concrete_values(question)
    for value in _concrete_values(claim) - question_values:
        if not any(
            source_kind(chunk.document_type) not in {"example", "template"}
            and value in _concrete_values(chunk.content)
            for chunk in chunks
        ):
            raise ValueError(f"unsupported concrete value: {value}")


def _validate_deterministic_relations(
    claim: str,
    question: str,
    chunks: Sequence[SearchResult],
) -> None:
    amount = normalize_money_amount(question)
    approval_chunks = [
        chunk for chunk in chunks if chunk.document_type == "approval_rules"
    ]
    if amount is not None and approval_chunks:
        ranges = [
            item
            for chunk in approval_chunks
            for item in parse_money_ranges(chunk.content)
        ]
        if not ranges:
            raise ValueError("cited approval rule has no deterministic money range")
        if not any(
            value_in_range(
                amount,
                item.minimum,
                item.maximum,
                minimum_inclusive=item.minimum_inclusive,
                maximum_inclusive=item.maximum_inclusive,
            )
            for item in ranges
        ):
            raise ValueError("amount does not match a cited normative range")

    duration = parse_duration_days(question)
    if duration is None or not re.search(r"сроч|p2|меньше норматив", claim.casefold()):
        return
    threshold = _normative_duration_threshold(question, chunks)
    if threshold is None:
        raise ValueError("duration comparison lacks cited normative threshold")
    if not duration_below_threshold(duration, threshold):
        raise ValueError("duration is not below the cited normative threshold")


def _normative_duration_threshold(
    question: str,
    chunks: Sequence[SearchResult],
) -> int | None:
    support = _normative_duration_support(question, chunks)
    if support is None:
        return None
    for line in support.content.splitlines():
        normalized_question = normalize_regulation_text(question)
        if "мероприят" in normalized_question and "мероприят" not in line.casefold():
            continue
        threshold = parse_duration_days(line)
        if threshold is not None:
            return threshold
    return None


def _normative_duration_support(
    question: str,
    chunks: Sequence[SearchResult],
) -> SearchResult | None:
    subject_patterns = []
    normalized_question = normalize_regulation_text(question)
    if "мероприят" in normalized_question:
        subject_patterns.append("мероприят")
    for chunk in chunks:
        if chunk.document_type not in {"urgency_rules", "field_matrix"}:
            continue
        lines = chunk.content.splitlines()
        matching = [
            line
            for line in lines
            if not subject_patterns
            or any(pattern in line.casefold() for pattern in subject_patterns)
        ]
        for line in matching:
            threshold = parse_duration_days(line)
            if threshold is not None:
                return chunk
    return None


def _concrete_values(value: str) -> set[str]:
    normalized = normalize_regulation_text(value)
    dates = set(re.findall(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", normalized))
    numbers = {
        item.replace(",", ".")
        for item in re.findall(r"(?<![a-zа-я])\d+(?:[.,]\d+)?", normalized)
    }
    return dates | numbers


def _support_terms(value: str) -> set[str]:
    normalized = normalize_regulation_text(value)
    return {
        term[:6] if len(term) >= 7 else term
        for term in re.findall(r"[a-zа-я0-9]+", normalized)
        if len(term) >= 4 and term not in {"котор", "этого", "таким", "нужно"}
    }


def _comparison_terms(value: str) -> list[str]:
    normalized = normalize_regulation_text(value)
    terms = re.findall(r"[a-zа-я0-9]+", normalized)
    ignored = {
        "а",
        "его",
        "ее",
        "и",
        "их",
        "поскольку",
        "поэтому",
        "р",
        "руб",
        "рубль",
        "рубля",
        "рублей",
        "рубли",
        "через",
        "это",
    }
    return [
        term[:6] if len(term) >= 7 else term
        for term in terms
        if term not in ignored
    ]


def _diagnostics(
    started: float,
    retrieval_status: str,
    chunk_count: int,
    source_count: int,
) -> dict[str, Any]:
    return {
        "retrieval_status": retrieval_status,
        "chunk_count": chunk_count,
        "source_count": source_count,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


def _unavailable(
    started: float,
    error_code: str,
    stage: str,
) -> RegulationAnswer:
    return RegulationAnswer(
        answer=_UNAVAILABLE,
        status="unavailable",
        refusal_reason=stage,
        diagnostics={
            **_diagnostics(started, "failed", 0, 0),
            "error_code": error_code,
        },
    )


def _safe_refusal(
    started: float,
    reason_code: str,
    chunk_count: int,
    error_code: str,
    validation_rule: str | None = None,
) -> RegulationAnswer:
    return RegulationAnswer(
        answer=_INSUFFICIENT,
        status="insufficient_context",
        refusal_reason=reason_code,
        diagnostics={
            **_diagnostics(started, "rejected", chunk_count, 0),
            "error_code": error_code,
            "validation_rule": validation_rule,
        },
    )


def _validation_rule(exc: ValueError) -> str:
    message = str(exc)
    rules = {
        "grounded claim is absent": "claim_not_in_answer",
        "grounded claim has no source": "claim_without_source",
        "claim cites unsupported": "unknown_chunk_id",
        "claim text is not supported": "claim_not_supported_by_source",
        "claim does not answer": "claim_not_relevant",
        "unsupported concrete value": "unsupported_concrete_value",
        "example or template": "example_source_not_allowed",
        "example content is not labelled": "example_not_labelled",
        "amount does not match": "amount_outside_cited_ranges",
        "cited approval rule has no": "missing_cited_money_range",
        "duration comparison lacks": "missing_cited_duration_threshold",
        "duration is not below": "duration_not_below_threshold",
        "answer cites unsupported": "unsupported_answer_citation",
        "excessive source quotation": "excessive_source_quote",
        "answer has no grounded": "no_grounded_claims",
        "invalid answer length": "invalid_answer_length",
        "source conflict": "undisclosed_source_conflict",
        "technical terminology": "technical_user_language",
    }
    return next(
        (code for prefix, code in rules.items() if message.startswith(prefix)),
        "validation_error",
    )
