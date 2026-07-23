from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import UserRole


class UserCreate(BaseModel):
    telegram_id: int | None = None
    full_name: str
    department: str | None = None
    role: UserRole = UserRole.REQUESTER


class UserRead(UserCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
