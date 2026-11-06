"""Input rules shared by every request schema.

Each type cleans the value first (trims, collapses runs of spaces in one-line fields), then checks it, and refuses
with a message a person can act on ("must include a capital letter (A-Z)"). Lengths stay on each field
(`Field(min_length=..., max_length=...)`) and are measured after the cleaning, so "   " never counts as text.

    PersonName   letters (any language), spaces, apostrophes, hyphens and dots; at least 2 letters
    Name         names and titles of things (banks, branches, policies, tickets): one line, at least one letter or
                 digit, no < or >
    Line         any other one-line value (issuer, owner, labels, numbers): one line, no control characters
    Text         descriptions, notes, messages, reasons, questions: line breaks allowed, not blank
    Phone        digits with + ( ) - . and spaces; 7-15 digits; + only at the start
    Website      an http:// or https:// address without spaces
    Tag          a skill or keyword: one line, 1-50 characters, no < or > or commas
    NewPassword  the password policy (`password_problems`); applies whenever a password is set, never at sign-in
"""

import re
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field
from pydantic_core import PydanticCustomError

PASSWORD_MIN = 12
PASSWORD_MAX = 128
PASSWORD_SYMBOLS = "!@#$%^&*()-_=+[]{};:'\",.<>/?\\|`~"

_CONTROL_LINE = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps tab, line feed, carriage return
_REPEAT = re.compile(r"(.)\1{3,}")
_WEBSITE = re.compile(r"^https?://[^\s/$.?#][^\s]*\.[^\s]+$", re.I)


def _fail(kind: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(kind, message)


# --- cleaning (before the length checks) --------------------------------------------------------------------------

def _one_line(value):
    if not isinstance(value, str):
        return value
    if _CONTROL_LINE.search(value.replace("\t", " ")):
        raise _fail("one_line", "must be one line, without line breaks or control characters")
    return " ".join(value.split())


def _text(value):
    if not isinstance(value, str):
        return value
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if _CONTROL_TEXT.search(value):
        raise _fail("control_characters", "must not contain control characters")
    return value.strip()


def _blank_is_none(value):
    if isinstance(value, str) and not value.strip():
        return None
    return value


# --- checks (after) -----------------------------------------------------------------------------------------------

def _no_angle(value: str) -> str:
    if "<" in value or ">" in value:
        raise _fail("angle_brackets", "must not contain < or >")
    return value


def _name(value: str) -> str:
    if not any(ch.isalnum() for ch in value):
        raise _fail("name", "must contain at least one letter or digit")
    return _no_angle(value)


def _person(value: str) -> str:
    if any(not (ch.isalpha() or ch in " .'-") for ch in value):
        raise _fail("person_name", "may contain only letters, spaces, apostrophes ('), hyphens (-) and dots (.)")
    if sum(ch.isalpha() for ch in value) < 2:
        raise _fail("person_name", "must contain at least 2 letters")
    return value


def _not_blank(value: str) -> str:
    if not value:
        raise _fail("blank", "must not be blank")
    return value


def _phone(value: str | None) -> str | None:
    if value is None:
        return None
    if re.search(r"[^0-9+()\-. ]", value):
        raise _fail("phone", "may contain only digits, spaces and + ( ) - .")
    if "+" in value[1:]:
        raise _fail("phone", "may have + only at the start")
    digits = sum(ch.isdigit() for ch in value)
    if not 7 <= digits <= 15:
        raise _fail("phone", "must have 7 to 15 digits")
    return value


def _website(value: str | None) -> str | None:
    if value is None:
        return None
    if not _WEBSITE.match(value):
        raise _fail("website", "must be a web address starting with http:// or https://, e.g. https://www.bank.com")
    return value


def _tag(value: str) -> str:
    if "," in value:
        raise _fail("tag", "must be one item without commas (add each as its own entry)")
    return _no_angle(_not_blank(value))


def password_problems(password: str, *, email: str | None = None, name: str | None = None) -> list[str]:
    """What a new password lacks, in the order a person would fix it. Empty when it meets the policy."""
    problems = []
    if len(password) < PASSWORD_MIN:
        problems.append(f"be at least {PASSWORD_MIN} characters long")
    if len(password) > PASSWORD_MAX:
        problems.append(f"be at most {PASSWORD_MAX} characters long")
    if not any(ch.isupper() for ch in password):
        problems.append("include a capital letter (A-Z)")
    if not any(ch.islower() for ch in password):
        problems.append("include a small letter (a-z)")
    if not any(ch.isdigit() for ch in password):
        problems.append("include a digit (0-9)")
    if not any(not ch.isalnum() and not ch.isspace() for ch in password):
        problems.append("include a symbol such as @ # $ % & * !")
    if password != password.strip():
        problems.append("not start or end with a space")
    if _CONTROL_LINE.search(password):
        problems.append("not contain line breaks or control characters")
    if _REPEAT.search(password):
        problems.append("not repeat the same character 4 or more times in a row")
    lowered = password.lower()
    if email:
        local = email.split("@")[0].lower()
        if len(local) >= 3 and local in lowered:
            problems.append("not contain your email address")
    if name:
        for part in re.split(r"[\s.'-]+", name.lower()):
            if len(part) >= 3 and part in lowered:
                problems.append("not contain your name")
                break
    return problems


def _password(value: str) -> str:
    problems = password_problems(value)
    if problems:
        raise _fail("password_policy", "must " + "; ".join(problems))
    return value


def check_password(password: str, *, email: str | None, name: str | None) -> None:
    """The checks that need the account (its name and email). Raises the API's ValidationError."""
    from app.core.exceptions import ValidationError

    problems = password_problems(password, email=email, name=name)
    if problems:
        raise ValidationError("The password must " + "; ".join(problems) + ".", code="WEAK_PASSWORD")


def _rules(*rules: str):
    """The rules as they appear in the API documentation (and the validation reference PDF)."""
    return Field(json_schema_extra={"x-rules": list(rules)})


_PASSWORD_RULES = (f"{PASSWORD_MIN}-{PASSWORD_MAX} characters", "a capital letter (A-Z)", "a small letter (a-z)",
                   "a digit (0-9)", "a symbol such as @ # $ % & * !", "no space at the start or end",
                   "no character 4 or more times in a row", "must not contain the person's name or email")

PersonName = Annotated[str, BeforeValidator(_one_line), AfterValidator(_person),
                       _rules("letters, spaces, apostrophes, hyphens and dots only", "at least 2 letters",
                              "extra spaces removed")]
Name = Annotated[str, BeforeValidator(_one_line), AfterValidator(_name),
                 _rules("one line", "at least one letter or digit", "no < or >", "extra spaces removed")]
Line = Annotated[str, BeforeValidator(_one_line), AfterValidator(_not_blank),
                 _rules("one line, no control characters", "not blank", "extra spaces removed")]
Text = Annotated[str, BeforeValidator(_text), AfterValidator(_not_blank),
                 _rules("line breaks allowed, no control characters", "not blank", "trimmed")]
Phone = Annotated[str | None, BeforeValidator(_blank_is_none), BeforeValidator(_one_line),
                  Field(max_length=30), AfterValidator(_phone),
                  _rules("digits, spaces and + ( ) - . only", "7-15 digits", "+ only at the start", "blank = none")]
Website = Annotated[str | None, BeforeValidator(_blank_is_none), BeforeValidator(_one_line),
                    Field(max_length=300), AfterValidator(_website),
                    _rules("starts with http:// or https://", "no spaces", "blank = none")]
Tag = Annotated[str, BeforeValidator(_one_line), Field(min_length=1, max_length=50), AfterValidator(_tag),
                _rules("one item, no commas", "no < or >")]
NewPassword = Annotated[str, Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX), AfterValidator(_password),
                        _rules(*_PASSWORD_RULES)]


def _maybe(check):
    return lambda value: None if value is None else check(value)


# Optional variants: an empty or blank value means "not given" (forms send "" for an empty box).
MaybeName = Annotated[str | None, BeforeValidator(_blank_is_none), BeforeValidator(_one_line),
                     AfterValidator(_maybe(_name)),
                     _rules("one line", "at least one letter or digit", "no < or >", "blank = not given")]
MaybeLine = Annotated[str | None, BeforeValidator(_blank_is_none), BeforeValidator(_one_line),
                     _rules("one line, no control characters", "extra spaces removed", "blank = not given")]
MaybeText = Annotated[str | None, BeforeValidator(_blank_is_none), BeforeValidator(_text),
                     _rules("line breaks allowed, no control characters", "trimmed", "blank = not given")]
