import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from app.core.inputs import NewPassword, PersonName, Phone
from app.modules.auth.permissions import Role
from app.modules.branches.schema import NewBranch




class UserCreate(BaseModel):
    email: EmailStr
    full_name: PersonName = Field(min_length=2, max_length=200)
    # Left out: the server generates a temporary password and returns it once.
    password: NewPassword | None = None
    phone: Phone = None
    role: Role
    # Only the master admin chooses an organization; tenant admins create users in their own.
    organization_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    # A Branch Manager for a branch that does not exist yet: it is created with them.
    new_branch: NewBranch | None = None
    department_id: uuid.UUID | None = None


class UserUpdate(BaseModel):
    full_name: PersonName | None = Field(default=None, min_length=2, max_length=200)
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
    new_password: NewPassword | None = None


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
    branch_name: str | None = None
    department_id: uuid.UUID | None
    is_active: bool
    # Two-step sign-in (authenticator app) is on.
    mfa_enabled: bool = False
    last_login_at: datetime | None
    created_at: datetime
    # The caller may change this user (edit, deactivate, delete, reset). False: shown read only.
    can_manage: bool = True


class UserCreated(UserRead, TemporaryPassword):
    pass


UserStatus = Literal["active", "inactive"]
UserSort = Literal["name", "-name", "email", "-email", "created", "-created", "last_login", "-last_login",
                   "organization", "-organization", "role", "-role"]
# "manage": users the caller may change; "read_only": the ones it only sees.
UserAccess = Literal["manage", "read_only"]


class UserStats(BaseModel):
    total: int
    active: int
    inactive: int
    # Users of each role (all statuses), e.g. {"org_admin": 12, "department_user": 30}.
    by_role: dict[str, int] = {}
    # Master Admins and Organization Admins.
    privileged: int = 0
    # Organizations the listed users belong to.
    organizations: int = 0
    # Users the caller may change; the rest it sees read only.
    manageable: int = 0
