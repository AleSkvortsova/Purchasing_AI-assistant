from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError

from app.extraction.exceptions import ApprovalExtractionProviderError
from app.extraction.models import RawApprovalExtraction
from app.extraction.openai_schema import OpenAIApprovalExtractionPayload
from app.extraction.provider import OpenAIApprovalExtractionProvider
from scripts import extract_approval_context


def _provider(*, error: Exception | None = None, response=None):
    client = Mock()
    if error is not None:
        client.responses.parse.side_effect = error
    else:
        client.responses.parse.return_value = response
    return OpenAIApprovalExtractionProvider(
        api_key="test-key",
        model="gpt-5.6-luna",
        max_retries=0,
        client=client,
    )


def _status_error(error_type, status: int, *, code: str, param: str):
    request = httpx.Request(
        "POST",
        "https://api.openai.com/v1/responses",
    )
    response = httpx.Response(
        status,
        request=request,
        headers={"x-request-id": "req_safe_test"},
    )
    return error_type(
        "sensitive server message with sk-secret Authorization header",
        response=response,
        body={"error": {"code": code, "param": param}},
    )


def _response(
    *,
    parsed=None,
    status="completed",
    output=None,
    reason=None,
    error=None,
    output_text="",
):
    return SimpleNamespace(
        id="resp_test",
        output_parsed=parsed,
        status=status,
        output=output or [],
        output_text=output_text,
        error=error,
        incomplete_details=(
            SimpleNamespace(reason=reason) if reason else None
        ),
    )


@pytest.mark.parametrize(
    ("error_type", "status", "code", "param", "message"),
    [
        (
            AuthenticationError,
            401,
            "invalid_api_key",
            "api_key",
            "authentication failed",
        ),
        (
            PermissionDeniedError,
            403,
            "permission_denied",
            "project",
            "permission was denied",
        ),
        (
            NotFoundError,
            404,
            "model_not_found",
            "model",
            "model is invalid or unavailable",
        ),
        (
            BadRequestError,
            400,
            "invalid_request_error",
            "text_format",
            "rejected the extraction request",
        ),
    ],
)
def test_non_retryable_api_errors_have_safe_diagnostics(
    error_type,
    status: int,
    code: str,
    param: str,
    message: str,
) -> None:
    error = _status_error(
        error_type,
        status,
        code=code,
        param=param,
    )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match=message,
    ) as captured:
        _provider(error=error).extract("user text")

    diagnostic = captured.value
    assert diagnostic.error_type == error_type.__name__
    assert diagnostic.status_code == status
    assert diagnostic.error_code == code
    assert diagnostic.error_param == param
    assert diagnostic.request_id == "req_safe_test"
    assert "sk-secret" not in diagnostic.safe_message
    assert "Authorization" not in diagnostic.safe_message


def test_rate_limit_error_is_distinct() -> None:
    error = _status_error(
        RateLimitError,
        429,
        code="rate_limit_exceeded",
        param="model",
    )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="rate limit",
    ) as captured:
        _provider(error=error).extract("user text")

    assert captured.value.status_code == 429
    assert captured.value.error_type == "RateLimitError"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            APITimeoutError(
                httpx.Request("POST", "https://api.openai.com/v1/responses")
            ),
            "timed out",
        ),
        (
            APIConnectionError(
                request=httpx.Request(
                    "POST",
                    "https://api.openai.com/v1/responses",
                )
            ),
            "network connection failed",
        ),
    ],
)
def test_timeout_and_network_errors_are_distinct(
    error: Exception,
    message: str,
) -> None:
    with pytest.raises(
        ApprovalExtractionProviderError,
        match=message,
    ) as captured:
        _provider(error=error).extract("user text")

    assert captured.value.error_type == type(error).__name__


def test_internal_server_error_is_distinct() -> None:
    error = _status_error(
        InternalServerError,
        500,
        code="server_error",
        param="responses",
    )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="temporarily unavailable",
    ) as captured:
        _provider(error=error).extract("user text")

    assert captured.value.error_type == "InternalServerError"
    assert captured.value.status_code == 500


def test_refusal_is_distinct_and_metadata_is_safe() -> None:
    response = _response(
        output=[
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        type="refusal",
                        refusal="sensitive refusal text",
                    )
                ],
            )
        ]
    )
    provider = _provider(response=response)

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="refused",
    ) as captured:
        provider.extract("secret user text")

    assert captured.value.error_type == "OpenAIRefusalError"
    assert captured.value.response_status == "completed"
    assert provider.last_metadata == {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "response_id": "resp_test",
        "response_status": "completed",
        "incomplete_details": {"reason": None},
        "response_error": None,
        "output_item_types": ["message"],
        "has_refusal": True,
        "has_output_text": False,
    }
    assert "sensitive refusal text" not in str(provider.last_metadata)
    assert "secret user text" not in str(provider.last_metadata)


def test_incomplete_response_reports_status_and_reason() -> None:
    response = _response(status="incomplete", reason="max_output_tokens")

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="incomplete response",
    ) as captured:
        _provider(response=response).extract("user text")

    diagnostic = captured.value
    assert diagnostic.error_type == "OpenAIIncompleteResponseError"
    assert diagnostic.response_status == "incomplete"
    assert diagnostic.incomplete_reason == "max_output_tokens"


def test_failed_response_reports_safe_response_error() -> None:
    response = _response(
        status="failed",
        error=SimpleNamespace(
            code="response_failed",
            type="server_error",
            message="secret response body",
        ),
    )
    provider = _provider(response=response)

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="response failed",
    ) as captured:
        provider.extract("user text")

    diagnostic = captured.value
    assert diagnostic.error_type == "OpenAIFailedResponseError"
    assert diagnostic.response_status == "failed"
    assert diagnostic.error_code == "response_failed"
    assert provider.last_metadata["response_error"] == {
        "code": "response_failed",
        "type": "server_error",
    }
    assert "secret response body" not in str(provider.last_metadata)


def test_completed_response_without_parsed_output_is_distinct() -> None:
    with pytest.raises(
        ApprovalExtractionProviderError,
        match="completed without parsed structured output",
    ) as captured:
        _provider(response=_response(output_text="unparsed text")).extract(
            "user text"
        )

    diagnostic = captured.value
    assert diagnostic.error_type == "OpenAIUnparsedResponseError"
    assert diagnostic.response_status == "completed"
    assert "unparsed text" not in diagnostic.safe_message


def test_pydantic_validation_error_lists_fields() -> None:
    with pytest.raises(ValidationError) as validation:
        RawApprovalExtraction.model_validate(
            {"confidence_by_field": {"amount": 2}}
        )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="schema validation",
    ) as captured:
        _provider(error=validation.value).extract("user text")

    diagnostic = captured.value
    assert diagnostic.error_type == "ValidationError"
    assert diagnostic.validation_errors == ["confidence_by_field"]


def test_invalid_evidence_reports_only_rejected_fields() -> None:
    parsed = OpenAIApprovalExtractionPayload(
        amount_raw="600 тысяч",
        budget_status_raw=None,
        urgency_raw=None,
        single_supplier_raw=None,
        category_raw=None,
        has_data_access_raw=None,
        work_on_site_raw=None,
        urgency_claimed=False,
        confidence_items=[],
        evidence_items=[
            {
                "field_name": "amount",
                "evidence": "secret invented evidence",
            }
        ],
        unknown_fields=[],
        contradictions=[],
    )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="evidence validation",
    ) as captured:
        _provider(response=_response(parsed=parsed)).extract(
            "Юридические услуги на 600 тысяч"
        )

    diagnostic = captured.value
    assert diagnostic.error_type == "ApprovalEvidenceValidationError"
    assert diagnostic.validation_errors == [
        "amount: evidence not present in input"
    ]
    assert "secret invented evidence" not in diagnostic.safe_message


def test_other_openai_api_error_is_distinct() -> None:
    error = _status_error(
        APIStatusError,
        503,
        code="server_error",
        param="responses",
    )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="API request failed",
    ) as captured:
        _provider(error=error).extract("user text")

    assert captured.value.error_type == "APIStatusError"
    assert captured.value.status_code == 503


def test_other_exception_exposes_only_its_type() -> None:
    error = ValueError(
        "secret response and Authorization: Bearer sk-secret"
    )

    with pytest.raises(
        ApprovalExtractionProviderError,
        match="response could not be processed",
    ) as captured:
        _provider(error=error).extract("user text")

    diagnostic = captured.value
    assert diagnostic.error_type == "ValueError"
    assert "secret" not in diagnostic.safe_message
    assert diagnostic.status_code is None


class _FailingService:
    def __init__(self, error: ApprovalExtractionProviderError) -> None:
        self.error = error

    def extract(self, text: str):
        raise self.error


@pytest.mark.parametrize("debug", [False, True])
def test_cli_debug_controls_safe_technical_fields(
    monkeypatch,
    capsys,
    debug: bool,
) -> None:
    error = ApprovalExtractionProviderError(
        "OpenAI authentication failed",
        error_type="AuthenticationError",
        status_code=401,
        error_code="invalid_api_key",
        error_param="api_key",
        request_id="req_safe_test",
        response_status=None,
        incomplete_reason=None,
        validation_errors=None,
    )
    monkeypatch.setattr(
        extract_approval_context,
        "build_extraction_service",
        lambda *args, **kwargs: _FailingService(error),
    )
    argv = ["secret user text", "--provider", "openai"]
    if debug:
        argv.append("--debug")

    exit_code = extract_approval_context.main(argv)
    stderr = capsys.readouterr().err

    assert exit_code == 1
    assert "ERROR: OpenAI authentication failed" in stderr
    assert ("error_type: AuthenticationError" in stderr) is debug
    assert ("status_code: 401" in stderr) is debug
    assert ("error_code: invalid_api_key" in stderr) is debug
    assert ("error_param: api_key" in stderr) is debug
    assert ("request_id: req_safe_test" in stderr) is debug
    assert ("response_status: null" in stderr) is debug
    assert ("incomplete_reason: null" in stderr) is debug
    assert ("validation_errors: null" in stderr) is debug
    assert "secret user text" not in stderr
    assert "Authorization" not in stderr
    assert "system prompt" not in stderr
