import uuid

from pydantic import BaseModel, EmailStr, Field

from app.core.security import MIN_PASSWORD_LENGTH


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    # From the emailed link (PASSWORD_RESET_URL?token=...).
    token: str = Field(min_length=1, max_length=2048)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class Message(BaseModel):
    message: str


class TokenPair(BaseModel):
    """Issued tokens. Internal only: they reach the browser as cookies (`cookies.py`), never in a body."""

    access_token: str
    refresh_token: str
    csrf_token: str
    expires_in: int


class AuthSession(BaseModel):
    """What login and refresh return: when the access cookie expires, not the tokens."""

    expires_in: int


class Me(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    organization_id: uuid.UUID | None
    organization_name: str | None
    branch_id: uuid.UUID | None
    branch_name: str | None = None
    department_id: uuid.UUID | None
    permissions: list[str]
