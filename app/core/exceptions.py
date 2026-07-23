class ApplicationError(Exception):
    """Base class for controlled application errors."""


class RequestNotFoundError(ApplicationError):
    """Raised when a procurement request does not exist."""


class DraftUpdateForbiddenError(ApplicationError):
    """Raised when an operation attempts to update a non-draft request."""


class RepositoryError(ApplicationError):
    """Raised when persistence operations fail."""
