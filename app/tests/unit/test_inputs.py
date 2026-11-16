"""The shared input rules (app/core/inputs.py)."""

from typing import Annotated

import pytest
from pydantic import BaseModel, Field, ValidationError

from app.core.inputs import (
    AtMost, Line, MaybeText, Name, NewPassword, PersonName, Phone, Tag, Text, Website, password_problems,
)
from app.core.security import generate_temporary_password


class Form(BaseModel):
    person: PersonName | None = Field(default=None, min_length=2, max_length=200)
    name: Name | None = Field(default=None, min_length=2, max_length=200)
    line: Line | None = None
    text: Text | None = Field(default=None, min_length=3, max_length=50)
    note: Annotated[MaybeText, AtMost(50)] = None
    phone: Phone = None
    website: Website = None
    tags: list[Tag] = []
    password: NewPassword | None = None


def message(**data) -> str:
    with pytest.raises(ValidationError) as caught:
        Form(**data)
    return caught.value.errors()[0]["msg"]


def test_passwords_need_every_kind_of_character():
    assert Form(password="Strong#Pass2026").password == "Strong#Pass2026"
    assert "capital letter" in message(password="strong#pass2026")
    assert "small letter" in message(password="STRONG#PASS2026")
    assert "digit" in message(password="Strong#Password")
    assert "symbol" in message(password="StrongPass2026x")
    assert "4 or more times" in message(password="Strong#Paaaass26")
    assert "at least 12" in message(password="Sh0rt#")
    assert password_problems("Anshul#Strong2026", email="anshul@bank.com") == ["not contain your email address"]
    assert password_problems("Riya#Strong-2026", name="Riya Sharma") == ["not contain your name"]


def test_generated_passwords_meet_the_policy():
    for _ in range(200):
        assert password_problems(generate_temporary_password()) == []


def test_names_and_text_are_cleaned_then_checked():
    assert Form(person="  Anne-Marie  O'Neil ").person == "Anne-Marie O'Neil"
    assert "only letters" in message(person="Agent 007")
    assert Form(name="  Mumbai   Main Branch (HO) ").name == "Mumbai Main Branch (HO)"
    assert "< or >" in message(name="<script>alert(1)</script>")
    assert "letter or digit" in message(name="---")
    assert "one line" in message(line="first\nsecond")
    assert Form(text="  line one\r\nline two  ").text == "line one\nline two"
    assert "at least 3" in message(text="    ")
    assert Form(note="   ").note is None  # an empty optional box is "not given"


def test_phones_websites_and_tags():
    assert Form(phone="+91 98765 43210").phone == "+91 98765 43210"
    assert Form(phone="  ").phone is None
    assert "7 to 15 digits" in message(phone="12345")
    assert "only at the start" in message(phone="91+98765 43210")
    assert "only digits" in message(phone="call me")
    assert Form(website="https://www.bank.com").website == "https://www.bank.com"
    assert "http" in message(website="www.bank.com")
    assert "commas" in message(tags=["sql, python"])


def test_optional_boxes_left_empty_are_not_given():
    """A blank or null optional value is None, never measured: max_length on None was a 500 (POST /organizations
    with an empty address, phone or website)."""
    for empty in ("", "   ", None):
        form = Form(note=empty, phone=empty, website=empty)
        assert (form.note, form.phone, form.website) == (None, None, None)
    assert "at most 50 characters" in message(note="x" * 51)
    assert Form(note="  " + "x" * 50 + "  ").note == "x" * 50  # measured after trimming
    assert "at most 30 characters" in message(phone="1" * 31)
    assert Form.model_json_schema()["properties"]["note"]["maxLength"] == 50
