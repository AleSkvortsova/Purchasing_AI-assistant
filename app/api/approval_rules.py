from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import (
    get_approval_rule_service,
    get_optional_approval_rule_repository,
)
from app.core.config import get_settings
from app.rules.exceptions import ApprovalRuleRepositoryError
from app.rules.models import (
    ApprovalContext,
    ApprovalRouteResult,
    ApprovalRulesHealth,
)
from app.rules.repository import ApprovalRuleRepository
from app.rules.service import ApprovalRuleService

router = APIRouter(prefix="/approval-rules", tags=["approval-rules"])


@router.post("/evaluate", response_model=ApprovalRouteResult)
def evaluate_approval_route(
    context: ApprovalContext,
    service: Annotated[
        ApprovalRuleService,
        Depends(get_approval_rule_service),
    ],
) -> ApprovalRouteResult:
    try:
        return service.evaluate(context)
    except ApprovalRuleRepositoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/health", response_model=ApprovalRulesHealth)
def approval_rules_health(
    repository: Annotated[
        ApprovalRuleRepository | None,
        Depends(get_optional_approval_rule_repository),
    ],
) -> ApprovalRulesHealth:
    settings = get_settings()
    if repository is None:
        return ApprovalRulesHealth(
            configured=False,
            database_configured=settings.supabase_configured,
        )
    try:
        statistics = repository.get_rule_statistics()
    except ApprovalRuleRepositoryError:
        return ApprovalRulesHealth(
            configured=False,
            database_configured=settings.supabase_configured,
        )
    return ApprovalRulesHealth(
        configured=True,
        database_configured=True,
        **statistics.model_dump(),
    )
