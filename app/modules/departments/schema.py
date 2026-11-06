import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.branches.schema import CODE_PATTERN
from app.core.inputs import Name


class DepartmentCreate(BaseModel):
    branch_id: uuid.UUID
    name: Name = Field(min_length=2, max_length=200)
    code: str = Field(pattern=CODE_PATTERN)


class DepartmentUpdate(BaseModel):
    name: Name | None = Field(default=None, min_length=2, max_length=200)
    is_active: bool | None = None


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID
    name: str
    code: str
    is_active: bool
    created_at: datetime
