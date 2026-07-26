class IntakeError(Exception):
    """Base error for deterministic intake processing."""


class IntakeValidationError(IntakeError):
    """Raised when an intake update cannot be validated."""
