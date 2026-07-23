from pydantic import BaseModel

from app.schemas.common import DatabaseHealthStatus


class DatabaseHealthResponse(BaseModel):
    status: DatabaseHealthStatus
    configured: bool
    detail: str
