import re
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.rules.exceptions import ApprovalRuleRepositoryError
from app.rules.repository import SupabaseApprovalRuleRepository


@pytest.mark.parametrize(
    ("url", "service_role_key"),
    [
        ("https://example.supabase.co", ""),
        ("", "test-service-role-key"),
    ],
)
def test_repository_requires_complete_server_credentials(
    url: str,
    service_role_key: str,
) -> None:
    with pytest.raises(
        ApprovalRuleRepositoryError,
        match="Supabase URL and service role key are required",
    ):
        SupabaseApprovalRuleRepository.from_credentials(
            url,
            service_role_key,
        )


@pytest.mark.parametrize(
    ("backend_error", "expected_message"),
    [
        (
            "PGRST205: table was not found in the schema cache",
            "Approval rule tables are unavailable; apply migration 005",
        ),
        (
            "42501: permission denied for table approval_base_rules",
            "Permission denied while reading approval rules",
        ),
        (
            "Network connection timed out while contacting backend",
            "Network error while reading approval rules",
        ),
    ],
)
def test_repository_reports_understandable_backend_errors(
    backend_error: str,
    expected_message: str,
) -> None:
    client = MagicMock()
    client.table.return_value.select.return_value.execute.side_effect = (
        RuntimeError(backend_error)
    )
    repository = SupabaseApprovalRuleRepository(client)

    with pytest.raises(
        ApprovalRuleRepositoryError,
        match=re.escape(expected_message),
    ) as caught:
        repository.get_all_active_rules(date(2026, 7, 24))

    assert backend_error not in str(caught.value)
