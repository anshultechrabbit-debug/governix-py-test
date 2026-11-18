"""Outgoing email, configured by the Master Admin (Settings › Email) or, until then, by the SMTP_* lines of .env.

Every email Governix sends goes through here:
  * account emails (welcome with the temporary password, admin password reset, two-step reset, password
    changed, forgot password), sent right after the change was saved;
  * copies of in-app notifications (`queue_notification_emails`), sent only once the transaction that created
    them commits, so a rolled-back action never emails anyone.

Sending happens on a small thread pool and never fails the request: a failure is logged. Settings are read in a
session of their own (cached for a few seconds), so a problem there cannot spoil the caller's transaction.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any
from urllib.parse import urlencode, urlsplit

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.config import AppEnv, Settings
from app.modules.auth import mfa
from app.modules.email.schema import (
    ACCOUNT_EVENTS, NOTIFICATION_EVENTS, EmailEvent, EmailSettingsRead, EmailSettingsSave,
)
from app.modules.email.templates import Brand, Content, render_html, render_text

logger = logging.getLogger(__name__)

KEY = "email"
CACHE_SECONDS = 15
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="governix-mail")
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str  # starttls | ssl | none
    username: str | None
    password: str | None
    sender: str  # "Governix <no-reply@bank.com>"
    reply_to: str | None
    timeout: float
    portal_url: str | None
    include_password: bool
    events: dict[str, bool] = field(default_factory=dict)

    def wants(self, event_key: str) -> bool:
        if event_key == "forgot_password":
            return True
        default = ACCOUNT_EVENTS.get(event_key, (None, None, False))[2] if event_key in ACCOUNT_EVENTS \
            else NOTIFICATION_EVENTS.get(event_key, (None, False))[1]
        return bool(self.events.get(event_key, default))


# --- settings ---------------------------------------------------------------------------------------------------

def _stored(session: Session) -> dict | None:
    """The saved "email" setting, read in a session of its own and cached briefly; None when there is none
    (or the table does not exist yet: the migration has not run)."""
    with _cache_lock:
        hit = _cache.get(KEY)
        if hit and hit[0] > time.monotonic():
            return hit[1] or None
    from app.modules.email.model import PlatformSetting

    value: dict = {}
    try:
        with Session(bind=session.get_bind()) as own:
            row = own.get(PlatformSetting, KEY)
            value = dict(row.value) if row is not None else {}
    except Exception:  # no table yet, database briefly away: fall back to .env
        logger.debug("Email settings not readable; using .env", exc_info=True)
    with _cache_lock:
        _cache[KEY] = (time.monotonic() + CACHE_SECONDS, value)
    return value or None


def forget_cached() -> None:
    with _cache_lock:
        _cache.clear()


def _portal_default(settings: Settings) -> str | None:
    parts = urlsplit(settings.PASSWORD_RESET_URL or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def active_config(session: Session, settings: Settings) -> SmtpConfig | None:
    """The mail server in force: the saved settings when there are any (switched off: no email at all), else
    the SMTP_* lines of .env, else None."""
    stored = _stored(session)
    if stored is not None:
        if not stored.get("enabled") or not stored.get("host"):
            return None
        sender = formataddr((stored.get("from_name") or settings.APP_NAME, stored.get("from_email")
                             or stored.get("username") or f"no-reply@{stored['host']}"))
        return SmtpConfig(
            host=stored["host"], port=int(stored.get("port") or 587), security=stored.get("security") or "starttls",
            username=stored.get("username"),
            password=mfa.decrypt(settings, stored["password"]) if stored.get("password") else None,
            sender=sender, reply_to=stored.get("reply_to"), timeout=settings.SMTP_TIMEOUT_SECONDS,
            portal_url=stored.get("portal_url") or _portal_default(settings),
            include_password=stored.get("include_password", True), events=dict(stored.get("events") or {}),
        )
    if settings.SMTP_HOST:
        return SmtpConfig(
            host=settings.SMTP_HOST, port=settings.SMTP_PORT,
            security="ssl" if settings.SMTP_SSL else "starttls" if settings.SMTP_STARTTLS else "none",
            username=settings.SMTP_USERNAME,
            password=settings.SMTP_PASSWORD.get_secret_value() if settings.SMTP_PASSWORD else None,
            sender=settings.SMTP_FROM, reply_to=None, timeout=settings.SMTP_TIMEOUT_SECONDS,
            portal_url=_portal_default(settings), include_password=True,
        )
    return None


class SettingsNotReady(Exception):
    """The platform_settings table does not exist yet: the database migration has not been run."""


def _row(session: Session):
    """The saved "email" row, or SettingsNotReady when the table is missing (the session is rolled back)."""
    from sqlalchemy.exc import ProgrammingError

    from app.modules.email.model import PlatformSetting

    try:
        return session.get(PlatformSetting, KEY)
    except ProgrammingError as exc:
        session.rollback()
        if "platform_settings" in str(exc):
            raise SettingsNotReady from None
        raise


def read_settings(session: Session, settings: Settings) -> EmailSettingsRead:
    row = _row(session)
    stored = dict(row.value) if row is not None else None
    if stored is not None:
        source, values = "settings", stored
    elif settings.SMTP_HOST:
        name, address = parseaddr(settings.SMTP_FROM)
        source, values = "environment", {
            "enabled": True, "host": settings.SMTP_HOST, "port": settings.SMTP_PORT,
            "username": settings.SMTP_USERNAME, "password": bool(settings.SMTP_PASSWORD),
            "security": "ssl" if settings.SMTP_SSL else "starttls" if settings.SMTP_STARTTLS else "none",
            "from_email": address or None, "from_name": name or None,
        }
    else:
        source, values = "none", {"enabled": False}
    chosen = values.get("events") or {}
    events = [EmailEvent(key=k, group="account", label=label, description=text, enabled=chosen.get(k, default))
              for k, (label, text, default) in ACCOUNT_EVENTS.items()]
    events += [EmailEvent(key=k, group="notification", label=label, enabled=chosen.get(k, default))
               for k, (label, default) in NOTIFICATION_EVENTS.items()]
    return EmailSettingsRead(
        source=source, enabled=bool(values.get("enabled")), host=values.get("host"),
        port=int(values.get("port") or 587), username=values.get("username"),
        has_password=bool(values.get("password")), security=values.get("security") or "starttls",
        from_email=values.get("from_email"), from_name=values.get("from_name"), reply_to=values.get("reply_to"),
        portal_url=values.get("portal_url") or _portal_default(settings),
        include_password=values.get("include_password", True), events=events,
        updated_at=row.updated_at if row is not None else None,
    )


def _merged(session: Session, settings: Settings, body: EmailSettingsSave) -> dict:
    """The value to store: the form, keeping the stored password unless a new one (or "") is given."""
    row = _row(session)
    previous = dict(row.value) if row is not None else {}
    value = body.model_dump(mode="json", exclude={"password"})
    if body.password is None:
        value["password"] = previous.get("password")
    else:
        value["password"] = mfa.encrypt(settings, body.password) if body.password else None
    value["events"] = {**(previous.get("events") or {}), **body.events}
    return value


def save_settings(session: Session, settings: Settings, body: EmailSettingsSave, actor_id: uuid.UUID | None) -> None:
    from app.modules.email.model import PlatformSetting

    value = _merged(session, settings, body)
    row = _row(session)
    if row is None:
        session.add(PlatformSetting(key=KEY, value=value, updated_by_id=actor_id))
    else:
        row.value, row.updated_by_id = value, actor_id
    forget_cached()


def config_from_form(session: Session, settings: Settings, body: EmailSettingsSave) -> SmtpConfig | None:
    value = _merged(session, settings, body)
    if not value.get("host"):
        return None
    return SmtpConfig(
        host=value["host"], port=int(value["port"]), security=value["security"], username=value.get("username"),
        password=mfa.decrypt(settings, value["password"]) if value.get("password") else None,
        sender=formataddr((value.get("from_name") or settings.APP_NAME,
                           value.get("from_email") or value.get("username") or f"no-reply@{value['host']}")),
        reply_to=value.get("reply_to"), timeout=settings.SMTP_TIMEOUT_SECONDS,
        portal_url=value.get("portal_url"), include_password=value.get("include_password", True),
    )


# --- delivery ---------------------------------------------------------------------------------------------------

def deliver(config: SmtpConfig, *, to: str, subject: str, text: str, html_body: str | None = None) -> None:
    """Send one message now; raises on failure (callers decide whether that matters)."""
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = config.sender, to, subject
    if config.reply_to:
        message["Reply-To"] = config.reply_to
    message.set_content(text)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    if config.security == "ssl":
        client = smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout, context=context)
    else:
        client = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
    with client:
        if config.security == "starttls":
            client.starttls(context=context)
        if config.username:
            client.login(config.username, config.password or "")
        client.send_message(message)


# The latest deliveries of this process (newest first), shown on the Settings page: the answer to "it didn't arrive".
RECENT: deque[dict[str, Any]] = deque(maxlen=50)


def _deliver_quietly(config: SmtpConfig, to: str, subject: str, text: str, html_body: str | None) -> None:
    entry = {"at": datetime.now(UTC).isoformat(), "to": to, "subject": subject, "ok": False, "error": None}
    try:
        deliver(config, to=to, subject=subject, text=text, html_body=html_body)
        entry["ok"] = True
        logger.info("Email %r sent to %s.", subject, to)
    except (OSError, smtplib.SMTPException) as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("Could not send email %r to %s: %s", subject, to, exc)
    RECENT.appendleft(entry)


def _not_configured(settings: Settings, to: str, subject: str, text: str) -> None:
    RECENT.appendleft({"at": datetime.now(UTC).isoformat(), "to": to, "subject": subject, "ok": False,
                       "error": "No mail server is set up (or email is switched off)."})
    if settings.APP_ENV is AppEnv.PRODUCTION:
        logger.info("No mail server is set up: email %r to %s was not sent.", subject, to)
    else:
        logger.warning("No mail server is set up, so this email was not sent.\nTo: %s\nSubject: %s\n\n%s",
                       to, subject, text)


def brand_for(session: Session, settings: Settings, organization_id: uuid.UUID | None,
              config: SmtpConfig | None = None) -> Brand:
    """The name and colour an email carries: the person's bank, or the platform for Governix staff."""
    from app.modules.organizations.model import Organization

    org = session.get(Organization, organization_id) if organization_id else None
    return Brand.of(settings.APP_NAME, org, portal_for(session, settings, organization_id, config))


def send(session: Session, settings: Settings, event_key: str, *, to: str, subject: str, content: Content,
         organization_id: uuid.UUID | None = None) -> bool:
    """Send one email in the background if the mail server is set up and this kind of email is on. Call it after
    the change it describes is saved. Returns whether it was queued."""
    if not to:
        return False
    config = active_config(session, settings)
    brand = brand_for(session, settings, organization_id, config)
    text = render_text(content, brand, to)
    if config is None:
        _not_configured(settings, to, subject, text)
        return False
    if not config.wants(event_key):
        return False
    _POOL.submit(_deliver_quietly, config, to, subject, text, render_html(content, brand, to))
    return True


# --- where people sign in ---------------------------------------------------------------------------------------

def portal_for(session: Session, settings: Settings, organization_id: uuid.UUID | None,
               config: SmtpConfig | None = None) -> str | None:
    """The address a person signs in at: their bank's own port (or domain) on the portal's host, or the main
    portal (Master Admins, Governix support staff)."""
    config = config or active_config(session, settings)
    base = (config.portal_url if config else None) or _portal_default(settings)
    if organization_id is None or base is None:
        return base
    from app.modules.organizations.model import Organization

    org = session.get(Organization, organization_id)
    if org is None:
        return base
    if settings.NGINX_BASE_DOMAIN:
        return f"http://{org.slug}.{settings.NGINX_BASE_DOMAIN}"
    if org.allowed_port:
        parts = urlsplit(base)
        return f"{parts.scheme}://{parts.hostname}:{org.allowed_port}"
    return base


def reset_link(session: Session, settings: Settings, user, token: str) -> str:
    """The reset page of the person's own portal (their bank's port), never an address taken from the request."""
    portal = portal_for(session, settings, user.organization_id)
    base = f"{portal}/reset-password" if portal else settings.PASSWORD_RESET_URL
    return f"{base}?{urlencode({'token': token})}"


# --- account emails ---------------------------------------------------------------------------------------------

SECURITY_NOTE = ("If you did not expect this email, contact your administrator straight away. We will never ask "
                 "for your password or authenticator code.")


def _sign_in(session: Session, settings: Settings, user) -> tuple[str, str] | None:
    portal = portal_for(session, settings, user.organization_id)
    return ("Sign in", f"{portal}/login") if portal else None


def account_created(session: Session, settings: Settings, user, temporary_password: str | None) -> None:
    config = active_config(session, settings)
    show = bool(temporary_password) and (config is None or config.include_password)
    brand = brand_for(session, settings, user.organization_id, config)
    send(session, settings, "account_created", to=user.email, organization_id=user.organization_id,
         subject=f"Welcome to {brand.name}: your account is ready",
         content=Content(
             title="Your account is ready",
             preheader=f"Sign in to {brand.name} with your new account.",
             greeting=f"Dear {user.full_name},",
             paragraphs=[f"An account has been created for you on {brand.name}. Use the details below to sign in.",
                         "For your security, change the temporary password as soon as you sign in (account menu › "
                         "Change password), and set up two-step sign-in."
                         if show else "Your administrator will give you your temporary password separately."],
             details=[("Sign-in email", user.email), ("Role", _role_name(user.role))]
             + ([("Portal", brand.portal)] if brand.portal else []),
             secret=("Temporary password", temporary_password) if show else None,
             button=_sign_in(session, settings, user),
             notice=SECURITY_NOTE, tone="security",
         ))


def password_reset_by_admin(session: Session, settings: Settings, user, temporary_password: str) -> None:
    config = active_config(session, settings)
    show = config is None or config.include_password
    send(session, settings, "password_reset_by_admin", to=user.email, organization_id=user.organization_id,
         subject="Your password has been reset",
         content=Content(
             title="Your password has been reset",
             preheader="An administrator set a new temporary password for your account.",
             greeting=f"Dear {user.full_name},",
             paragraphs=["An administrator has reset the password of your account. For your security, every "
                         "session you had has been signed out.",
                         "Sign in with the temporary password below, then choose a new one straight away."
                         if show else "Your administrator will give you the new temporary password separately."],
             secret=("Temporary password", temporary_password) if show else None,
             button=_sign_in(session, settings, user),
             notice=SECURITY_NOTE, tone="security",
         ))


def two_step_reset(session: Session, settings: Settings, user) -> None:
    send(session, settings, "two_step_reset", to=user.email, organization_id=user.organization_id,
         subject="Two-step sign-in was turned off on your account",
         content=Content(
             title="Two-step sign-in turned off",
             preheader="An administrator turned off two-step sign-in on your account.",
             greeting=f"Dear {user.full_name},",
             paragraphs=["An administrator has turned off two-step sign-in on your account, for example after a "
                         "lost phone. Every session you had has been signed out.",
                         "Sign in with your password and set up your authenticator app again from the account menu "
                         "(Two-step sign-in)."],
             details=[("Account", user.email), ("Changed", _now_text())],
             button=_sign_in(session, settings, user),
             notice="If you did not ask for this, contact your administrator now: your account is protected by "
                    "your password only until two-step sign-in is set up again.", tone="alert",
         ))


def password_changed(session: Session, settings: Settings, user) -> None:
    send(session, settings, "password_changed", to=user.email, organization_id=user.organization_id,
         subject="Your password was changed",
         content=Content(
             title="Your password was changed",
             preheader="The password of your account was just changed.",
             greeting=f"Dear {user.full_name},",
             paragraphs=["The password of your account was changed successfully, and every session was signed "
                         "out. You can now sign in with your new password."],
             details=[("Account", user.email), ("Changed", _now_text())],
             button=_sign_in(session, settings, user),
             notice="If you did not change your password, contact your administrator immediately.", tone="alert",
         ))


def forgot_password(session: Session, settings: Settings, user, link: str) -> None:
    send(session, settings, "forgot_password", to=user.email, organization_id=user.organization_id,
         subject="Reset your password",
         content=Content(
             title="Reset your password",
             preheader=f"Use this link within {settings.PASSWORD_RESET_TTL_MINUTES} minutes to choose a new password.",
             greeting=f"Dear {user.full_name},",
             paragraphs=["We received a request to reset the password of your account. Select the button below "
                         "to choose a new one."],
             details=[("Account", user.email), ("Link valid for", f"{settings.PASSWORD_RESET_TTL_MINUTES} minutes, "
                                                                  "and works once")],
             button=("Reset my password", link),
             notice="If you did not ask for a password reset, ignore this email: your password stays the same and "
                    "no one can use this link after it expires.", tone="security",
         ))


def test_message(settings: Settings, config: SmtpConfig, to: str) -> tuple[str, str, str]:
    """(subject, text, html) of the Settings page's test email."""
    brand = Brand.of(settings.APP_NAME, None, config.portal_url)
    content = Content(
        title="Your mail server works",
        preheader="This is a test email from the Settings page.",
        paragraphs=[f"This test email was sent from Settings › Email through {config.host}:{config.port}. "
                    "If you can read it, Governix can send email: welcome emails, password resets and alerts."],
        details=[("Mail server", f"{config.host}:{config.port}"), ("Security", config.security.upper()),
                 ("Sender", config.sender)],
    )
    return f"{settings.APP_NAME} test email", render_text(content, brand, to), render_html(content, brand, to)


def _role_name(role: str) -> str:
    return {"master_admin": "Master Admin", "org_admin": "Organization Admin", "branch_manager": "Branch Manager",
            "department_user": "User", "support_agent": "Support Staff"}.get(str(role), str(role))


def _now_text() -> str:
    return datetime.now(UTC).strftime("%d %b %Y, %H:%M UTC")


# --- notification emails (after commit) -------------------------------------------------------------------------

_PENDING = "governix_pending_emails"
_PANEL_PATHS = ("/masteradmin", "/organizationadmin", "/branchmanager", "/user", "/supportstaff")


def queue_notification_emails(session: Session, settings: Settings, type_: str, user_ids: list[uuid.UUID],
                              title: str, body: str | None, link: str | None) -> None:
    """Email copies of a notification just created for these users, sent when the transaction commits."""
    if not user_ids:
        return
    config = active_config(session, settings)
    if config is None or not config.wants(str(type_)):
        return
    from app.modules.users.model import User

    people = session.execute(select(User.email, User.full_name, User.organization_id).where(
        User.id.in_(user_ids), User.is_active, User.deleted_at.is_(None))).all()
    pending: list[dict[str, Any]] = session.info.setdefault(_PENDING, [])
    alert = str(type_) in ("SECURITY_ALERT", "AI_THRESHOLD", "TICKET_SLA")
    for address, name, organization_id in people:
        brand = brand_for(session, settings, organization_id, config)
        url = (f"{brand.portal}{link}" if brand.portal and link and link.startswith(_PANEL_PATHS)
               else f"{brand.portal}/" if brand.portal else None)
        content = Content(
            title=title, preheader=(body or title)[:140], greeting=f"Dear {name},",
            paragraphs=[body] if body else [],
            button=(f"Open in {brand.name}", url) if url else None,
            notice=("You receive this because it concerns your account or your work. The same notice is in your "
                    "notifications when you sign in."), tone="alert" if alert else "info",
        )
        pending.append({"config": config, "to": address, "subject": title[:200],
                        "text": render_text(content, brand, address), "html": render_html(content, brand, address)})


@event.listens_for(Session, "after_commit")
def _send_pending(session: Session) -> None:
    for item in session.info.pop(_PENDING, []):
        _POOL.submit(_deliver_quietly, item["config"], item["to"], item["subject"], item["text"], item["html"])


@event.listens_for(Session, "after_rollback")
def _drop_pending(session: Session) -> None:
    session.info.pop(_PENDING, None)
