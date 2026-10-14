import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$"
# PIN codes and postal codes: "400001", "94105", "94105-1234", "SW1A 1AA".
POSTAL_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 -]{1,18}[A-Za-z0-9]$"


class BranchAddress(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    street_address: str = Field(min_length=2, max_length=300)
    state: str = Field(min_length=2, max_length=100)
    postal_code: str = Field(pattern=POSTAL_CODE_PATTERN)


class NewBranch(BranchAddress):
    """A branch created with its Branch Manager (Add user); its code is made from the name."""

    name: str = Field(min_length=2, max_length=200)


class BranchCreate(NewBranch):
    code: str = Field(pattern=CODE_PATTERN)


class BranchUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=200)
    street_address: str | None = Field(default=None, min_length=2, max_length=300)
    state: str | None = Field(default=None, min_length=2, max_length=100)
    postal_code: str | None = Field(default=None, pattern=POSTAL_CODE_PATTERN)
    is_active: bool | None = None


class BranchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    code: str
    street_address: str | None = None
    state: str | None = None
    postal_code: str | None = None
    is_active: bool
    created_at: datetime
