import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from app.core.security import MIN_PASSWORD_LENGTH
from app.modules.auth.permissions import Role


def _blank_is_none(value: str | None) -> str | None:
    return (value or "").strip() or None


# Digits with the usual separators: "+1 (555) 000-0000", "+91 98765 43210".
Phone = Annotated[str | None, Field(max_length=50, pattern=r"^[0-9+()\-. ]*$"), AfterValidator(_blank_is_none)]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    # Left out: the server generates a temporary password and returns it once.
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=1024)
    phone: Phone = None
    role: Role
    # Only the master admin chooses an organization; tenant admins create users in their own.
    organization_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    email: EmailStr | None = None
    phone: Phone = None
    # Master admin only, and only for an account that never signed in.
    organization_id: uuid.UUID | None = None
    is_active: bool | None = None
    role: Role | None = None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class PasswordReset(BaseModel):
    # Left out: the server generates one and returns it once.
    new_password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class TemporaryPassword(BaseModel):
    """A generated password, shown to the admin once and never stored in clear."""

    temporary_password: str | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    phone: str | None
    role: str
    organization_id: uuid.UUID | None
    organization_name: str | None = None
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreated(UserRead, TemporaryPassword):
    pass


UserStatus = Literal["active", "inactive"]


class UserStats(BaseModel):
    total: int
    active: int
    inactive: int
