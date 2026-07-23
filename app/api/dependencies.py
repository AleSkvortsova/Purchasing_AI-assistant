from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.config import get_settings
from app.repositories.request import RequestRepository
from app.repositories.supabase import SupabaseRequestRepository
from app.services.database import DatabaseHealthService
from app.services.requests import RequestService


@lru_cache
def get_supabase_repository() -> SupabaseRequestRepository | None:
    settings = get_settings()
    if not settings.supabase_configured:
        return None
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    return SupabaseRequestRepository.from_credentials(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )


def get_request_repository() -> RequestRepository:
    repository = get_supabase_repository()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase is not configured",
        )
    return repository


def get_request_service(
    repository: Annotated[RequestRepository, Depends(get_request_repository)],
) -> RequestService:
    return RequestService(repository)


def get_database_health_service() -> DatabaseHealthService:
    return DatabaseHealthService(get_supabase_repository())
