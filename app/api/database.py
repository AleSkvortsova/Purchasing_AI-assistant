from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_database_health_service
from app.schemas.database import DatabaseHealthResponse
from app.services.database import DatabaseHealthService

router = APIRouter(prefix="/db", tags=["database"])
DatabaseHealthServiceDependency = Annotated[
    DatabaseHealthService,
    Depends(get_database_health_service),
]


@router.get("/health", response_model=DatabaseHealthResponse)
def database_health(
    service: DatabaseHealthServiceDependency,
) -> DatabaseHealthResponse:
    return service.check()
