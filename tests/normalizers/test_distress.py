"""
Tests for structured distress extraction.

The whole value of moving this out of free-text matching is precision, so
most of what's covered here is false positives — the failure mode that makes
a text-derived flag untrustworthy. Every negative case below is a real
phrasing that a naive `ILIKE '%keyword%'` matches and shouldn't.
"""

from datetime import datetime

from aevorex.normalizers.distress import extract_distress

# ---------------------------------------------------------------- positives


def test_foreclosure_auction_is_fully_extracted():
    d = extract_distress(
        "Foreclosure Auction Ends July 29, 2026 at 11:00 AM EST. "
        "Explore this as-is opportunity. Opening bid to be announced."
    )

    assert d["is_foreclosure"] is True
    assert d["is_auction"] is True
    assert d["is_as_is"] is True
    assert d["auction_date"] == datetime(2026, 7, 29)
    assert "Foreclosure" in d["distress_signals"]["is_foreclosure"]


def test_numeric_auction_date_format_is_parsed():
    d = extract_distress("Bank owned property. Auction ends 8/19/2026.")

    assert d["is_reo"] is True
    assert d["auction_date"] == datetime(2026, 8, 19)


def test_signals_are_read_from_ai_summary_too():
    d = extract_distress(None, "- Property is currently rented with an active lease.")

    assert d["is_tenant_occupied"] is True


def test_matched_phrases_are_recorded_for_audit():
    d = extract_distress("Estate sale, sold as is, cash only.")

    signals = d["distress_signals"]
    assert set(signals) == {"is_probate_or_estate", "is_as_is", "is_cash_only"}
    assert signals["is_cash_only"] == ["cash only"]


# ----------------------------------------------------------- false positives


def test_reo_does_not_match_inside_other_words():
    # "REO" is a substring of stereo, oreo and moreover. Word boundaries only.
    d = extract_distress("Stereo system included; moreover the kitchen is new.")

    assert d["is_reo"] is False


def test_vacant_land_is_not_a_vacant_house():
    assert extract_distress("Vacant land ready to build your dream home.")["is_vacant"] is False
    assert extract_distress("Home is vacant and easy to show.")["is_vacant"] is True


def test_as_is_requires_the_sale_term_not_a_comparison():
    assert extract_distress("As is typical of homes in this area, the lot is large.")["is_as_is"] is False
    assert extract_distress("Sold as is, with all faults.")["is_as_is"] is True


def test_negated_signals_are_not_flagged():
    d = extract_distress("This is not a short sale and not a foreclosure.")

    assert d["is_short_sale"] is False
    assert d["is_foreclosure"] is False


def test_rental_restriction_is_not_read_as_str_permission():
    # "no short-term rentals" contains "short-term rental" — the phrase that
    # would otherwise register as STR being allowed.
    d = extract_distress("NO SHORT-TERM RENTALS permitted by the HOA. 12 month minimum.")

    assert d["is_rental_restricted"] is True
    assert d["allows_short_term_rental"] is False


def test_str_permission_is_recognised_when_actually_granted():
    d = extract_distress("Short-term rentals allowed! Airbnb friendly building.")

    assert d["allows_short_term_rental"] is True
    assert d["is_rental_restricted"] is False


def test_no_short_term_rental_restrictions_means_permitted_not_prohibited():
    # A live listing reads "AIRBNB APPROVED. NO SHORT-TERM RENTAL
    # RESTRICTIONS." — one of the most STR-friendly listings in the set, and
    # the phrase is one word away from meaning the exact opposite.
    d = extract_distress("AIRBNB APPROVED. NO SHORT-TERM RENTAL RESTRICTIONS. Building presents well.")

    assert d["is_rental_restricted"] is False
    assert d["allows_short_term_rental"] is True


def test_no_restrictions_survives_an_intervening_word():
    d = extract_distress("No Community Rental Restrictions. Enjoy proximity to the beach.")

    assert d["allows_short_term_rental"] is True
    assert d["is_rental_restricted"] is False


def test_due_diligence_boilerplate_is_not_a_restriction():
    # "Buyer to verify ... rental restrictions" is a disclaimer present in a
    # large share of Florida listings; it says nothing about this property.
    d = extract_distress(
        "Buyer responsible to verify all dimensions, rental restrictions and pet restrictions."
    )

    assert d["is_rental_restricted"] is False


def test_a_disclaimer_does_not_silence_the_next_sentence():
    # The disclaimer guard reaches 90 characters, so it must stop at the
    # sentence boundary or it would swallow the real restriction here.
    d = extract_distress("Buyer to verify square footage. No short-term rentals allowed.")

    assert d["is_rental_restricted"] is True


def test_age_restriction_matches_the_plus_notation():
    # `\b` after a `+` never matches, since `+` is not a word character —
    # the pattern has to stop at the plus.
    assert extract_distress("Vibrant 55+ active adult community.")["is_age_restricted"] is True


# ------------------------------------------------------------- missing text


def test_no_text_yields_none_not_false():
    # "we looked and found nothing" and "we had nothing to look at" are
    # different facts; 0.6% of live properties have no description at all.
    d = extract_distress(None, "")

    assert d["is_foreclosure"] is None
    assert d["distress_signals"] is None
    assert d["auction_date"] is None


def test_text_with_no_signals_yields_false_not_none():
    d = extract_distress("Beautifully maintained three bedroom home with a new roof.")

    assert d["is_foreclosure"] is False
    assert d["distress_signals"] is None


def test_auction_date_is_only_set_when_it_is_an_auction():
    d = extract_distress("Open house ends July 29, 2026.")

    assert d["is_auction"] is False
    assert d["auction_date"] is None
