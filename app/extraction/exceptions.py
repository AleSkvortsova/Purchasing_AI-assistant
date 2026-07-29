class ApprovalExtractionError(Exception):
    """Base extraction-layer error."""


class ApprovalExtractionConfigurationError(ApprovalExtractionError):
    """Raised when the selected provider is not configured."""


class ApprovalExtractionProviderError(ApprovalExtractionError):
    """Raised when a provider cannot return a valid extraction."""

    def __init__(
        self,
        safe_message: str,
        *,
        error_type: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
        error_param: str | None = None,
        request_id: str | None = None,
        response_status: str | None = None,
        incomplete_reason: str | None = None,
        validation_errors: list[str] | None = None,
        diagnostic_code: str | None = None,
        validation_error_codes: dict[str, str] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.error_type = error_type
        self.safe_message = safe_message
        self.status_code = status_code
        self.error_code = error_code
        self.error_param = error_param
        self.request_id = request_id
        self.response_status = response_status
        self.incomplete_reason = incomplete_reason
        self.validation_errors = validation_errors
        self.diagnostic_code = diagnostic_code
        self.validation_error_codes = validation_error_codes


class ApprovalExtractionValidationError(ApprovalExtractionError):
    """Raised when extraction input is invalid."""
