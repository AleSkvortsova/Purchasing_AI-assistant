from pathlib import Path
from typing import Protocol

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ContentFilterFinishReasonError,
    InternalServerError,
    LengthFinishReasonError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError

from app.extraction.exceptions import (
    ApprovalExtractionConfigurationError,
    ApprovalExtractionProviderError,
)
from app.extraction.models import RawApprovalExtraction
from app.extraction.normalization import (
    MultipleMoneyRangesError,
    compact_category_reference,
    evidence_is_present,
    fact_requires_evidence,
    match_category,
    normalize_budget_status,
    normalize_money,
    normalize_search_text,
    normalize_urgency,
)
from app.extraction.openai_schema import OpenAIApprovalExtractionPayload


class ApprovalExtractionProvider(Protocol):
    def extract(self, text: str) -> RawApprovalExtraction: ...


class OpenAIApprovalExtractionProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 30,
        max_retries: int = 2,
        client: OpenAI | None = None,
    ) -> None:
        if not api_key:
            raise ApprovalExtractionConfigurationError(
                "OPENAI_API_KEY is not configured for approval extraction"
            )
        if not model or not model.strip():
            raise ApprovalExtractionConfigurationError(
                "APPROVAL_EXTRACTION_MODEL is not configured"
            )
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.model = model.strip()
        self.max_retries = max_retries
        self._client = client or OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self._timeout_seconds = timeout_seconds
        self.last_metadata: dict = {}

    def extract(self, text: str) -> RawApprovalExtraction:
        prompt = _load_prompt()
        instructions = (
            f"{prompt}\n\nКомпактный справочник категорий:\n"
            f"{compact_category_reference()}"
        )
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.responses.parse(
                    model=self.model,
                    instructions=instructions,
                    input=text,
                    text_format=OpenAIApprovalExtractionPayload,
                    store=False,
                    timeout=self._timeout_seconds,
                )
                self.last_metadata = {
                    "provider": "openai",
                    "model": self.model,
                    **_safe_response_metadata(response),
                }
                refusal = self.last_metadata["has_refusal"]
                if refusal:
                    raise _diagnostic_error(
                        "OpenAI refused to process the extraction",
                        response=response,
                        error_type="OpenAIRefusalError",
                    )
                response_status = getattr(response, "status", None)
                if response_status == "incomplete":
                    raise _diagnostic_error(
                        "OpenAI returned an incomplete response",
                        response=response,
                        error_type="OpenAIIncompleteResponseError",
                    )
                if response_status == "failed":
                    raise _diagnostic_error(
                        "OpenAI response failed",
                        response=response,
                        error_type="OpenAIFailedResponseError",
                    )
                payload = response.output_parsed
                if payload is None:
                    raise _diagnostic_error(
                        "OpenAI completed without parsed structured output",
                        response=response,
                        error_type="OpenAIUnparsedResponseError",
                    )
                parsed = payload.to_raw_extraction()
                invalid_evidence = _invalid_evidence_fields(text, parsed)
                if invalid_evidence:
                    raise _diagnostic_error(
                        "OpenAI structured output failed evidence validation",
                        response=response,
                        error_type="ApprovalEvidenceValidationError",
                        validation_errors=[
                            f"{field}: evidence not present in input"
                            for field in invalid_evidence
                        ],
                    )
                return parsed
            except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
                if attempt >= self.max_retries:
                    if isinstance(exc, RateLimitError):
                        message = "OpenAI rate limit was exceeded"
                    elif isinstance(exc, APITimeoutError):
                        message = "OpenAI request timed out"
                    else:
                        message = "OpenAI network connection failed"
                    raise _diagnostic_error(message, exc=exc) from exc
            except InternalServerError as exc:
                if attempt >= self.max_retries:
                    raise _diagnostic_error(
                        "OpenAI API is temporarily unavailable",
                        exc=exc,
                    ) from exc
            except AuthenticationError as exc:
                raise _diagnostic_error(
                    "OpenAI authentication failed",
                    exc=exc,
                ) from exc
            except PermissionDeniedError as exc:
                raise _diagnostic_error(
                    "OpenAI permission was denied",
                    exc=exc,
                ) from exc
            except NotFoundError as exc:
                raise _diagnostic_error(
                    "OpenAI model is invalid or unavailable",
                    exc=exc,
                ) from exc
            except BadRequestError as exc:
                if _is_model_error(exc):
                    raise _diagnostic_error(
                        "OpenAI model is invalid or unavailable",
                        exc=exc,
                    ) from exc
                raise _diagnostic_error(
                    "OpenAI rejected the extraction request",
                    exc=exc,
                ) from exc
            except ContentFilterFinishReasonError as exc:
                raise _diagnostic_error(
                    "OpenAI refused to process the extraction",
                    exc=exc,
                ) from exc
            except LengthFinishReasonError as exc:
                raise _diagnostic_error(
                    "OpenAI returned an incomplete response",
                    exc=exc,
                    incomplete_reason="max_output_tokens",
                ) from exc
            except ValidationError as exc:
                fields = sorted(
                    {
                        ".".join(str(part) for part in error["loc"])
                        for error in exc.errors()
                    }
                )
                raise _diagnostic_error(
                    "OpenAI structured output failed schema validation",
                    exc=exc,
                    validation_errors=fields or ["unknown"],
                ) from exc
            except ApprovalExtractionProviderError:
                raise
            except (APIStatusError, APIError) as exc:
                raise _diagnostic_error(
                    "OpenAI API request failed",
                    exc=exc,
                ) from exc
            except Exception as exc:
                raise _diagnostic_error(
                    "OpenAI response could not be processed",
                    exc=exc,
                ) from exc
        raise ApprovalExtractionProviderError(
            "OpenAI approval extraction failed"
        )


def _diagnostic_error(
    message: str,
    *,
    exc: Exception | None = None,
    response: object | None = None,
    error_type: str | None = None,
    incomplete_reason: str | None = None,
    validation_errors: list[str] | None = None,
) -> ApprovalExtractionProviderError:
    source = (
        response
        if response is not None
        else getattr(exc, "response", None)
    )
    status = getattr(exc, "status_code", None) or getattr(
        source,
        "status_code",
        None,
    )
    response_status = getattr(source, "status", None)
    code = (
        getattr(exc, "code", None)
        or _error_body_value(exc, "code")
        or _response_error_value(source, "code")
    )
    param = (
        getattr(exc, "param", None)
        or _error_body_value(exc, "param")
        or _response_error_value(source, "param")
    )
    request_id = getattr(exc, "request_id", None) or getattr(
        source,
        "_request_id",
        None,
    )
    headers = getattr(source, "headers", None)
    if request_id is None and headers is not None:
        request_id = headers.get("x-request-id")
    reason = incomplete_reason or _incomplete_reason(source)
    return ApprovalExtractionProviderError(
        safe_message=message,
        error_type=error_type or (
            type(exc).__name__ if exc is not None else None
        ),
        status_code=_safe_int(status),
        error_code=_safe_optional_string(code),
        error_param=_safe_optional_string(param),
        request_id=_safe_optional_string(request_id),
        response_status=_safe_optional_string(response_status),
        incomplete_reason=_safe_optional_string(reason),
        validation_errors=validation_errors,
    )


def _error_body_value(exc: Exception | None, key: str) -> object | None:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict) and error.get(key) is not None:
        return error[key]
    return body.get(key)


def _safe_diagnostic_value(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:200]


def _safe_optional_string(value: object | None) -> str | None:
    return None if value is None else _safe_diagnostic_value(value)


def _safe_int(value: object | None) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _response_error_value(response: object | None, key: str) -> object | None:
    error = getattr(response, "error", None)
    if isinstance(error, dict):
        return error.get(key)
    return getattr(error, key, None)


def _safe_response_metadata(response: object) -> dict:
    output = getattr(response, "output", ()) or ()
    output_item_types = [
        _safe_diagnostic_value(_item_type(item) or type(item).__name__)
        for item in output
    ]
    response_error = getattr(response, "error", None)
    return {
        "response_id": _safe_optional_string(getattr(response, "id", None)),
        "response_status": _safe_optional_string(
            getattr(response, "status", None)
        ),
        "incomplete_details": {
            "reason": _safe_optional_string(_incomplete_reason(response))
        },
        "response_error": (
            {
                "code": _safe_optional_string(
                    _response_error_value(response, "code")
                ),
                "type": _safe_optional_string(
                    _response_error_value(response, "type")
                ),
            }
            if response_error is not None
            else None
        ),
        "output_item_types": output_item_types,
        "has_refusal": _response_has_refusal(response),
        "has_output_text": bool(getattr(response, "output_text", None)),
    }


def _is_model_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or _error_body_value(exc, "code")
    param = getattr(exc, "param", None) or _error_body_value(exc, "param")
    return param == "model" or code in {
        "invalid_model",
        "model_not_found",
        "unsupported_model",
    }


def _response_has_refusal(response: object) -> bool:
    for item in getattr(response, "output", ()) or ():
        if _item_type(item) == "refusal":
            return True
        for content in getattr(item, "content", ()) or ():
            if _item_type(content) == "refusal":
                return True
    return False


def _item_type(item: object) -> object | None:
    if isinstance(item, dict):
        return item.get("type")
    return getattr(item, "type", None)


def _incomplete_reason(response: object) -> object | None:
    details = getattr(response, "incomplete_details", None)
    if isinstance(details, dict):
        return details.get("reason")
    return getattr(details, "reason", None)


def _invalid_evidence_fields(
    source_text: str,
    parsed: RawApprovalExtraction,
) -> list[str]:
    facts = {
        "amount": parsed.amount_raw,
        "budget_status": parsed.budget_status_raw,
        "urgency": parsed.urgency_raw
        or ("claimed" if parsed.urgency_claimed else None),
        "single_supplier": parsed.single_supplier_raw,
        "category": parsed.category_raw,
        "has_data_access": parsed.has_data_access_raw,
        "work_on_site": parsed.work_on_site_raw,
    }
    invalid: set[str] = set()
    for field, evidence in parsed.evidence_by_field.items():
        if not evidence or not evidence_is_present(source_text, evidence):
            invalid.add(field)
    for field, value in facts.items():
        evidence = parsed.evidence_by_field.get(field)
        if fact_requires_evidence(field, value) and (
            not evidence or not evidence_is_present(source_text, evidence)
        ):
            invalid.add(field)
    return sorted(invalid)


class FakeApprovalExtractionProvider:
    def __init__(
        self,
        result: RawApprovalExtraction | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or RawApprovalExtraction()
        self.error = error
        self.calls = 0
        self.last_metadata = {"provider": "fake"}

    def extract(self, text: str) -> RawApprovalExtraction:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result.model_copy(deep=True)


class RuleBasedApprovalExtractionProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.last_metadata = {"provider": "rule_based"}

    def extract(self, text: str) -> RawApprovalExtraction:
        self.calls += 1
        normalized = normalize_search_text(text)
        evidence: dict[str, str] = {}
        confidence: dict[str, float] = {}

        amount_raw = None
        amount_conflicts: list[str] = []
        try:
            money = normalize_money(text)
            amount_raw = text
            evidence["amount"] = money.evidence
            confidence["amount"] = 1.0
        except MultipleMoneyRangesError as exc:
            amount_conflicts.append(str(exc))
        except ValueError:
            pass

        budget_status, budget_conflicts = normalize_budget_status(text)
        if budget_status:
            budget_evidence = _budget_evidence(normalized, budget_status)
            evidence["budget_status"] = budget_evidence
            confidence["budget_status"] = 1.0

        urgency, urgency_claimed, _ = normalize_urgency(text)
        if urgency:
            evidence["urgency"] = urgency
            confidence["urgency"] = 1.0
        elif urgency_claimed:
            evidence["urgency"] = _first_match(
                normalized,
                (
                    r"очень\s+срочно",
                    r"как\s+можно\s+скорее",
                    r"нужно\s+вчера",
                    r"срочн\w*",
                    r"горит",
                ),
            )
            confidence["urgency"] = 1.0

        single_supplier = bool(
            _first_match(
                normalized,
                (
                    r"единственн\w+\s+поставщик",
                    r"поставщик\w*\s+единственн\w*",
                    r"только\s+этот\s+поставщик",
                    r"без\s+альтернатив",
                    r"конкретн\w+\s+поставщик\w*\s+без\s+конкурс",
                ),
            )
        )
        if single_supplier:
            evidence["single_supplier"] = _first_match(
                normalized,
                (
                    r"единственн\w+\s+поставщик",
                    r"поставщик\w*\s+единственн\w*",
                    r"только\s+этот\s+поставщик",
                    r"без\s+альтернатив",
                    r"конкретн\w+\s+поставщик\w*\s+без\s+конкурс\w*",
                ),
            )
            confidence["single_supplier"] = 1.0

        category_code, category_warnings = match_category(text)
        if category_code:
            category_evidence = _category_evidence(normalized, category_code)
            evidence["category"] = category_evidence
            confidence["category"] = 0.9

        data_access_evidence = _first_match(
            normalized,
            (
                r"доступ\w*\s+к\s+персональн\w+\s+данн",
                r"доступ\w*\s+к\s+корпоративн\w+\s+систем",
                r"доступ\w*\s+к\s+баз\w+\s+данн",
                r"доступ\w*\s+к\s+данн",
                r"конфиденциальн\w+\s+информац",
                r"интеграц\w+\s+с\s+данн\w+\s+компани",
            ),
        )
        if data_access_evidence:
            evidence["has_data_access"] = data_access_evidence
            confidence["has_data_access"] = 1.0

        site_evidence = _first_match(
            normalized,
            (
                r"работ\w*\s+в\s+офис",
                r"монтаж\w*\s+на\s+склад",
                r"выезд\w*\s+на\s+объект",
                r"на\s+территори\w+\s+компани",
                r"на\s+площадк",
            ),
        )
        if site_evidence:
            evidence["work_on_site"] = site_evidence
            confidence["work_on_site"] = 1.0

        unknown_fields: list[str] = []
        if category_code == "S05" and not data_access_evidence:
            unknown_fields.append("has_data_access")
        if category_code in {"S01", "S02"} and not site_evidence:
            unknown_fields.append("work_on_site")
        if category_warnings:
            unknown_fields.append("category_code")
        elif (
            category_code is None
            and __import__("re").search(r"\bкатегори\w+\s+\S+", normalized)
        ):
            unknown_fields.append("category_code")

        return RawApprovalExtraction(
            amount_raw=amount_raw,
            budget_status_raw=budget_status,
            urgency_raw=urgency,
            single_supplier_raw=True if single_supplier else None,
            category_raw=category_code,
            has_data_access_raw=True if data_access_evidence else None,
            work_on_site_raw=True if site_evidence else None,
            urgency_claimed=urgency_claimed,
            confidence_by_field=confidence,
            evidence_by_field=evidence,
            unknown_fields=unknown_fields,
            contradictions=[*budget_conflicts, *amount_conflicts],
        )


def _load_prompt() -> str:
    return (
        Path(__file__).resolve().parent
        / "prompts"
        / "approval_context_extraction.md"
    ).read_text(encoding="utf-8")


def _first_match(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = __import__("re").search(pattern, text)
        if match:
            return match.group(0)
    return ""


def _budget_evidence(text: str, status: str) -> str:
    patterns = (
        (
            r"вне\s*бюджет\w*",
            r"внебюджет\w*",
            r"не\s+предусмотр\w*\s+бюджет\w*",
            r"бюджета\s+нет",
        )
        if status == "unbudgeted"
        else (
            r"закупк\w*\s+бюджетн\w*",
            r"предусмотр\w*\s+бюджет\w*",
            r"в\s+бюджете",
            r"по\s+утвержденн\w+\s+статье",
        )
    )
    return _first_match(text, patterns)


def _category_evidence(text: str, code: str) -> str:
    from app.extraction.normalization import CATEGORY_ALIASES

    return next(
        (alias for alias in CATEGORY_ALIASES[code] if alias in text),
        code,
    )
