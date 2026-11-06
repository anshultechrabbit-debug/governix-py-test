"""Why an authenticator code is refused: the message must say what to do (auth/mfa.py)."""

import pyotp

from app.modules.auth import mfa

SECRET = pyotp.random_base32(32)
NOW = 1_800_000_000.0  # a fixed moment, so the steps are known
STEP = int(NOW // 30)


def code(step: int, secret: str = SECRET) -> str:
    return pyotp.TOTP(secret).at(step * 30)


def why(entered: str, last_step: int | None = None) -> str:
    return mfa.refusal(SECRET, entered, last_step=last_step, email="riya@bank.com", now=NOW)


def test_a_current_code_is_accepted_once():
    assert mfa.accepted_step(SECRET, code(STEP), last_step=None, now=NOW) == STEP
    assert mfa.accepted_step(SECRET, code(STEP), last_step=STEP, now=NOW) is None
    assert "already used" in why(code(STEP), last_step=STEP)


def test_a_phone_clock_that_is_off_is_named():
    assert mfa.accepted_step(SECRET, code(STEP - 10), last_step=None, now=NOW) is None
    assert "clock is about 5 minutes behind" in why(code(STEP - 10))
    assert "ahead" in why(code(STEP + 4))


def test_a_code_from_an_old_entry_points_to_the_newest_entry():
    old_secret = pyotp.random_base32(32)
    message = why(code(STEP, old_secret))
    assert "newest Governix entry for riya@bank.com" in message


def test_backup_codes_and_malformed_input():
    assert "backup code" in why("k7mq-x3vp-9t2n")
    assert "6-digit" in why("12345")
