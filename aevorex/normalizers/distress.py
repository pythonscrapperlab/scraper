"""
Structured seller-distress and use-restriction signals extracted from listing text.

WHY THIS EXISTS
---------------
The classic basis for a motivated-seller list is ownership/legal data —
pre-foreclosure filings, tax liens, probate dockets, absentee owners. None of
that is in this schema, and the motivated-seller scorer says so in its own
docstring. What *is* available, and was being thrown away, is the listing
remarks: 44 properties in the live set are open foreclosure auctions, and the
only way to know that today is `description ILIKE '%foreclosure%'`.

Free-text matching at scoring time is the wrong place for this. It can't be
indexed, it can't be filtered in a query ("show me every REO in Broward"), it
re-runs on every scoring pass, and each scorer ends up with its own slightly
different keyword list that drifts apart. Extraction belongs at ingestion,
once, into typed columns.

WHAT EACH SIGNAL MEANS TO AN INVESTOR
-------------------------------------
These are not interchangeable "distress" synonyms — they behave differently
and several point in opposite directions depending on the strategy:

- `foreclosure_auction`  Sold at courthouse/online auction by the lender.
  Cash only, no inspection, no clean title guarantee, possible occupants and
  junior liens. The listed price is an opening bid or deposit, NOT market
  value — which is exactly why `price_is_placeholder` exists below.
- `reo`  Bank already owns it (auction failed). Now an ordinary MLS listing
  with an unemotional institutional seller who wants it off the books. The
  single best-quality motivated-seller signal here: real discount, normal
  closing process.
- `short_sale`  Owner owes more than it's worth; the lender must approve the
  sale. Genuinely motivated, but 60-180 day closings and the lender can walk.
  Motivated ≠ fast — a flipper's carrying-cost model must treat this
  differently from REO.
- `probate_or_estate`  Heirs selling inherited property. Typically
  out-of-state, emotionally detached, deferred maintenance, want a clean fast
  close. High-quality flip lead.
- `as_is`  Seller won't repair. Weak on its own (near-boilerplate in Florida)
  but meaningful combined with age and no recorded renovation.
- `tenant_occupied`  Sign flips by strategy: income already in place for
  buy-and-hold, but for a flip it means no access, no renovation, and a
  Florida eviction timeline before work can start.
- `vacant`  Positive for a flip (immediate access) and a carrying-cost
  pressure signal on the seller.
- `cash_only`  Usually means it won't finance — structural problems, no
  certificate of occupancy, unpermitted work, or a condo whose reserves or
  milestone inspection failed. Post-Surfside Florida, this is a serious
  condo-specific warning, not just a payment preference.
- `age_restricted`  55+ community. Ends any short-term-rental case outright
  and shrinks the resale pool.
- `rental_restricted`  HOA minimum-lease or no-lease rules. The only
  STR-legality signal available anywhere in this dataset, and the STR scorer
  currently ships with an empty regulatory table.

FALSE POSITIVES ARE THE REAL RISK
---------------------------------
Naive substring matching is what makes text extraction untrustworthy:
"REO" is inside "stereo" and "moreover"; "as is" is inside "as is typical of
this neighborhood"; "vacant" describes vacant *land* as often as a vacant
house; and "no short-term rentals permitted" contains "short-term rental".
Every pattern here is word-boundary anchored, and every match is checked
against a preceding-window negation guard. The matched phrases are returned
alongside the booleans so a human can audit any flag without re-reading the
listing.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Pattern, Sequence, Tuple

logger = logging.getLogger("aevorex.normalizers.distress")

# Words that flip the meaning of a match when they appear shortly before it.
# "not a foreclosure", "no short term rentals", "never tenant occupied".
_NEGATIONS = ("not ", "no ", "non-", "never ", "isn't ", "aren't ", "without ", "excluding ")

# How far back to look for a negation. Long enough to catch "this is not a
# short sale", short enough that an unrelated "no" earlier in the same
# sentence doesn't suppress a real match.
_NEGATION_WINDOW = 26

# Due-diligence boilerplate. "Buyer to verify all HOA information, fees,
# rental restrictions, and community regulations" is a disclaimer telling you
# nothing about this property — it is not evidence that rentals are
# restricted, and it appears in a large share of Florida listings.
_DISCLAIMERS = ("verify", "confirm", "check with", "buyer responsible", "due diligence")

# Wider than the negation window because these are long enumerations, but
# still clipped at the sentence boundary (see _preceding) so a disclaimer in
# one sentence can't silence a genuine restriction stated in the next.
_DISCLAIMER_WINDOW = 90

_SENTENCE_BOUNDARY = re.compile(r"[.!?\n]")


def _compile(*patterns: str) -> Tuple[Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# Each signal maps to the patterns that evidence it. Order within a tuple
# doesn't matter; every pattern is tested so the audit trail lists all matches.
_SIGNAL_PATTERNS: Dict[str, Tuple[Pattern, ...]] = {
    "is_foreclosure": _compile(
        r"\bforeclosur\w*\b",
        r"\bpre-?foreclosure\b",
        r"\blis\s+pendens\b",
        r"\btrustee'?s?\s+sale\b",
    ),
    "is_auction": _compile(
        r"\bauction\b",
        r"\bbidding\s+(?:opens|ends|begins)\b",
        r"\bopening\s+bid\b",
    ),
    "is_short_sale": _compile(
        r"\bshort\s+sale\b",
        r"\bsubject\s+to\s+lender\s+approval\b",
        r"\bthird[- ]party\s+approval\s+required\b",
    ),
    "is_reo": _compile(
        r"\bREO\b",
        r"\bbank[- ]owned\b",
        r"\blender[- ]owned\b",
        r"\breal\s+estate\s+owned\b",
        r"\bcorporate[- ]owned\b",
    ),
    "is_probate_or_estate": _compile(
        r"\bprobate\b",
        r"\bestate\s+sale\b",
        r"\bheirs?\b",
        r"\bpersonal\s+representative\b",
        r"\bdeceased\b",
    ),
    "is_as_is": _compile(
        # "as-is" hyphenated, or "as is" followed by a word that confirms it's
        # the sale term rather than the start of a comparison ("as is typical").
        r"\bas-is\b",
        r"\bas\s+is\s+(?:condition|where\s+is|with\s+all\s+faults|sale|basis)\b",
        r"\bsold\s+as\s+is\b",
        r"\bseller\s+will\s+not\s+(?:make\s+)?repair",
    ),
    "is_tenant_occupied": _compile(
        r"\btenant[- ]occupied\b",
        r"\blease\s+in\s+place\b",
        r"\bcurrently\s+(?:rented|leased)\b",
        r"\bactive\s+lease\b",
        r"\bexisting\s+tenant\b",
    ),
    "is_vacant": _compile(
        # Excludes "vacant land" / "vacant lot", which describe the parcel
        # type rather than an empty house awaiting a buyer.
        r"\bvacant\b(?!\s+(?:land|lot|parcel|acreage))",
        r"\bunoccupied\b",
        r"\beasy\s+to\s+show,?\s+vacant\b",
    ),
    "is_cash_only": _compile(
        r"\bcash\s+only\b",
        r"\bcash\s+offers?\s+only\b",
        r"\bno\s+financing\b",
        r"\bwill\s+not\s+(?:finance|qualify\s+for\s+financing)\b",
        r"\bnot\s+financeable\b",
    ),
    "is_age_restricted": _compile(
        # No trailing \b after the plus: `+` is a non-word character, so \b
        # there requires a word character immediately after it and "55+
        # community" would never match.
        r"\b55\s*\+",
        r"\b55\s+(?:and|&)\s+(?:older|over|up)\b",
        r"\bage[- ]restricted\b",
        r"\bactive\s+adult\s+community\b",
        r"\bsenior\s+community\b",
    ),
    "is_rental_restricted": _compile(
        # The lookahead is load-bearing: "NO SHORT-TERM RENTAL RESTRICTIONS"
        # means the opposite of "NO SHORT-TERM RENTALS", and without it the
        # STR-friendliest listings were being flagged as STR-prohibited.
        r"\bno\s+short[- ]term\s+rentals?\b(?!\s*restrictions?)",
        r"\bno\s+(?:rentals?|leasing)\s+(?:allowed|permitted)\b",
        r"\brental\s+restrictions?\b",
        r"\bminimum\s+(?:lease|rental)\b",
        r"\b(?:\d+|one|two|three|six|twelve)\s+month\s+minimum\b",
        r"\bno\s+airbnb\b",
    ),
    "allows_short_term_rental": _compile(
        r"\bshort[- ]term\s+rentals?\s+(?:are\s+)?(?:allowed|permitted|ok|welcome)\b",
        r"\bairbnb\s+(?:friendly|ready|allowed|permitted|approved)\b",
        r"\bvacation\s+rental\s+(?:allowed|permitted|income)\b",
        r"\bdaily\s+(?:rentals?|leasing)\s+(?:allowed|permitted)\b",
        # Optional intervening word covers "no COMMUNITY rental restrictions".
        r"\bno\s+(?:\w+\s+)?rental\s+restrictions?\b",
        r"\bno\s+short[- ]term\s+rental\s+restrictions?\b",
    ),
}

# Individual patterns whose own wording contains a negation ("no short-term
# rentals", "not financeable"). The negation guard must skip exactly these
# and nothing else.
#
# This is deliberately per-pattern rather than per-signal. Exempting a whole
# signal is what let "NO SHORT-TERM RENTALS permitted by the HOA" register as
# `allows_short_term_rental`: the permissive pattern matched the tail of the
# phrase, and the guard that would have caught the leading "NO" had been
# switched off for the entire signal because one of its *other* patterns
# ("no rental restrictions") needed the exemption.
_NEGATION_EXEMPT_PATTERNS = frozenset({
    r"\bno\s+short[- ]term\s+rentals?\b(?!\s*restrictions?)",
    r"\bno\s+(?:rentals?|leasing)\s+(?:allowed|permitted)\b",
    r"\bno\s+airbnb\b",
    r"\bno\s+financing\b",
    r"\bwill\s+not\s+(?:finance|qualify\s+for\s+financing)\b",
    r"\bnot\s+financeable\b",
    r"\bno\s+(?:\w+\s+)?rental\s+restrictions?\b",
    r"\bno\s+short[- ]term\s+rental\s+restrictions?\b",
    r"\bseller\s+will\s+not\s+(?:make\s+)?repair",
})

# "Foreclosure Auction Ends July 29, 2026 at 11:00 AM EST"
# "Auction ends 8/19/2026"
_AUCTION_DATE_PATTERNS = (
    re.compile(
        r"auction\s+(?:ends?|closes?|begins?|starts?)\s*(?:on\s+)?"
        r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"auction\s+(?:ends?|closes?|begins?|starts?)\s*(?:on\s+)?"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        re.IGNORECASE,
    ),
)

_AUCTION_DATE_FORMATS = ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%m/%d/%Y")


def _preceding(text: str, match_start: int, max_chars: int) -> str:
    """
    The text immediately before a match, clipped to the current sentence.

    Clipping matters in both directions: without it a "no" in the previous
    sentence silences a real signal, and a "buyer to verify..." disclaimer
    silences a restriction genuinely stated in the sentence after it.
    """
    window_start = max(0, match_start - max_chars)
    window = text[window_start:match_start]
    boundary = None
    for match in _SENTENCE_BOUNDARY.finditer(window):
        boundary = match.end()
    return (window[boundary:] if boundary is not None else window).lower()


def _is_suppressed(text: str, match_start: int) -> bool:
    """
    Whether a match should be discarded as negated or as boilerplate.

    Two separate guards with different reach — a negation sits right next to
    what it negates ("not a short sale"), while a disclaimer heads a long
    list ("buyer to verify all HOA information, fees, rental restrictions").
    """
    if any(n in _preceding(text, match_start, _NEGATION_WINDOW) for n in _NEGATIONS):
        return True
    return any(d in _preceding(text, match_start, _DISCLAIMER_WINDOW) for d in _DISCLAIMERS)


def _find_signal(text: str, patterns: Sequence[Pattern]) -> List[str]:
    """Return every non-negated matched phrase for one signal."""
    found: List[str] = []
    for pattern in patterns:
        guarded = pattern.pattern not in _NEGATION_EXEMPT_PATTERNS
        for match in pattern.finditer(text):
            if guarded and _is_suppressed(text, match.start()):
                continue
            phrase = match.group(0).strip()
            if phrase.lower() not in {f.lower() for f in found}:
                found.append(phrase)
    return found


def _parse_auction_date(text: str) -> Optional[datetime]:
    """Pull the auction end date out of the remarks, or None."""
    for pattern in _AUCTION_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).replace(",", ", ").replace("  ", " ").strip()
        raw = re.sub(r",\s*", ", ", raw)
        for fmt in _AUCTION_DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        logger.debug("Found an auction date phrase but couldn't parse it: %r", match.group(1))
    return None


def extract_distress(*texts: Optional[str]) -> Dict[str, Any]:
    """
    Extract structured distress / use-restriction signals from listing text.

    Pass every text field that might carry the signal (description first,
    then ai_summary) — they're concatenated and scanned together.

    Returns a dict of `is_*` booleans plus:
      - `auction_date`: parsed datetime, or None
      - `distress_signals`: {signal_name: [matched phrases]}, the audit trail

    Every boolean is None (not False) when there was no text to read at all.
    "We looked and found nothing" and "we had nothing to look at" are
    different facts, and the project's own convention is to flag gaps rather
    than guess — 0.6% of properties have no description.
    """
    combined = "\n".join(t for t in texts if t and t.strip())

    if not combined.strip():
        result: Dict[str, Any] = {name: None for name in _SIGNAL_PATTERNS}
        result["auction_date"] = None
        result["distress_signals"] = None
        return result

    matches: Dict[str, List[str]] = {}
    result = {}
    for name, patterns in _SIGNAL_PATTERNS.items():
        found = _find_signal(combined, patterns)
        result[name] = bool(found)
        if found:
            matches[name] = found

    result["auction_date"] = _parse_auction_date(combined) if result.get("is_auction") else None
    result["distress_signals"] = matches or None
    return result
