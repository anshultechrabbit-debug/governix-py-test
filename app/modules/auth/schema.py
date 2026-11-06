import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.core.inputs import NewPassword


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: NewPassword


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    # From the emailed link (PASSWORD_RESET_URL?token=...).
    token: str = Field(min_length=1, max_length=2048)
    new_password: NewPassword


class Message(BaseModel):
    message: str


class TokenPair(BaseModel):
    """Issued tokens. Internal only: they reach the browser as cookies (`cookies.py`), never in a body."""

    access_token: str
    refresh_token: str
    csrf_token: str
    expires_in: int


class AuthSession(BaseModel):
    """What login and refresh return: when the access cookie expires, not the tokens.

    `mfa_required`: the password was accepted but the account uses two-step sign-in. No session yet: send the
    authenticator (or backup) code to /auth/mfa/verify within `expires_in` seconds."""

    expires_in: int
    mfa_required: bool = False


class MfaChallenge(BaseModel):
    """Internal: the code step of a sign-in. Reaches the browser only as the HttpOnly governix_mfa cookie."""

    token: str
    expires_in: int


class MfaCode(BaseModel):
    # 6 digits from the app, or a backup code ("k7mq-x3vp-9t2n").
    code: str = Field(min_length=6, max_length=32)


class MfaDisable(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=32)


class MfaStatus(BaseModel):
    enabled: bool
    enabled_at: datetime | None
    recovery_codes_left: int


class MfaSetup(BaseModel):
    """Shown once while setting up: the QR code holds `otpauth_uri`; `secret` is for typing it in by hand."""

    secret: str
    otpauth_uri: str


class RecoveryCodes(BaseModel):
    """Shown once. Each works once, in place of an authenticator code."""

    codes: list[str]


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
    mfa_enabled: bool = False
