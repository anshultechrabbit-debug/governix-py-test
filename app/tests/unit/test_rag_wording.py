"""Wording that names nothing never decides the answer (v75): plain words may frame, terms never do."""
from types import SimpleNamespace

import pytest

from app.modules.rag.service import _nearest_passages, _plain_word


@pytest.mark.parametrize("question, term, plain", [
    ("Tell me something about the mortgage policy", "something", True),
    ("Something about the mortgage policy?", "something", True),
    ("What documents have you consumed so far?", "consumed", True),
    ("What does FIU-IND require?", "fiu", False),
    ("What is the NRE deposit rate?", "nre", False),
    ("What is the rate for car loans?", "car", False),  # names which loans
    ("What is the rate on loans for a car?", "car", True),  # left to the model's judgement, as before
])
def test_only_a_plain_word_no_document_uses_may_frame_a_question(question, term, plain):
    terms = {"something", "mortgage", "consumed", "far", "fiu", "nre", "deposit", "rate", "car", "loans"}
    assert _plain_word(question, term, terms) is plain


def test_the_nearest_passages_are_headings_with_the_start_of_their_text():
    def item(path, text):
        return SimpleNamespace(source=SimpleNamespace(section_path=path), candidate=SimpleNamespace(text=text))
    evidence = SimpleNamespace(items=[
        item("Home Loan > Charges > Foreclosure", "A borrower may   foreclose the loan after 6 EMIs."),
        item("Contents", "1. Purpose 2. Scope"),
        item("Home Loan > Charges > Foreclosure", "A borrower may foreclose the loan after 6 EMIs."),
    ])
    assert _nearest_passages(evidence) == ["Charges > Foreclosure: A borrower may foreclose the loan after 6 EMIs."]
