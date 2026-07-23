from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_request_service
from app.core.exceptions import (
    DraftUpdateForbiddenError,
    RepositoryError,
    RequestNotFoundError,
)
from app.schemas.request import RequestCreate, RequestRead, RequestUpdate
from app.services.requests import RequestService

router = APIRouter(prefix="/requests", tags=["requests"])
RequestServiceDependency = Annotated[RequestService, Depends(get_request_service)]


@router.post("", response_model=RequestRead, status_code=status.HTTP_201_CREATED)
def create_draft(
    request: RequestCreate,
    service: RequestServiceDependency,
) -> RequestRead:
    try:
        return service.create_draft(request)
    except RepositoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/{request_id}", response_model=RequestRead)
def get_request(
    request_id: UUID,
    service: RequestServiceDependency,
) -> RequestRead:
    try:
        return service.get_request(request_id)
    except RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except RepositoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.patch("/{request_id}", response_model=RequestRead)
def update_draft(
    request_id: UUID,
    update: RequestUpdate,
    service: RequestServiceDependency,
) -> RequestRead:
    try:
        return service.update_draft(request_id, update)
    except RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except DraftUpdateForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except RepositoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
