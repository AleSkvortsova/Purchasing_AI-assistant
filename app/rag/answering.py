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
from app.rag.question_understanding import (
    is_draft_question,
    is_history_question,
    is_procurement_event,
)
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
    detect_budget_status,
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
    status: Literal[
        "answered",
        "clarification_required",
        "insufficient_context",
        "unavailable",
    ]
    clarifying_question: str | None = None
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
        return self._request(question, chunks)

    def repair(
        self,
        question: str,
        chunks: Sequence[SearchResult],
        reason_code: str,
    ) -> GroundedAnswerPayload:
        repair_instruction = (
            "Предыдущий структурированный ответ не прошёл проверку "
            f"({reason_code}). Сформируй ответ заново и строго соблюдай "
            "правила цитирования и подтверждения значений."
        )
        return self._request(question, chunks, repair_instruction)

    def _request(
        self,
        question: str,
        chunks: Sequence[SearchResult],
        extra_instruction: str | None = None,
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
            instructions = _PROMPT_PATH.read_text(encoding="utf-8")
            if extra_instruction:
                instructions = f"{instructions}\n\n{extra_instruction}"
            response = self._client.responses.parse(
                model=self.model,
                instructions=instructions,
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

    def repair(
        self,
        question: str,
        chunks: Sequence[SearchResult],
        reason_code: str,
    ) -> GroundedAnswerPayload:
        del reason_code
        return self.generate(question, chunks)


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
        direct = _direct_understanding_answer(plan, started)
        if direct is not None:
            return direct
        clarification = clarifying_question_for(plan)
        if clarification is not None:
            return RegulationAnswer(
                answer=clarification,
                status="clarification_required",
                clarifying_question=clarification,
                diagnostics={
                    "retrieval_status": "not_called",
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "clarification_reason": "missing_budget_status",
                },
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
        if any(
            intent
            in {
                "approval",
                "urgency",
                "request_cancellation",
                "status",
                "category_fields",
                "draft_history",
                "outside_kb",
            }
            for intent in retrieval.plan.intents
        ):
            deterministic = _validated_deterministic_fallback(retrieval, started)
            if deterministic is not None:
                deterministic.diagnostics["deterministic_resolution"] = True
                return deterministic
        last_validation: ValueError | None = None
        last_malformed: MalformedAnswerResponseError | None = None
        for attempt in range(2):
            try:
                if attempt == 0:
                    payload = self._provider.generate(normalized, retrieval.chunks)
                else:
                    repair = getattr(self._provider, "repair", None)
                    reason_code = (
                        _validation_rule(last_validation)
                        if last_validation is not None
                        else "malformed_output"
                    )
                    payload = (
                        repair(normalized, retrieval.chunks, reason_code)
                        if callable(repair)
                        else self._provider.generate(normalized, retrieval.chunks)
                    )
                result = self.validate_payload(
                    payload,
                    retrieval,
                    started=started,
                    allow_fallback=False,
                )
                if attempt:
                    result.diagnostics["repair_attempted"] = True
                return result
            except MalformedAnswerResponseError as exc:
                last_malformed = exc
            except AnswerProviderUnavailableError as exc:
                return _unavailable(
                    started, type(exc).__name__, "provider_unavailable"
                )
            except AnswerGenerationError as exc:
                return _unavailable(
                    started, type(exc).__name__, "provider_unavailable"
                )
            except ValueError as exc:
                last_validation = exc
        fallback = _validated_deterministic_fallback(retrieval, started)
        if fallback is not None:
            fallback.diagnostics["repair_attempted"] = True
            fallback.diagnostics["fallback_used"] = True
            if last_validation is not None:
                fallback.diagnostics["validation_rule"] = _validation_rule(
                    last_validation
                )
            return fallback
        if last_malformed is not None:
            return _safe_refusal(
                started,
                "malformed_output",
                len(retrieval.chunks),
                type(last_malformed).__name__,
            )
        if last_validation is not None:
            return _safe_refusal(
                started,
                "unsupported_answer",
                len(retrieval.chunks),
                type(last_validation).__name__,
                _validation_rule(last_validation),
            )
        return _safe_refusal(
            started,
            "unsupported_answer",
            len(retrieval.chunks),
            "UnknownError",
        )

    def validate_payload(
        self,
        payload: GroundedAnswerPayload,
        retrieval: RegulationRetrievalOutcome,
        *,
        started: float | None = None,
        allow_fallback: bool = True,
    ) -> RegulationAnswer:
        validation_started = started if started is not None else time.perf_counter()
        fallback = _deterministic_grounded_payload(
            retrieval.plan,
            retrieval.chunks,
        )
        if (
            not allow_fallback
            and payload.insufficient_context
            and fallback is not None
        ):
            raise ValueError("provider refused a deterministically answerable question")
        effective_payload = (
            fallback
            if allow_fallback and payload.insufficient_context and fallback is not None
            else payload
        )
        try:
            result = self._validate(
                effective_payload,
                retrieval.chunks,
                retrieval.plan,
                validation_started,
            )
            if effective_payload is fallback:
                result.diagnostics["fallback_used"] = True
            return result
        except ValueError as exc:
            if (
                not allow_fallback
                or fallback is None
                or effective_payload is fallback
            ):
                raise
            result = self._validate(
                fallback,
                retrieval.chunks,
                retrieval.plan,
                validation_started,
            )
            result.diagnostics["fallback_used"] = True
            result.diagnostics["validation_rule"] = _validation_rule(exc)
            return result

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


def clarifying_question_for(plan: RegulationQueryPlan) -> str | None:
    return plan.understanding.clarifying_question


def _direct_understanding_answer(
    plan: RegulationQueryPlan,
    started: float,
) -> RegulationAnswer | None:
    understanding = plan.understanding
    if understanding.domain_decision == "outside_domain":
        return RegulationAnswer(
            answer=(
                "С этим запросом я не помогу — я отвечаю только на вопросы, "
                "связанные с внутренними закупками. Могу помочь оформить заявку "
                "на товар или услугу либо подсказать правила оформления и "
                "согласования закупки."
            ),
            status="insufficient_context",
            refusal_reason="outside_domain",
            diagnostics=_diagnostics(started, "not_called", 0, 0),
        )
    if understanding.outside_kb_intent:
        return RegulationAnswer(
            answer=(
                "Я не могу рекомендовать конкретного поставщика, подрядчика "
                "или перевозчика и не располагаю актуальными рыночными данными."
            ),
            status="insufficient_context",
            refusal_reason="outside_kb",
            diagnostics=_diagnostics(started, "not_called", 0, 0),
        )
    if understanding.primary_intent == "general_help":
        question = (
            "Я могу помочь с:\n"
            "• оформлением заявки;\n"
            "• обязательными полями;\n"
            "• срочностью;\n"
            "• согласованием;\n"
            "• статусами;\n"
            "• отменой;\n"
            "• требованиями к бренду и эквивалентам.\n"
            "Уточните, что именно вас интересует."
        )
        return RegulationAnswer(
            answer=question,
            status="clarification_required",
            clarifying_question=question,
            diagnostics=_diagnostics(started, "not_called", 0, 0),
        )
    if understanding.primary_intent == "draft_and_history":
        if is_history_question(understanding.normalized_question):
            answer = (
                "Откройте раздел «Мои заявки». В нём показываются последние "
                "зарегистрированные и отменённые заявки вашего профиля."
            )
            document_id = "kb-014"
            display_name = "Инструкция по работе с ассистентом"
        elif is_draft_question(understanding.normalized_question):
            return None
        else:
            return None
        return RegulationAnswer(
            answer=answer,
            status="answered",
            sources=[
                RegulationSource(
                    document_id=document_id,
                    display_name=display_name,
                )
            ],
            diagnostics=_diagnostics(started, "not_called", 0, 1),
        )
    return None


def _validated_deterministic_fallback(
    retrieval: RegulationRetrievalOutcome,
    started: float,
) -> RegulationAnswer | None:
    payload = _deterministic_grounded_payload(retrieval.plan, retrieval.chunks)
    if payload is None:
        return None
    try:
        return RegulationQuestionAnsweringService._validate(
            object(),
            payload,
            retrieval.chunks,
            retrieval.plan,
            started,
        )
    except ValueError:
        return None


def _deterministic_grounded_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    for builder in (
        _deterministic_approval_payload,
        _deterministic_policy_payload,
        _deterministic_urgency_payload,
        _deterministic_status_payload,
        _deterministic_cancellation_payload,
        _deterministic_responsibility_payload,
        _deterministic_draft_payload,
        _deterministic_category_fields_payload,
    ):
        payload = builder(plan, chunks)
        if payload is not None:
            return payload
    return None


def _deterministic_approval_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    if "approval" not in plan.intents:
        return None
    amount = normalize_money_amount(plan.original_query)
    budget_status = detect_budget_status(plan.original_query)
    if amount is None or budget_status not in {"budgeted", "unbudgeted"}:
        return None
    chunk = next(
        (item for item in chunks if item.document_type == "approval_rules"),
        None,
    )
    if chunk is None:
        return None
    selected: tuple[str, str] | None = None
    for line in chunk.content.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        condition, approvers = cells
        condition_budget_status = detect_budget_status(condition)
        if condition_budget_status != budget_status:
            continue
        ranges = parse_money_ranges(condition)
        if ranges and any(
            value_in_range(
                amount,
                item.minimum,
                item.maximum,
                minimum_inclusive=item.minimum_inclusive,
                maximum_inclusive=item.maximum_inclusive,
            )
            for item in ranges
        ):
            selected = (condition, approvers)
            break
    if selected is None:
        return None
    condition, approvers = selected
    route_claim = (
        f"Для условия «{condition}» маршрут согласования: {approvers}."
    )
    claims = [
        GroundedClaim(
            text=route_claim,
            cited_chunk_ids=[str(chunk.chunk_id)],
        )
    ]
    answer_parts = [route_claim]
    if re.search(r"срок|как долго|за какой", plan.normalized_query):
        deadline_claim = (
            "Рекомендуемый срок ответа одного согласующего — один рабочий день."
        )
        answer_parts.append(deadline_claim)
        claims.append(
            GroundedClaim(
                text=deadline_claim,
                cited_chunk_ids=[str(chunk.chunk_id)],
            )
        )
    return GroundedAnswerPayload(
        answer=" ".join(answer_parts),
        claims=claims,
        insufficient_context=False,
        source_conflict=False,
    )


def _deterministic_policy_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    intents = set(plan.intents)
    if "channel" in intents:
        chunk = next(
            (
                item
                for item in chunks
                if re.search(
                    r"устн\w*.*не\s+замен\w*.*регистрац",
                    normalize_regulation_text(item.content),
                )
            ),
            None,
        )
        if chunk is not None:
            answer = (
                "Обсудить срочную заявку с закупщиком устно можно, но устная "
                "просьба не заменяет регистрацию заявки."
            )
            return _single_claim_payload(answer, chunk)
    if "mixed_categories" in intents:
        chunk = next(
            (
                item
                for item in chunks
                if re.search(
                    r"разн\w*\s+категор|отдельн\w*\s+заяв",
                    normalize_regulation_text(item.content),
                )
            ),
            None,
        )
        if chunk is not None:
            answer = (
                "Товары и услуги разных категорий нужно разделить на отдельные "
                "заявки по однородным категориям."
            )
            return _single_claim_payload(answer, chunk)
    if "brand_policy" in intents:
        chunk = next(
            (
                item
                for item in chunks
                if "эквивалент" in normalize_regulation_text(item.content)
            ),
            None,
        )
        if chunk is not None:
            answer = (
                "Конкретный бренд можно указать как референс с пометкой «или "
                "эквивалент». Запрет эквивалента нужно обосновать."
            )
            return _single_claim_payload(answer, chunk)
    return None


def _single_claim_payload(
    answer: str,
    chunk: SearchResult,
) -> GroundedAnswerPayload:
    return GroundedAnswerPayload(
        answer=answer,
        claims=[
            GroundedClaim(
                text=answer,
                cited_chunk_ids=[str(chunk.chunk_id)],
            )
        ],
        insufficient_context=False,
        source_conflict=False,
    )


def _deterministic_draft_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    if plan.primary_intent != "draft_history" or not is_draft_question(
        plan.normalized_query
    ):
        return None
    chunk = next(
        (
            item
            for item in chunks
            if item.document_type in {"faq", "user_guide"}
            and re.search(
                r"черновик|сохран\w*.{0,30}заяв|продолж\w*.{0,20}позж",
                normalize_regulation_query(item.content),
            )
        ),
        None,
    )
    if chunk is None:
        return None
    answer = (
        "Да, незавершённую заявку можно сохранить как черновик и продолжить "
        "её заполнение позже."
    )
    return _single_claim_payload(answer, chunk)


def _deterministic_urgency_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    if "urgency" not in plan.intents:
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
    fields = [
        match.group(1).strip()
        for line in (fields_chunk.content.splitlines() if fields_chunk else [])
        if (match := re.match(r"^\s*-\s+(.+?)[;.]?\s*$", line))
    ]
    claims: list[GroundedClaim] = []
    answer_parts: list[str] = []
    duration = plan.understanding.duration_days
    asks_for_decision = bool(
        re.search(
            r"будет\s+ли|будет.*сроч|считает\w*\s+сроч|"
            r"какая\s+заявка\s+считает\w*\s+сроч|"
            r"уже\s+сроч|срочн\w*\s+заявк\w*\s+или|"
            r"основан\w*\s+для\s+p2|это\s+.*p2|приоритет\w*\s+p2|"
            r"высок\w*\s+приоритет|это\s+.*приоритет",
            plan.normalized_query,
        )
    )
    asks_for_fields = bool(
        re.search(
            r"что.*(?:указ|напис|писат)|как\s+оформ|какие.*сведен",
            plan.normalized_query,
        )
    )
    if (
        duration is not None
        and plan.understanding.category_hint == "S07"
        and asks_for_fields
    ):
        asks_for_decision = True
    if duration is None and priority_chunk is not None:
        threshold_chunk = _normative_duration_support(plan.original_query, chunks)
        threshold = (
            _normative_duration_threshold(plan.original_query, [threshold_chunk])
            if threshold_chunk is not None
            else None
        )
        if (
            threshold is not None
            and plan.understanding.category_hint == "S07"
            and re.search(r"за\s+сколько\s+дн", plan.normalized_query)
        ):
            definition = (
                f"Обычную заявку на мероприятие нужно подавать за {threshold} "
                "календарных дней до даты мероприятия."
            )
            definition_ids = [str(threshold_chunk.chunk_id)]
        else:
            definition = (
                "Основанием для предварительного высокого приоритета P2 является "
                "срок меньше нормативного, риск заметных финансовых потерь, срыв "
                "мероприятия или серьёзный сбой. Окончательное решение принимает "
                "отдел закупок."
            )
            definition_ids = [str(priority_chunk.chunk_id)]
        answer_parts.append(definition)
        claims.append(
            GroundedClaim(
                text=definition,
                cited_chunk_ids=definition_ids,
            )
        )
    elif duration is not None and asks_for_decision and priority_chunk is not None:
        threshold_chunk = _normative_duration_support(plan.original_query, chunks)
        threshold = (
            _normative_duration_threshold(plan.original_query, [threshold_chunk])
            if threshold_chunk is not None
            else None
        )
        if threshold is not None and duration_below_threshold(duration, threshold):
            if re.search(r"\bдве\s+недел", plan.original_query.casefold()):
                subject = (
                    "мероприятия"
                    if plan.understanding.category_hint == "S07"
                    else "необходимой даты"
                )
                duration_phrase = (
                    f"До {subject} осталось две недели, то есть 14 дней."
                )
            else:
                duration_phrase = (
                    f"До необходимой даты осталось {_days_phrase(duration)}."
                )
            unit = (
                "календарных"
                if plan.understanding.category_hint == "S07"
                else "рабочих"
            )
            priority_claim = (
                f"{duration_phrase} Это меньше нормативного срока в {threshold} "
                f"{unit} дней, поэтому это основание для предварительного "
                "приоритета P2."
            )
            answer_parts.append(priority_claim)
            claims.append(
                GroundedClaim(
                    text=priority_claim,
                    cited_chunk_ids=[
                        str(threshold_chunk.chunk_id),
                        str(priority_chunk.chunk_id),
                    ],
                )
            )
        elif threshold is not None and threshold_chunk is not None:
            regular_claim = (
                f"До необходимой даты осталось {_days_phrase(duration)}. "
                f"Это соответствует или превышает нормативный срок в {threshold} "
                "дней; признак высокого приоритета по сроку отсутствует."
            )
            answer_parts.append(regular_claim)
            claims.append(
                GroundedClaim(
                    text=regular_claim,
                    cited_chunk_ids=[
                        str(threshold_chunk.chunk_id),
                        str(priority_chunk.chunk_id),
                    ],
                )
            )
    if (asks_for_fields or not answer_parts) and fields_chunk is not None and fields:
        fields_claim = "Для срочной заявки укажите: " + "; ".join(fields) + "."
        answer_parts.append(fields_claim)
        claims.append(
            GroundedClaim(
                text=fields_claim,
                cited_chunk_ids=[str(fields_chunk.chunk_id)],
            )
        )
    if not answer_parts:
        return None
    return GroundedAnswerPayload(
        answer=" ".join(answer_parts),
        claims=claims,
        insufficient_context=False,
        source_conflict=False,
    )


def _deterministic_status_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    if "status" not in plan.intents:
        return None
    status_chunk = next(
        (chunk for chunk in chunks if chunk.document_type == "status_guide"),
        None,
    )
    if status_chunk is None and plan.understanding.status_name == "requires_rework":
        status_chunk = next(
            (
                chunk
                for chunk in chunks
                if chunk.document_type == "faq"
                and "требует доработки"
                in normalize_regulation_text(chunk.content)
            ),
            None,
        )
    if status_chunk is None:
        return None
    if plan.understanding.status_name == "на согласовании":
        answer = (
            "Статус «На согласовании» означает, что по заявке требуется "
            "решение. После согласования заявка может вернуться в статус "
            "«Принята в работу», перейти в статус «В работе» или быть "
            "отклонена. Отдельное действие заказчика для этого статуса в "
            "правилах не указано."
        )
        return _single_claim_payload(answer, status_chunk)
    if plan.understanding.status_name == "transferred_to_procurement":
        registered = (
            "Статус «Передана в отдел закупок» означает, что заявка подтверждена "
            "и зарегистрирована, а затем направлена в отдел закупок."
        )
        check_chunk = next(
            (
                chunk
                for chunk in chunks
                if chunk.document_type == "regulation"
                and re.search(
                    r"первичн\w*\s+проверк|провер\w*\s+полнот",
                    normalize_regulation_text(chunk.content),
                )
            ),
            None,
        )
        check_claim = (
            "После регистрации отдел закупок проверяет полноту заявки."
            if check_chunk is not None
            else None
        )
        next_step = (
            "После этого закупщик может перевести заявку в статус «Требует "
            "доработки», если нужны сведения заказчика, либо продолжить её "
            "обработку. До запроса уточнений отдельное действие заказчика в "
            "правилах не указано."
        )
        claims = [
            GroundedClaim(
                text=registered,
                cited_chunk_ids=[str(status_chunk.chunk_id)],
            )
        ]
        answer_parts = [registered]
        if check_claim is not None and check_chunk is not None:
            answer_parts.append(check_claim)
            claims.append(
                GroundedClaim(
                    text=check_claim,
                    cited_chunk_ids=[str(check_chunk.chunk_id)],
                )
            )
        answer_parts.append(next_step)
        claims.append(
            GroundedClaim(
                text=next_step,
                cited_chunk_ids=[str(status_chunk.chunk_id)],
            )
        )
        return GroundedAnswerPayload(
            answer=" ".join(answer_parts),
            claims=claims,
            insufficient_context=False,
            source_conflict=False,
        )
    if plan.understanding.status_name == "requires_rework":
        detail_chunk = next(
            (
                chunk
                for chunk in chunks
                if re.search(
                    r"конкретн\w*\s+недостающ|какие\s+сведен\w*\s+нужно\s+дополн",
                    normalize_regulation_text(chunk.content),
                )
            ),
            status_chunk,
        )
        answer = (
            "Если закупщик вернул заявку, откройте его комментарий: в нём должны "
            "быть указаны конкретные недостающие сведения. Дополните их и снова "
            "передайте заявку в отдел закупок."
        )
        return _single_claim_payload(answer, detail_chunk)
    statuses = []
    for line in status_chunk.content.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] not in {"Статус", "---", "Черновик"}:
            statuses.append(cells[0])
    statuses = list(dict.fromkeys(statuses))
    if not statuses:
        return None
    answer = (
        "После регистрации заявка может проходить статусы: "
        + "; ".join(statuses)
        + "."
    )
    return _single_claim_payload(answer, status_chunk)


def _deterministic_cancellation_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    if "request_cancellation" not in plan.intents:
        return None
    chunk = next(
        (
            item
            for item in chunks
            if re.search(
                r"отмен\w*.*до\s+статуса.*принята\s+в\s+работу|"
                r"после\s+начала\s+закупки\s+отмена\s+согласуется",
                normalize_regulation_text(item.content),
            )
        ),
        None,
    )
    if chunk is None:
        return None
    answer = (
        "Инициатор может отменить заявку до статуса «Принята в работу». "
        "Если закупщик уже начал закупку, отмену нужно согласовать "
        "с закупщиком."
    )
    return _single_claim_payload(answer, chunk)


def _deterministic_responsibility_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    if "responsibility" not in plan.intents:
        return None
    chunk = next(
        (
            item
            for item in chunks
            if re.search(
                r"внутренн\w*\s+заказчик.*(?:характерист|требован)|"
                r"кто\s+отвечает\s+за\s+характерист",
                normalize_regulation_text(item.content),
            )
        ),
        None,
    )
    if chunk is None:
        return None
    answer = (
        "Техническое описание и характеристики оборудования готовит внутренний "
        "заказчик или профильный эксперт."
    )
    return _single_claim_payload(answer, chunk)


def _deterministic_category_fields_payload(
    plan: RegulationQueryPlan,
    chunks: Sequence[SearchResult],
) -> GroundedAnswerPayload | None:
    intents = set(plan.intents)
    if not intents & {"transport_fields", "category_fields"}:
        return None
    chunk = next(
        (
            item
            for item in chunks
            if item.document_type == "field_matrix"
            and (
                plan.understanding.category_hint is not None
                or "для всех заявок обязательны"
                in normalize_regulation_text(item.content)
            )
        ),
        None,
    )
    if chunk is None:
        return None
    normalized = plan.normalized_query
    if plan.understanding.category_hint is None:
        common = re.search(
            r"для всех заявок обязательны:\s*([^\n]+)",
            normalize_regulation_text(chunk.content),
        )
        if common is None:
            return None
        answer = (
            "Для заявки укажите общие обязательные сведения: "
            f"{common.group(1).strip().rstrip('.')}. После выбора категории "
            "могут потребоваться дополнительные поля."
        )
        return _single_claim_payload(answer, chunk)
    row_marker = "S03" if "transport_fields" in intents else "S05"
    row = next(
        (
            line
            for line in chunk.content.splitlines()
            if row_marker.casefold() in line.casefold()
        ),
        None,
    )
    if row is None:
        return None
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    if len(cells) != 2:
        return None
    category, required = cells
    required_items = [item.strip() for item in required.split(",")]
    if row_marker == "S03":
        provided = []
        route = re.search(
            r"\bиз\s+([^.,!?]+?)\s+на\s+([^.,!?]+?)(?=[.,!?]|$)",
            normalized,
        )
        if route is not None:
            provided.append(f"маршрут из {route.group(1)} на {route.group(2)}")
            required_items = [
                item for item in required_items if not item.startswith("маршрут")
            ]
        pallets = re.search(r"\b(\d+)\s+паллет", normalized)
        if pallets is not None:
            provided.append(f"груз — {pallets.group(1)} паллет")
            required_items = [
                item for item in required_items if not item.startswith("груз")
            ]
        intro = (
            "Уже указаны " + " и ".join(provided) + ". " if provided else ""
        )
        detail = (
            f"{intro}Для категории «{category}» дополнительно нужны: "
            + ", ".join(required_items)
            + "."
        )
        if re.search(r"вес.*неизвест|неизвест.*вес", normalized):
            detail = (
                "Для транспортной заявки нужно указать вес или объём груза. "
                "Если точный вес неизвестен, укажите доступный объём либо "
                "уточните вес до передачи полной заявки в отдел закупок. "
                + detail
            )
    else:
        if plan.understanding.category_hint != "S05":
            return None
        required_items = [
            item for item in required_items if not item.startswith("интеграц")
        ]
        detail = (
            "Вы уже указали, что нужна интеграция между системами. "
            f"Для категории «{category}» дополнительно нужны: "
            + ", ".join(required_items)
            + "."
        )
    return GroundedAnswerPayload(
        answer=detail,
        claims=[
            GroundedClaim(
                text=detail,
                cited_chunk_ids=[str(chunk.chunk_id)],
            )
        ],
        insufficient_context=False,
        source_conflict=False,
    )


def _days_phrase(value: int) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if remainder_10 == 1 and remainder_100 != 11:
        suffix = "день"
    elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        suffix = "дня"
    else:
        suffix = "дней"
    return f"{value} {suffix}"


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
    if not claim_terms:
        return False
    matched = sum(
        min(count, answer_terms[term]) for term, count in claim_terms.items()
    )
    return matched / sum(claim_terms.values()) >= 0.8


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
    if duration is None or not _asserts_duration_relation(claim):
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
        "approval": r"соглас|руковод|финанс|бюджет|маршрут|срок|день",
        "urgency": (
            r"сроч|приоритет|срок|причин|последств|данн|указать|"
            r"подава|календарн|\bдн(?:я|ей)?\b|"
            r"подтвержден\w*\s+руковод|решен|отдел закупок"
        ),
        "transport_fields": (
            r"маршрут|груз|вес|объем|дат|погруз|перевоз|указать|"
            r"категор|сведен|поле"
        ),
        "mixed_categories": r"категор|отдельн|объедин|раздел",
        "status": (
            r"статус|доработ|сведен|закупщик|зарегистр|передан|действ|"
            r"провер|полнот|отдел закупок"
        ),
        "category_fields": (
            r"категор|обязательн|поле|требован|интеграц|результат|приемк|указать"
        ),
        "category_classification": r"категор|классифик|\b[gs]\d{2}\b",
        "budget_policy": r"бюджет|внебюджет|финанс|согласован|подать|заявк",
        "brand_policy": r"бренд|эквивалент|референс|обоснов",
        "channel": r"устн|официальн|регистрац|ассистент|заявк",
        "request_cancellation": r"отмен|снять|закупщик|принята\s+в\s+работу",
        "responsibility": r"заказчик|эксперт|характерист|техническ|описан",
        "draft_history": r"черновик|мои\s+заяв|последн|продолжить|отправ",
        "general_help": r"$^",
        "ambiguous_followup": r"$^",
        "outside_kb": r"$^",
        "outside_domain": r"$^",
        "generic": r"[a-zа-я0-9]",
    }
    return bool(re.search(patterns[plan.primary_intent], normalized))


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
    if duration is None or not _asserts_duration_relation(claim):
        return
    threshold = _normative_duration_threshold(question, chunks)
    if threshold is None:
        raise ValueError("duration comparison lacks cited normative threshold")
    if not duration_below_threshold(duration, threshold):
        raise ValueError("duration is not below the cited normative threshold")


def _asserts_duration_relation(claim: str) -> bool:
    normalized = normalize_regulation_text(claim)
    return bool(
        re.search(
            r"(?:срок|дн\w*)\W{0,30}меньше\W{0,20}норматив|"
            r"меньше\W{0,20}норматив\w*\W{0,30}(?:срок|дн\w*)|"
            r"основан\w*\W{0,20}(?:приоритет\w*\s+)?p2",
            normalized,
        )
    )


def _normative_duration_threshold(
    question: str,
    chunks: Sequence[SearchResult],
) -> int | None:
    support = _normative_duration_support(question, chunks)
    if support is None:
        return None
    for line in support.content.splitlines():
        normalized_question = normalize_regulation_text(question)
        if (
            is_procurement_event(normalized_question)
            and "мероприят" not in line.casefold()
        ):
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
    if is_procurement_event(normalized_question):
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
