"""Email settings (Master Admin › Settings › Email)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

Security = Literal["starttls", "ssl", "none"]

# Emails about the person's own account. "forgot_password" always goes out (it is how they get back in).
ACCOUNT_EVENTS: dict[str, tuple[str, str, bool]] = {
    # key: (label, what it sends, default)
    "account_created": ("New account", "Welcome email with the sign-in address and the temporary password.", True),
    "password_reset_by_admin": ("Password reset by an admin", "The new temporary password.", True),
    "two_step_reset": ("Two-step sign-in reset", "Tells the person their authenticator was turned off.", True),
    "password_changed": ("Password changed", "Security notice after the person changes their password.", True),
}

# Email copies of in-app notifications, per notification type.
NOTIFICATION_EVENTS: dict[str, tuple[str, bool]] = {
    "SECURITY_ALERT": ("Security alerts (two-step sign-in changes)", True),
    "AI_THRESHOLD": ("AI usage alerts (70% … 100% of a limit)", True),
    "AI_STATUS": ("AI availability changes (activated, suspended, key rejected)", True),
    "TICKET_ASSIGNED": ("Support ticket assigned", True),
    "SUPPORT_RESPONSE": ("Reply on a support ticket", True),
    "TICKET_SLA": ("Support ticket overdue (SLA)", True),
    "TICKET_FORWARDED": ("Support ticket forwarded", False),
    "TICKET_UPDATED": ("Support ticket status changed", False),
    "POLICY_ASSIGNED": ("Policy assigned to me", True),
    "VERSION_APPROVAL": ("New policy version waiting for my approval", True),
    "POLICY_EXPIRING": ("Policy expiring soon", True),
    "NEW_POLICY": ("New policy published", False),
    "POLICY_UPDATED": ("Policy updated", False),
    "POLICY_VERSION_CHANGED": ("New policy version in force", False),
    "POLICY_REMOVED": ("Policy assignment removed", False),
    "POLICY_EXPIRED": ("Policy expired", False),
    "COMPLAINT_RESPONSE": ("Reply on an old complaint", False),
}


class EmailEvent(BaseModel):
    key: str
    group: Literal["account", "notification"]
    label: str
    description: str | None = None
    enabled: bool


class EmailSettingsRead(BaseModel):
    # "settings": saved here; "environment": the SMTP_* lines of the server's .env; "none": not set up.
    source: Literal["settings", "environment", "none"]
    enabled: bool
    host: str | None
    port: int
    username: str | None
    # Only whether one is stored: the password itself never leaves the server.
    has_password: bool
    security: Security
    from_email: str | None
    from_name: str | None
    reply_to: str | None
    # The address in links (the main portal, e.g. http://89.116.122.215:7001); banks get their own port.
    portal_url: str | None
    include_password: bool
    events: list[EmailEvent]
    updated_at: datetime | None = None


class EmailSettingsSave(BaseModel):
    enabled: bool = True
    host: str | None = Field(None, max_length=255)
    port: int = Field(587, ge=1, le=65535)
    username: str | None = Field(None, max_length=255)
    # None: keep the stored password. "": remove it.
    password: str | None = Field(None, max_length=500)
    security: Security = "starttls"
    from_email: EmailStr | None = None
    from_name: str | None = Field(None, max_length=120)
    reply_to: EmailStr | None = None
    portal_url: str | None = Field(None, max_length=300, pattern=r"^https?://[^\s/]+(:\d+)?/?$")
    # The welcome and admin-reset emails include the temporary password (else: "ask your admin for it").
    include_password: bool = True
    events: dict[str, bool] = {}

    @field_validator("host", "username", "from_name", mode="before")
    @classmethod
    def _blank(cls, value):
        return (value or "").strip() or None

    @field_validator("events")
    @classmethod
    def _known(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = set(value) - set(ACCOUNT_EVENTS) - set(NOTIFICATION_EVENTS)
        if unknown:
            raise ValueError(f"Unknown email events: {', '.join(sorted(unknown))}.")
        return value


class EmailTest(BaseModel):
    to: EmailStr
    # Test the form as it is now (unsaved); None: the saved settings.
    settings: EmailSettingsSave | None = None


class EmailTestResult(BaseModel):
    ok: bool
    message: str
