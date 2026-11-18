"""Master Admin › Settings › Email: the outgoing mail server and which emails Governix sends."""

import dataclasses
import smtplib
import ssl
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import ServiceUnavailableError, ValidationError
from app.core.responses import ApiResponse, ok
from app.modules.audit.service import record_event
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.auth.step_up import StepUp
from app.modules.email import service
from app.modules.email.schema import EmailSettingsRead, EmailSettingsSave, EmailTest, EmailTestResult

router = APIRouter(prefix="/settings/email", tags=["settings"])

NOT_READY = ServiceUnavailableError(
    "Email settings need a database update first. On the server run: alembic upgrade head (in the backend "
    "folder), then reload this page. Until then, email uses the SMTP_* lines of .env.",
    code="SETTINGS_NOT_READY",
)

MasterAdmin = Annotated[Principal, Depends(require(Permission.ORGANIZATIONS_MANAGE))]
Db = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]


@router.get("", response_model=ApiResponse[EmailSettingsRead])
def read_email_settings(principal: MasterAdmin, db: Db, settings: AppSettings):
    """The mail server in force (saved here, from .env, or none) and each kind of email, on or off."""
    try:
        return ok(service.read_settings(db, settings))
    except service.SettingsNotReady:
        raise NOT_READY from None


@router.put("", response_model=ApiResponse[EmailSettingsRead])
def save_email_settings(body: EmailSettingsSave, principal: MasterAdmin, db: Db, settings: AppSettings,
                        confirm_code: StepUp):
    """Save the mail server and the email choices. Stores a mail password, so it needs an authenticator code."""
    if body.enabled and not body.host:
        raise ValidationError("Enter the mail server (SMTP host), or switch email off.", code="SMTP_HOST_REQUIRED")
    confirm_code("settings.email")
    try:
        service.save_settings(db, settings, body, principal.user_id)
    except service.SettingsNotReady:
        raise NOT_READY from None
    record_event(db, "settings.email_updated", actor=principal, resource_type="settings", details={
        "enabled": body.enabled, "host": body.host, "port": body.port, "security": body.security,
        "password_changed": body.password is not None,
        "events": {k: v for k, v in body.events.items()},
    })
    db.commit()
    return ok(service.read_settings(db, settings))


@router.post("/test", response_model=ApiResponse[EmailTestResult])
def send_test_email(body: EmailTest, principal: MasterAdmin, db: Db, settings: AppSettings):
    """Send a test message now, with the form as it is (unsaved) or the saved settings, and say what happened."""
    try:
        config = (service.config_from_form(db, settings, body.settings) if body.settings is not None
                  else service.active_config(db, settings))
    except service.SettingsNotReady:
        raise NOT_READY from None
    if config is None:
        return ok(EmailTestResult(ok=False, message="No mail server is set up yet: enter the SMTP host first."))
    if config.username and not config.password:
        # Signing in with a blank password: Gmail and others just hang up, which reads like a network fault.
        return ok(EmailTestResult(ok=False, message=(
            f"Enter the password for {config.username}. For Gmail and Microsoft 365 this is an app password "
            "(16 characters, created in the account's security settings with two-step verification on), "
            "not the normal account password.")))
    error = _try_send(settings, config, str(body.to))
    if error is None:
        return ok(EmailTestResult(ok=True, message=f"Sent to {body.to} through {config.host}:{config.port}."))
    mismatch = (config.port == 465 and config.security != "ssl") or (config.port == 587 and config.security == "ssl")
    if isinstance(error, (smtplib.SMTPServerDisconnected, ssl.SSLError, ConnectionResetError)) or (
            mismatch and isinstance(error, TimeoutError)):
        # The usual cause: the security mode does not match the port (465 wants SSL at once, 587 STARTTLS).
        # Try the other modes and say which one works, instead of a bare "connection closed".
        for security, port in _alternatives(config):
            trial = dataclasses.replace(config, security=security, port=port)
            if _try_send(settings, trial, str(body.to)) is None:
                return ok(EmailTestResult(ok=False, message=(
                    f"{config.host}:{config.port} with {LABELS[config.security]} closed the connection, but "
                    f"{LABELS[security]} on port {port} works (the test email was sent that way). Set Security to "
                    f"{LABELS[security]} and Port to {port}, then save.")))
        return ok(EmailTestResult(ok=False, message=(
            f"{config.host}:{config.port} closed the connection with every security mode. Check the host and "
            "port, and whether this network blocks outgoing mail ports (many networks block 25 and 465).")))
    return ok(EmailTestResult(ok=False, message=_explain(error, config)))


LABELS = {"starttls": "STARTTLS", "ssl": "SSL/TLS", "none": "no encryption"}


def _try_send(settings: Settings, config: service.SmtpConfig, to: str) -> Exception | None:
    subject, text, html_body = service.test_message(settings, config, to)
    try:
        service.deliver(config, to=to, subject=subject, text=text, html_body=html_body)
    except (smtplib.SMTPException, OSError) as exc:
        return exc
    return None


@router.get("/log", response_model=ApiResponse[list[dict]])
def recent_emails(principal: MasterAdmin):
    """The latest emails this server tried to send (newest first): to whom, what, and whether the mail server
    accepted them. "Accepted" means delivered to the provider; a missing email is then in Spam or a filter."""
    return ok(list(service.RECENT))


def _alternatives(config: service.SmtpConfig) -> list[tuple[str, int]]:
    """Other (security, port) pairs worth trying: the other mode on this port, then each mode on its usual port."""
    pairs = [(mode, config.port) for mode in ("ssl", "starttls") if mode != config.security]
    pairs += [(mode, port) for mode, port in (("ssl", 465), ("starttls", 587)) if (mode, port) not in pairs
              and (mode, port) != (config.security, config.port)]
    return pairs


def _explain(error: Exception, config: service.SmtpConfig) -> str:
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return ("The mail server rejected the username or password. Gmail and Microsoft 365 need an app password "
                "(with two-step verification on), not the normal account password.")
    if isinstance(error, smtplib.SMTPSenderRefused):
        return (f"The mail server won't send as {config.sender}: the From email must be the mailbox you sign in "
                "with, or an address it may send for.")
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address."
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return (f"{config.host}:{config.port} does not offer {LABELS[config.security]}. Try SSL/TLS on 465 or "
                "STARTTLS on 587.")
    if isinstance(error, smtplib.SMTPException):
        return f"The mail server refused the message: {error}"[:300]
    return (f"Could not reach {config.host}:{config.port} ({type(error).__name__}). Check the host and port; the "
            "server's network may block outgoing mail ports.")

