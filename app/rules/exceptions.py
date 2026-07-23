class ApprovalRuleError(Exception):
    """Base error for approval rule evaluation."""


class ApprovalRuleRepositoryError(ApprovalRuleError):
    """Raised when approval rules cannot be read."""
