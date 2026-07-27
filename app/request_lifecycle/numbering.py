from datetime import datetime


def format_request_number(registered_at: datetime, sequence_value: int) -> str:
    """Format a global database sequence value without promising gaplessness."""
    if sequence_value < 1:
        raise ValueError("sequence_value must be positive")
    return f"PR-{registered_at.year}-{sequence_value:06d}"
