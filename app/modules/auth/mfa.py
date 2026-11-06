"""Two-step sign-in with an authenticator app: Google Authenticator, Microsoft Authenticator, Authy, ...

Time-based one-time codes (TOTP, RFC 6238): 6 digits, a new one every 30 seconds, from a secret the app scans
from a QR code. The server keeps that secret encrypted (Fernet, key from MFA_ENCRYPTION_KEY), accepts a code
from the step before or after the current one (phone clocks drift), and never the same step twice (a code
seen over someone's shoulder cannot be replayed). Backup codes are 10 random one-time codes, stored hashed.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import time

import pyotp
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

logger = logging.getLogger(__name__)

STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: read aloud or typed from paper


def _fernet(settings: Settings) -> Fernet:
    source = settings.MFA_ENCRYPTION_KEY or settings.JWT_SECRET
    key = hashlib.sha256(b"governix-mfa:" + source.get_secret_value().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def new_secret() -> str:
    return pyotp.random_base32(32)  # 160 bits, the size RFC 4226 recommends


def encrypt(settings: Settings, secret: str) -> str:
    return _fernet(settings).encrypt(secret.encode()).decode()


def decrypt(settings: Settings, stored: str | None) -> str | None:
    """The secret, or None when it cannot be read (the key changed): then only a backup code or an admin
    reset gets the person in."""
    if not stored:
        return None
    try:
        return _fernet(settings).decrypt(stored.encode()).decode()
    except InvalidToken:
        logger.error("An authenticator secret could not be decrypted: MFA_ENCRYPTION_KEY changed?")
        return None


def provisioning_uri(secret: str, *, email: str, issuer: str, label: str | None = None) -> str:
    """What the QR code holds: otpauth://totp/Issuer:email (label)?secret=...&issuer=Issuer. The label (the setup
    date) tells this entry apart from older ones in the app, which stop working when it is set up again."""
    name = f"{email} ({label})" if label else email
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS).provisioning_uri(name=name, issuer_name=issuer)


def accepted_step(secret: str, code: str, *, last_step: int | None, now: float | None = None) -> int | None:
    """The 30-second step this code belongs to, if it is valid now and newer than the last one accepted."""
    return check_code(secret, code, last_step=last_step, now=now)[0]


def check_code(secret: str, code: str, *, last_step: int | None, now: float | None = None) -> tuple[int | None, bool]:
    """(the accepted step, None if refused; whether it was refused only because it was already used)."""
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) != DIGITS or len(code.strip()) > DIGITS + 2:
        return None, False
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS)
    current = int((now if now is not None else time.time()) // STEP_SECONDS)
    reused = False
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), digits):
            if last_step is not None and step <= last_step:
                reused = True
                continue
            return step, False
    return None, reused


CLOCK_STEPS = 20  # 10 minutes either side: a code from there means the phone's clock is off, not a wrong code


def refusal(secret: str | None, code: str, *, last_step: int | None, email: str, now: float | None = None) -> str:
    """Why a code was refused, in words that tell the person what to do. Never accepts anything itself."""
    digits = "".join(ch for ch in code if ch.isdigit())
    if looks_like_recovery(code) and len(digits) != DIGITS:
        return "That backup code isn't valid, or it was already used. Each backup code works once."
    if len(digits) != DIGITS:
        return "Enter the 6-digit code your authenticator app shows for Governix."
    if secret is None:
        return ("Your authenticator can't be checked right now. Sign in with a backup code, or ask your admin to reset "
                "two-step sign-in.")
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS)
    current = int((now if now is not None else time.time()) // STEP_SECONDS)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), digits) and last_step is not None and step <= last_step:
            return "That code was already used. Wait for the next code in your app (up to 30 seconds) and enter it."
    for offset in range(-CLOCK_STEPS, CLOCK_STEPS + 1):
        if abs(offset) > DRIFT_STEPS and hmac.compare_digest(totp.at((current + offset) * STEP_SECONDS), digits):
            minutes = max(1, round(abs(offset) * STEP_SECONDS / 60))
            way = "behind" if offset < 0 else "ahead"
            return (f"Your phone's clock is about {minutes} minute{'s' if minutes != 1 else ''} {way}. Turn on automatic "
                    "date and time in your phone's settings, then enter the new code.")
    return (f"That code doesn't match. In your authenticator app, use the newest Governix entry for {email}: if two-step "
            "sign-in was set up more than once, older entries no longer work and can be deleted.")


def new_recovery_codes() -> list[str]:
    """Ten codes like "k7mq-x3vp-9t2n" (60 bits each)."""
    def one() -> str:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
    return [one() for _ in range(RECOVERY_CODE_COUNT)]


def normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def hash_recovery(code: str) -> str:
    return hashlib.sha256(normalize_recovery(code).encode()).hexdigest()


def looks_like_recovery(code: str) -> bool:
    return len(normalize_recovery(code)) == 12
