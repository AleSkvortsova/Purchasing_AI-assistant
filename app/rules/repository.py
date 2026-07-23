from datetime import date
from decimal import Decimal
from typing import Protocol

from supabase import Client, create_client

from app.rules.exceptions import ApprovalRuleRepositoryError
from app.rules.models import (
    ApprovalContext,
    ApprovalRule,
    ApprovalRuleStatistics,
    BudgetStatus,
)


class ApprovalRuleRepository(Protocol):
    def get_matching_base_rules(
        self,
        amount: Decimal,
        budget_status: BudgetStatus,
        evaluation_date: date,
    ) -> list[ApprovalRule]: ...

    def get_matching_additional_rules(
        self,
        context: ApprovalContext,
        evaluation_date: date,
    ) -> list[ApprovalRule]: ...

    def get_all_active_rules(
        self,
        evaluation_date: date,
    ) -> list[ApprovalRule]: ...

    def get_rule_statistics(self) -> ApprovalRuleStatistics: ...


class InMemoryApprovalRuleRepository:
    def __init__(
        self,
        base_rules: list[ApprovalRule] | None = None,
        additional_rules: list[ApprovalRule] | None = None,
    ) -> None:
        self._base_rules = list(base_rules or [])
        self._additional_rules = list(additional_rules or [])

    def get_matching_base_rules(
        self,
        amount: Decimal,
        budget_status: BudgetStatus,
        evaluation_date: date,
    ) -> list[ApprovalRule]:
        return sorted(
            [
                rule
                for rule in self._base_rules
                if _is_effective(rule, evaluation_date)
                and rule.budget_status == budget_status
                and _amount_matches(rule, amount)
            ],
            key=lambda rule: (rule.priority, rule.rule_code),
        )

    def get_matching_additional_rules(
        self,
        context: ApprovalContext,
        evaluation_date: date,
    ) -> list[ApprovalRule]:
        return sorted(
            [
                rule
                for rule in self._additional_rules
                if _is_effective(rule, evaluation_date)
                and _amount_matches(rule, context.amount)
                and _condition_matches(rule, context)
            ],
            key=lambda rule: (rule.priority, rule.rule_code),
        )

    def get_all_active_rules(
        self,
        evaluation_date: date,
    ) -> list[ApprovalRule]:
        return [
            rule
            for rule in [*self._base_rules, *self._additional_rules]
            if _is_effective(rule, evaluation_date)
        ]

    def get_rule_statistics(self) -> ApprovalRuleStatistics:
        active_base = [rule for rule in self._base_rules if rule.is_active]
        active_additional = [
            rule for rule in self._additional_rules if rule.is_active
        ]
        return ApprovalRuleStatistics(
            base_rules_count=len(self._base_rules),
            additional_rules_count=len(self._additional_rules),
            active_rules_count=len(active_base) + len(active_additional),
        )


class SupabaseApprovalRuleRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    def from_credentials(
        cls,
        url: str,
        service_role_key: str,
    ) -> "SupabaseApprovalRuleRepository":
        if not url or not service_role_key:
            raise ApprovalRuleRepositoryError(
                "Supabase URL and service role key are required"
            )
        try:
            return cls(create_client(url, service_role_key))
        except Exception as exc:
            raise ApprovalRuleRepositoryError(
                "Failed to initialize approval rule repository"
            ) from exc

    def get_matching_base_rules(
        self,
        amount: Decimal,
        budget_status: BudgetStatus,
        evaluation_date: date,
    ) -> list[ApprovalRule]:
        rules = self._read_rules("approval_base_rules", "base")
        return sorted(
            [
                rule
                for rule in rules
                if _is_effective(rule, evaluation_date)
                and rule.budget_status == budget_status
                and _amount_matches(rule, amount)
            ],
            key=lambda rule: (rule.priority, rule.rule_code),
        )

    def get_matching_additional_rules(
        self,
        context: ApprovalContext,
        evaluation_date: date,
    ) -> list[ApprovalRule]:
        rules = self._read_rules("approval_additional_rules", "additional")
        return sorted(
            [
                rule
                for rule in rules
                if _is_effective(rule, evaluation_date)
                and _amount_matches(rule, context.amount)
                and _condition_matches(rule, context)
            ],
            key=lambda rule: (rule.priority, rule.rule_code),
        )

    def get_all_active_rules(
        self,
        evaluation_date: date,
    ) -> list[ApprovalRule]:
        rules = [
            *self._read_rules("approval_base_rules", "base"),
            *self._read_rules("approval_additional_rules", "additional"),
        ]
        return [rule for rule in rules if _is_effective(rule, evaluation_date)]

    def get_rule_statistics(self) -> ApprovalRuleStatistics:
        base = self._read_rules("approval_base_rules", "base")
        additional = self._read_rules(
            "approval_additional_rules",
            "additional",
        )
        return ApprovalRuleStatistics(
            base_rules_count=len(base),
            additional_rules_count=len(additional),
            active_rules_count=sum(
                rule.is_active for rule in [*base, *additional]
            ),
        )

    def _read_rules(
        self,
        table: str,
        rule_kind: str,
    ) -> list[ApprovalRule]:
        try:
            response = self._client.table(table).select("*").execute()
            return [
                ApprovalRule.model_validate(
                    {**row, "rule_kind": rule_kind}
                )
                for row in response.data
            ]
        except Exception as exc:
            raise ApprovalRuleRepositoryError(
                _repository_error_message(exc)
            ) from exc


def _is_effective(rule: ApprovalRule, evaluation_date: date) -> bool:
    return (
        rule.is_active
        and rule.effective_from <= evaluation_date
        and (
            rule.effective_to is None
            or evaluation_date <= rule.effective_to
        )
    )


def _amount_matches(rule: ApprovalRule, amount: Decimal) -> bool:
    return (
        (rule.min_amount is None or rule.min_amount <= amount)
        and (rule.max_amount is None or amount <= rule.max_amount)
    )


def _condition_matches(
    rule: ApprovalRule,
    context: ApprovalContext,
) -> bool:
    value = rule.condition_value
    if rule.condition_type == "urgency":
        return context.urgency == value
    if rule.condition_type == "single_supplier":
        return context.single_supplier is (value == "true")
    if rule.condition_type == "category":
        return context.category_code == value
    if rule.condition_type == "data_access":
        return context.has_data_access is (value == "true")
    if rule.condition_type == "work_on_site":
        return context.work_on_site is (value == "true")
    return False


def _repository_error_message(exc: Exception) -> str:
    message = str(exc).casefold()
    if any(
        marker in message
        for marker in (
            "pgrst205",
            "schema cache",
            "does not exist",
            "undefined table",
            "42p01",
        )
    ):
        return (
            "Approval rule tables are unavailable; "
            "apply migration 005"
        )
    if any(
        marker in message
        for marker in ("permission denied", "42501", "forbidden")
    ):
        return "Permission denied while reading approval rules"
    if any(
        marker in message
        for marker in (
            "network",
            "connection",
            "timeout",
            "timed out",
            "dns",
        )
    ):
        return "Network error while reading approval rules"
    return "Failed to read approval rules"
