from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import RequestStatus, RequestType


class RequestCreate(BaseModel):
    user_id: UUID
    request_type: RequestType | None = None
    category_code: str | None = None
    title: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RequestUpdate(BaseModel):
    request_type: RequestType | None = None
    category_code: str | None = None
    title: str | None = None
    data: dict[str, Any] | None = None


class RequestRead(RequestCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    request_number: str | None = None
    status: RequestStatus
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None
