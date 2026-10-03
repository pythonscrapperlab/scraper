"""Redaction of contact details in listing text (E6 step 5, closes audit 06 security 1).

The corpus below reproduces the patterns found in the real listing descriptions (survey of
12,981 live rows: 11 contain phone numbers; none an e-mail; ~1,100 use call/text/contact wording,
almost all generic) with invented names and numbers, so no real person's data is committed.
``scripts/check_redaction.py`` runs the same assertions against the live database.
"""

from __future__ import annotations

import re

import pytest

from aevorex.publisher.redact import cap_text, redact_text, serving_text

# (text, substrings that must NOT survive)
LEAKS: list[tuple[str, list[str]]] = [
    ("Great starter home. Call 407-555-0142 for a showing.", ["407-555-0142", "555-0142"]),
    ("Call (407) 555-0142 today!", ["555-0142", "(407)"]),
    ("Text 407.555.0142 anytime.", ["555.0142"]),
    ("Reach me at 407 555 0142 or by email.", ["555 0142"]),
    ("Phone: +1 407 555 0142", ["555 0142"]),
    ("Contact 4075550142 to schedule.", ["4075550142"]),
    ("Questions? 1-800-555-0199 ext 4.", ["800-555-0199"]),
    ("Email jane.doe@examplerealty.com for the disclosure package.", ["jane.doe", "examplerealty.com"]),
    ("Send offers to offers+orlando@broker-example.co please.", ["offers+orlando", "broker-example"]),
    ("Visit www.example-homes.com/listing/123 for the tour.", ["example-homes", "listing/123"]),
    ("Full details at https://tour.example.net/abc?x=1.", ["tour.example.net", "abc?x=1"]),
    ("Follow @orlandohomesby_maria for more.", ["@orlandohomesby_maria", "orlandohomesby"]),
    ("Please text Kenia to schedule an appointment.", ["Kenia"]),
    ("ITEM CALL ANA FOR SHOWING.", ["ANA"]),
    ("Call Maria Gonzalez at 305-555-0111 to see it.", ["Maria", "Gonzalez", "555-0111"]),
    ("Contact Mr. Robert Smith for details.", ["Robert", "Smith"]),
    ("Ask for Dennis. Easy to show.", ["Dennis"]),
    ("Speak with Priya Patel about financing.", ["Priya", "Patel"]),
    ("Call Tom or text Lisa Wong 727-555-0123.", ["Tom", "Lisa", "Wong", "555-0123"]),
    ("For access call Carlos O'Neil 813-555-0188 or email carlos@example.org", ["Carlos", "O'Neil", "555-0188", "carlos@"]),
    ("Contact Anna Today for more information.", ["Anna"]),
    ("Listed by Sunrise Realty. Call Dave 561-555-0166.", ["Dave", "555-0166"]),
]

# Generic wording that carries no personal data must survive untouched.
BENIGN: list[str] = [
    "A wonderful place to call home.",
    "Perfect Place To Call Home! Easy reach of shopping.",
    "Call the listing agent to schedule your private showing.",
    "Please call listing agent for details.",
    "Contact us today for an information sheet!",
    "Contact the listing agent for details.",
    "Pay $1,250,000 cash; 3 bed, 2 bath, 1,800 sq ft on 0.25 acres, built in 1998.",
    "MLS 20240001234 lot 12 block 5. Taxes $4,512 per year.",
    "Walk to the beach, 5 minutes to I-95 and 15 minutes to downtown.",
    "Easy to show. Ask for video walkthrough.",
]


@pytest.mark.parametrize(("text", "forbidden"), LEAKS)
def test_contact_details_are_removed(text: str, forbidden: list[str]) -> None:
    redacted = redact_text(text)
    assert redacted is not None
    for fragment in forbidden:
        assert fragment not in redacted, (fragment, redacted)


@pytest.mark.parametrize("text", BENIGN)
def test_generic_wording_is_left_alone(text: str) -> None:
    assert redact_text(text) == text


def test_thirty_descriptions_have_no_residue() -> None:
    corpus = [text for text, _ in LEAKS]
    assert len(corpus) + len(BENIGN) >= 30
    phone = re.compile(r"\d{3}[\s.\-)]*\d{3}[\s.\-]*\d{4}")
    for text in corpus:
        redacted = redact_text(text) or ""
        assert not phone.search(redacted)
        assert "@" not in redacted
        assert "http" not in redacted.lower()
        assert "www." not in redacted.lower()


def test_none_and_empty() -> None:
    assert redact_text(None) is None
    assert serving_text(None, 600) is None
    assert serving_text("   ", 600) is None


def test_redaction_runs_before_the_cap() -> None:
    filler = "Bright open plan with updated kitchen and a large yard. " * 10
    text = filler[:590] + " Call 407-555-0142 now."
    out = serving_text(text, 600)
    assert out is not None and len(out) <= 600
    assert "555" not in out and "0142" not in out


def test_cap_cuts_on_a_word_boundary_and_never_exceeds_the_limit() -> None:
    text = ("word " * 400).strip()
    out = cap_text(text, 600)
    assert out is not None
    assert len(out) <= 600
    assert out.endswith("…")
    assert not out.rstrip("…").endswith("wor")  # no half word
    assert cap_text("short", 600) == "short"


def test_ai_summary_gets_the_same_redaction() -> None:
    summary = "Modern kitchen visible in photos. Contact Dana Lee at dana@example.com."
    out = serving_text(summary, None)
    assert out is not None
    assert "Dana" not in out and "@" not in out
