from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DialogStateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    active_request_id: UUID | None = None
    current_intent: str | None = None
    current_step: str | None = None
    state_data: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime
