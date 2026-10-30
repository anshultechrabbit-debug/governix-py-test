import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditFact(BaseModel):
    """One readable line of an event's details ("Role: Branch Manager")."""

    label: str
    value: str


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: uuid.UUID | None
    request_id: str | None
    details: dict[str, Any]
    created_at: datetime
    # In plain words (app.modules.audit.describe): who, where, what, and one sentence saying what happened.
    actor_name: str | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    organization_name: str | None = None
    resource_name: str | None = None
    action_label: str = ""
    category: str = ""
    severity: Literal["info", "warning", "critical"] = "info"
    summary: str = ""
    facts: list[AuditFact] = Field(default_factory=list)
