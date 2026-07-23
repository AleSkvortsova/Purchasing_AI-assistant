from datetime import date

from app.rules.models import (
    ApprovalContext,
    ApprovalRouteResult,
    ApprovalRule,
    SourceReference,
)
from app.rules.repository import ApprovalRuleRepository


class ApprovalRuleService:
    def __init__(self, repository: ApprovalRuleRepository) -> None:
        self._repository = repository

    def evaluate(self, context: ApprovalContext) -> ApprovalRouteResult:
        evaluation_date = context.evaluation_date or date.today()
        if context.budget_status is None:
            return ApprovalRouteResult(
                status="needs_clarification",
                missing_fields=["budget_status"],
                clarification_questions=[
                    "Закупка предусмотрена бюджетом или является внебюджетной?"
                ],
            )

        base_rules = self._repository.get_matching_base_rules(
            context.amount,
            context.budget_status,
            evaluation_date,
        )
        if not base_rules:
            return ApprovalRouteResult(
                status="no_matching_rule",
                warnings=[
                    "Подходящее базовое правило не найдено; "
                    "требуется ручная проверка."
                ],
            )
        if len(base_rules) > 1:
            codes = ", ".join(rule.rule_code for rule in base_rules)
            return ApprovalRouteResult(
                status="conflict",
                warnings=[
                    f"Найдено несколько базовых правил: {codes}. "
                    "Требуется ручная проверка."
                ],
                source_references=[
                    _source_reference(rule) for rule in base_rules
                ],
            )

        base_rule = base_rules[0]
        additional_rules = (
            self._repository.get_matching_additional_rules(
                context,
                evaluation_date,
            )
        )
        base_approvers = _deduplicate(base_rule.approvers)
        additional_approvers = _deduplicate(
            [
                approver
                for rule in additional_rules
                for approver in rule.approvers
            ]
        )
        final_approvers = _deduplicate(
            [*base_approvers, *additional_approvers]
        )
        return ApprovalRouteResult(
            status="resolved",
            base_rule_code=base_rule.rule_code,
            applied_additional_rule_codes=[
                rule.rule_code for rule in additional_rules
            ],
            base_approvers=base_approvers,
            additional_approvers=additional_approvers,
            final_approvers=final_approvers,
            source_references=[
                _source_reference(base_rule),
                *[_source_reference(rule) for rule in additional_rules],
            ],
        )


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _source_reference(rule: ApprovalRule) -> SourceReference:
    return SourceReference(
        document_id=rule.source_document_id,
        section=rule.source_section,
        rule_code=rule.rule_code,
    )
