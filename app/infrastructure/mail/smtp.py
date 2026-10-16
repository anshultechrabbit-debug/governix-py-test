"""Outgoing email over SMTP (SMTP_* settings).

Sending never raises: it runs after the response (a background task), where a failure can only be
logged. Without SMTP_HOST nothing is sent; locally the message is logged so a developer can follow
the link, in production only the fact that it was not sent (a reset link is a credential).
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import AppEnv, Settings

logger = logging.getLogger(__name__)


def send_email(settings: Settings, *, to: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST:
        if settings.APP_ENV is AppEnv.PRODUCTION:
            logger.error("SMTP_HOST is not set: email %r to %s was not sent.", subject, to)
        else:
            logger.warning("SMTP_HOST is not set, so this email was not sent.\nTo: %s\nSubject: %s\n\n%s",
                           to, subject, body)
        return

    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.SMTP_FROM, to, subject
    message.set_content(body)
    context = ssl.create_default_context()
    try:
        if settings.SMTP_SSL:
            client = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS, context=context
            )
        else:
            client = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS)
        with client:
            if settings.SMTP_STARTTLS and not settings.SMTP_SSL:
                client.starttls(context=context)
            if settings.SMTP_USERNAME:
                client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value()
                             if settings.SMTP_PASSWORD else "")
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send email %r to %s.", subject, to)
