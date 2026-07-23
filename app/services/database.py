from app.core.exceptions import RepositoryError
from app.repositories.request import RequestRepository
from app.schemas.common import DatabaseHealthStatus
from app.schemas.database import DatabaseHealthResponse


class DatabaseHealthService:
    def __init__(self, repository: RequestRepository | None) -> None:
        self._repository = repository

    def check(self) -> DatabaseHealthResponse:
        if self._repository is None:
            return DatabaseHealthResponse(
                status=DatabaseHealthStatus.NOT_CONFIGURED,
                configured=False,
                detail="Supabase is not configured",
            )

        try:
            self._repository.health_check()
        except RepositoryError:
            return DatabaseHealthResponse(
                status=DatabaseHealthStatus.ERROR,
                configured=True,
                detail="Database connection check failed",
            )

        return DatabaseHealthResponse(
            status=DatabaseHealthStatus.OK,
            configured=True,
            detail="Database connection is healthy",
        )
