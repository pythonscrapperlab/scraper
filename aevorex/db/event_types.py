"""
Canonical vocabulary for `price_history.event_type`.

`price_history.event` stores the source's own wording verbatim ("Listed",
"Sold (MLS)", "Listing Removed", ...) — that's the audit trail, and it must
stay untouched so a later backfill can be reconciled against
`raw_scrapes.raw_json`. But every consumer that wants to *reason* about an
event ("has this been relisted?", "how many price cuts?") then has to know
each platform's exact phrasing, which is how the motivated-seller scorer
ended up matching on `event == "relisted"` while Redfin was writing
`"Relisted"` — a silent, permanent False.

`event_type` is the normalized slug alongside it. Normalizers set it on
write; scorers read it and fall back to `canonical_event_type(event)` for
rows written before this column existed.

Redfin's 12 observed `eventDescription` values (verified against the live
`raw_scrapes` set) all map here. Anything unrecognized returns None rather
than being force-fit into an existing bucket — an unmapped event is a
prompt to extend this table, not something to guess at.
"""

import logging
from typing import Optional

logger = logging.getLogger("aevorex.db.event_types")

LISTED = "listed"
RELISTED = "relisted"
REDUCED = "reduced"
INCREASED = "increased"
# "Price Changed" with no way to tell the direction (no earlier priced event
# to compare against). Deliberately distinct from `reduced`: counting an
# unknown-direction change as a price cut would inflate every motivation
# signal built on it.
PRICE_CHANGED = "price_changed"
PENDING = "pending"
CONTINGENT = "contingent"
SOLD = "sold"
REMOVED = "removed"
DELISTED = "delisted"
COMING_SOON = "coming_soon"
LISTED_FOR_RENT = "listed_for_rent"
RENTAL_REMOVED = "rental_removed"

# Event types that mean "this listing left the market without selling".
# The strongest lead-gen trigger available in this dataset: an expired or
# withdrawn listing is an owner who wanted to sell and couldn't.
OFF_MARKET_TYPES = frozenset({REMOVED, DELISTED})

# Types that carry no price by design on Redfin (verified: 100% null price
# across 32,558 events). They exist to record a state transition, which is
# exactly why `price_history.price` cannot be NOT NULL.
STATE_CHANGE_TYPES = frozenset({REMOVED, DELISTED, PENDING, CONTINGENT, RELISTED})

# Rental-market events. Redfin files these in the same history as sale
# events, and their prices are monthly rents — averaging ~$4,855 against
# ~$674,901 for a sale listing. Any sale-price arithmetic (original list
# price, price-cut percentage, comps) must exclude them, or a $2,800 rent
# sits in the same series as a $630,000 list price. 19.6% of properties in
# the live set carry both kinds.
RENTAL_EVENT_TYPES = frozenset({LISTED_FOR_RENT, RENTAL_REMOVED})

# Events that open (or reopen) a sale listing cycle. Used to decide which
# market a directionless "Price Changed" belongs to — see
# `RedfinNormalizer._resolve_price_change_directions`.
SALE_CYCLE_TYPES = frozenset({LISTED, RELISTED, COMING_SOON, SOLD, PENDING, CONTINGENT})

# Exact matches first — these also cover the legacy slugs that already exist
# in code/fixtures ("listed" / "reduced" / "relisted"), so a canonicalized
# value passed back through this function is stable.
_EXACT = {
    "listed": LISTED,
    "listed for sale": LISTED,
    "new listing": LISTED,
    "relisted": RELISTED,
    "reduced": REDUCED,
    "increased": INCREASED,
    "price changed": PRICE_CHANGED,
    "pending": PENDING,
    "contingent": CONTINGENT,
    "sold": SOLD,
    "sold (mls)": SOLD,
    "sold (public records)": SOLD,
    "listing removed": REMOVED,
    "delisted": DELISTED,
    "coming soon": COMING_SOON,
    "listed for rent": LISTED_FOR_RENT,
    "rental removed": RENTAL_REMOVED,
}

# Substring fallbacks, evaluated in order — order matters, since several of
# these are prefixes/suffixes of each other ("listed for rent" vs "listed",
# "rental removed" vs "listing removed").
_SUBSTRING_RULES = (
    ("rent", ("listed", "for rent"), LISTED_FOR_RENT),
    ("rental", ("removed", "delisted"), RENTAL_REMOVED),
    ("relist", (), RELISTED),
    ("delist", (), DELISTED),
    ("withdraw", (), REMOVED),
    ("off market", (), REMOVED),
    ("removed", (), REMOVED),
    ("coming soon", (), COMING_SOON),
    ("pending", (), PENDING),
    ("contingent", (), CONTINGENT),
    ("backup", (), CONTINGENT),
    ("sold", (), SOLD),
    ("reduc", (), REDUCED),
    ("increase", (), INCREASED),
    ("price change", (), PRICE_CHANGED),
    ("price", ("chang",), PRICE_CHANGED),
    ("listed", (), LISTED),
)


def canonical_event_type(description: Optional[str]) -> Optional[str]:
    """
    Map a source's raw event wording to a canonical slug, or None.

    Never raises and never guesses: an unrecognized description is logged
    once at debug level (these arrive in bulk, one per event per scrape)
    and returns None, leaving `event_type` NULL while the verbatim wording
    is still preserved in `event`.
    """
    if not description or not isinstance(description, str):
        return None

    normalized = " ".join(description.strip().lower().split())
    if not normalized:
        return None

    exact = _EXACT.get(normalized)
    if exact:
        return exact

    for needle, also_required, slug in _SUBSTRING_RULES:
        if needle not in normalized:
            continue
        if also_required and not any(extra in normalized for extra in also_required):
            continue
        return slug

    logger.debug("Unmapped price-history event description: %r", description)
    return None


def resolve_price_change_direction(
    event_type: Optional[str], price: Optional[int], previous_price: Optional[int]
) -> Optional[str]:
    """
    Refine a bare `price_changed` into `reduced` / `increased`.

    Redfin's payload says "Price Changed" without a direction, so direction
    has to come from the event immediately before it in that listing's own
    chronology (`previous_price`). Without a prior priced event to compare
    against, the type stays `price_changed` — see the note on PRICE_CHANGED
    above for why that isn't rounded down to "reduced".
    """
    if event_type != PRICE_CHANGED:
        return event_type
    if price is None or previous_price is None:
        return PRICE_CHANGED
    if price < previous_price:
        return REDUCED
    if price > previous_price:
        return INCREASED
    return PRICE_CHANGED
