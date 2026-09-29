"""Redfin normalizer — maps Redfin API data to properties schema."""

import ast
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aevorex.db.deduplicator import Deduplicator
from aevorex.db.event_types import (
    PRICE_CHANGED,
    RENTAL_EVENT_TYPES,
    SALE_CYCLE_TYPES,
    canonical_event_type,
    resolve_price_change_direction,
)
from aevorex.normalizers.base import BaseNormalizer
from aevorex.normalizers.distress import extract_distress

logger = logging.getLogger("aevorex.normalizers.redfin")


class RedfinNormalizer(BaseNormalizer):
    """
    Normalize Redfin API response into properties schema.

    Maps Redfin-specific fields (redfin_id, avm_value, location_score, amenities, ...)
    to the unified schema: core Property columns in the top-level dict, everything
    else grouped under `meta` (unstructured cross-source fields) or under a satellite
    key (schools, comps, pois, transport_stops, location_score, features, price_history,
    tax_history, market_snapshot) for the pipeline to write to its own table after
    Deduplicator.upsert() returns the property_id.

    The scraper's parse() step doesn't guarantee clean types for every field — missing
    values can come through as '', epoch milliseconds, or ISO strings depending on
    which endpoint they came from. Every value that lands on a typed Property or
    satellite column goes through _to_int / _to_float / _to_naive_utc here, so junk
    becomes None instead of a raw DB insert error.
    """

    def __init__(self):
        """Initialize Redfin normalizer."""
        super().__init__()

    async def normalize(self, platform_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize Redfin data to properties schema.

        Args:
            platform_dict: Parsed Redfin API response

        Returns:
            Normalized dict matching properties schema, or None
        """
        try:
            redfin_id = platform_dict.get("redfin_id")
            address = platform_dict.get("street_address") or platform_dict.get("address")
            city = platform_dict.get("city")
            state = platform_dict.get("state")
            zip_code = platform_dict.get("zip_code") or platform_dict.get("zip")

            # Validate required fields
            if not all([address, city, state, zip_code]):
                return None

            basic_info = platform_dict.get("basic_info") or {}
            mls = platform_dict.get("mls") or {}
            amenities = platform_dict.get("amenities") or {}
            location_score = platform_dict.get("location_score") or {}

            apn = platform_dict.get("apn") or basic_info.get("apn")
            county = platform_dict.get("county")

            # Redfin hands us two different "status" values and they are not
            # interchangeable. `status` is the real MLS status (Active, Coming
            # Soon, Pending, Contingent- Accepting Backups, ...); `listing_status`
            # is schema.org's offers.availability, which is the string "InStock"
            # on essentially every listing and therefore carries no information.
            # The MLS one is authoritative here; the schema.org one is kept in
            # its own column rather than thrown away.
            mls_status = self._clean_str(platform_dict.get("status"))
            availability_status = self._clean_str(platform_dict.get("listing_status"))
            if not mls_status:
                logger.debug(
                    "No MLS status in payload for redfin_id=%s; leaving listing_status NULL "
                    "rather than falling back to the schema.org availability value.",
                    redfin_id,
                )

            offer_stats = self._extract_offer_insights(platform_dict.get("offer_insights"))

            lot_sqft = self._to_float(platform_dict.get("lot_sqft") or basic_info.get("lotSqFt"))
            lot_size_acres = round(lot_sqft / 43560, 4) if lot_sqft else None

            price = self._to_int(platform_dict.get("list_price")) or self._to_int(platform_dict.get("price"))
            sqft = self._to_int(platform_dict.get("sqft"))
            description = platform_dict.get("description") or None
            ai_summary = self._clean_str(platform_dict.get("ai_summary"))
            avm_value = self._to_float(platform_dict.get("avm_value"))
            tax_history = self._extract_tax_history(platform_dict)

            # Seller-distress / use-restriction signals, pulled out of the
            # remarks into typed columns so they can be queried and indexed
            # rather than re-matched as free text inside every scorer.
            # See aevorex/normalizers/distress.py for what each one means.
            distress = extract_distress(description, ai_summary)

            price_is_placeholder, quality_flags = self._detect_placeholder_price(
                price=price,
                sqft=sqft,
                avm_value=avm_value,
                tax_history=tax_history,
                is_auction=bool(distress.get("is_auction")),
            )

            normalized: Dict[str, Any] = {
                "redfin_id": redfin_id,
                # Normalized here (not just at upsert time) so a duplicate check done
                # earlier in the pipeline, before upsert() runs, still sees the
                # canonical form.
                "apn": Deduplicator.normalize_apn(apn) if apn else None,
                "county": county,
                "address": self.normalize_address(address),
                "city": city,
                # Uppercased on write: the same state has arrived as "FL", "Fl"
                # and "fl" from different pages, which silently splits every
                # state-scoped query and the APN dedup lookup.
                "state": self.normalize_state(state),
                "zip_code": self.normalize_zip(zip_code),
                "latitude": self._to_float(platform_dict.get("latitude")),
                "longitude": self._to_float(platform_dict.get("longitude")),
                "price": price,
                "bedrooms": self._to_int(platform_dict.get("beds")) or self._to_int(platform_dict.get("bedrooms")),
                "bathrooms": self._to_float(platform_dict.get("baths")) or self._to_float(platform_dict.get("bathrooms")),
                "sqft": sqft,
                "lot_size": lot_size_acres,
                "year_built": self._to_int(platform_dict.get("year_built")) or self._to_int(basic_info.get("yearBuilt")),
                "property_type": basic_info.get("propertyTypeName") or platform_dict.get("property_type") or None,
                # Verbatim MLS status — including Redfin's own mangled spacing
                # in "Contingent- Accepting Backups". The cleaned-up slug lives
                # in listing_status_normalized; this column stays reconcilable
                # against raw_scrapes.
                "listing_status": mls_status,
                "listing_status_normalized": self.normalize_listing_status(mls_status),
                "availability_status": availability_status,
                "days_on_market": self._to_int(platform_dict.get("days_on_market")),
                "days_on_market_mls": self._to_int(platform_dict.get("days_on_market_mls")),
                "has_open": platform_dict.get("has_open"),
                "listing_url": platform_dict.get("listing_url") or None,
                "description": description,
                "ai_summary": ai_summary,
                "listed_at": self._to_naive_utc(platform_dict.get("listed_at")),
                # Epoch milliseconds on 100% of scrapes — _to_naive_utc handles
                # the ms/seconds ambiguity by magnitude.
                "source_updated_at": self._to_naive_utc(basic_info.get("propertyLastUpdatedDate")),
                "price_drop_count": offer_stats["price_drop_count"],
                "sale_to_list_pct": offer_stats["sale_to_list_pct"],
                "area_price_drop_pct": offer_stats["area_price_drop_pct"],
                "area_avg_days_to_pending": offer_stats["area_avg_days_to_pending"],
                "area_median_list_price": offer_stats["area_median_list_price"],
                # Economics promoted out of `meta` into typed, indexable
                # columns. Still mirrored in `meta` by _build_meta below —
                # that copy is the provenance record, these are what the
                # scorers and the API read.
                "avm_value": avm_value,
                "rental_est_low": self._to_int(platform_dict.get("rental_est_low")),
                "rental_est_mid": self._to_int(platform_dict.get("rental_est_mid")),
                "rental_est_high": self._to_int(platform_dict.get("rental_est_high")),
                "hoa_monthly": self._to_float(platform_dict.get("hoa_monthly")),
                "price_per_sqft": self._to_float(platform_dict.get("price_per_sqft")),
                "tax_annual": self._to_float(platform_dict.get("tax_annual")),
                # Climate risk — flood and wind are Florida insurability inputs,
                # not trivia; they decide whether a deal cash-flows at all.
                "flood_factor": self._to_int(platform_dict.get("flood_factor")),
                "fire_factor": self._to_int(platform_dict.get("fire_factor")),
                "heat_factor": self._to_int(platform_dict.get("heat_factor")),
                "wind_factor": self._to_int(platform_dict.get("wind_factor")),
                "walk_score": self._to_float(platform_dict.get("walk_score")),
                "transit_score": self._to_float(platform_dict.get("transit_score")),
                "bike_score": self._to_float(platform_dict.get("bike_score")),
                "year_renovated": self._to_int(basic_info.get("yearRenovated")),
                "stories": self._to_float(basic_info.get("numStories")),
                # Distress block — booleans, the parsed auction date, and the
                # matched phrases behind each flag.
                **{key: distress[key] for key in distress if key != "distress_signals"},
                "distress_signals": distress["distress_signals"],
                "price_is_placeholder": price_is_placeholder,
                "data_quality_flags": quality_flags or None,
                "primary_source": "redfin",
                "meta": self._build_meta(platform_dict, basic_info, mls, amenities),
                "price_history": self._extract_price_history(platform_dict),
                "tax_history": tax_history,
                "property_images": platform_dict.get("property_images") or [],
                "open_houses": platform_dict.get("open_houses") or [],
                "schools": self._extract_schools(platform_dict),
                "comps": self._extract_comps(platform_dict),
                "pois": self._extract_pois(platform_dict),
                "transport_stops": self._extract_transport_stops(platform_dict),
                "location_score": self._extract_location_score(location_score),
                "features": self._extract_features(amenities),
                "market_snapshot": self._extract_market_snapshot(
                    platform_dict, mls, zip_code, offer_stats["area_price_drop_pct"]
                ),
            }

            return normalized

        except Exception as exc:
            logger.error("Redfin normalization failed: error_class=%s", type(exc).__name__)
            return None

    # ------------------------------------------------------------------
    # price plausibility
    # ------------------------------------------------------------------

    # A list price below this share of a known reference value (assessed or
    # AVM) is not a discount, it's a different kind of number. The live
    # foreclosure rows sit at 1-3% of assessed value; a genuine fire-sale
    # teardown still clears 20-30%. 10% leaves a wide margin on both sides.
    PLACEHOLDER_REFERENCE_RATIO = 0.10

    # Below this $/sqft nothing in Florida is a real asking price — the
    # cheapest genuine markets in this dataset run well north of $80/sqft,
    # and the auction rows compute to $2-5.
    MIN_PLAUSIBLE_PRICE_PER_SQFT = 20.0

    # Auction deposits and opening bids are small round numbers. Only applied
    # when the remarks independently say this is an auction, so an genuinely
    # cheap mobile home or land parcel isn't caught by it.
    MAX_AUCTION_DEPOSIT = 25_000

    @classmethod
    def _detect_placeholder_price(
        cls,
        price: Optional[int],
        sqft: Optional[int],
        avm_value: Optional[float],
        tax_history: list,
        is_auction: bool,
    ) -> tuple:
        """
        Decide whether `price` is a real asking price, and say why if not.

        Foreclosure auctions on Redfin publish the deposit or opening bid in
        the price field — 38 live properties sit at exactly $5,000 against
        assessed values from $167k to $556k. Taken at face value those are the
        deepest discounts in the database, so every strategy ranks them first,
        for a reason that has nothing to do with the deal.

        Returns (is_placeholder, flags). `is_placeholder` is None when there's
        no price at all and False when the price survived every check that
        could be applied — including the case where no reference value existed
        to check it against, which is recorded as a flag rather than treated
        as suspicion.
        """
        if not price or price <= 0:
            return None, []

        flags: List[str] = []
        reasons: List[str] = []

        # Reference 1: the assessment. Most reliable of the three — it's a
        # county valuation, independent of the listing.
        assessed = None
        for row in sorted(tax_history, key=lambda r: r.get("tax_year") or 0, reverse=True):
            if row.get("assessed_value"):
                assessed = row["assessed_value"]
                break
        if assessed and price < assessed * cls.PLACEHOLDER_REFERENCE_RATIO:
            reasons.append(f"price_under_{int(cls.PLACEHOLDER_REFERENCE_RATIO * 100)}pct_of_assessed_value")

        # Reference 2: the source's own automated valuation.
        if avm_value and price < avm_value * cls.PLACEHOLDER_REFERENCE_RATIO:
            reasons.append(f"price_under_{int(cls.PLACEHOLDER_REFERENCE_RATIO * 100)}pct_of_avm")

        # Reference 3: the property's own floor area. Independent of any
        # valuation, so it still fires when both of the above are missing.
        if sqft and sqft > 0 and (price / sqft) < cls.MIN_PLAUSIBLE_PRICE_PER_SQFT:
            reasons.append("price_per_sqft_below_market_floor")

        # Reference 4: an auction the remarks told us about.
        if is_auction and price <= cls.MAX_AUCTION_DEPOSIT:
            reasons.append("price_is_auction_deposit_or_opening_bid")

        if not (assessed or avm_value or sqft):
            flags.append("price_not_verifiable_no_reference_value")

        if reasons:
            flags.extend(reasons)
            logger.info("Flagging price %s as a placeholder: %s", price, ", ".join(reasons))
            return True, flags

        return False, flags

    # ------------------------------------------------------------------
    # meta (unstructured cross-source fields that live on Property.meta)
    # ------------------------------------------------------------------

    @classmethod
    def _build_meta(
        cls,
        platform_dict: Dict[str, Any],
        basic_info: Dict[str, Any],
        mls: Dict[str, Any],
        amenities: Dict[str, Any],
    ) -> Dict[str, Any]:
        # meta is a JSON column, not strictly typed, so these don't need the
        # numeric coercion the Property columns need — but empty strings are
        # still worth normalizing to None so downstream consumers don't have
        # to special-case "" vs missing.
        def clean(v):
            return v if v not in ("", None) else None

        return {
            "source": "redfin",
            "mls_id": clean(platform_dict.get("mls_id")),
            "listing_agent": clean(platform_dict.get("listing_agent")) or mls.get("listingAgentName"),
            "listing_agent_phone": mls.get("listingAgentNumber"),
            "listing_broker": mls.get("listingBrokerName"),
            "listing_broker_phone": mls.get("listingBrokerNumber"),
            "hoa_monthly": cls._to_float(platform_dict.get("hoa_monthly")),
            "monthly_payment": clean(platform_dict.get("monthly_payment")),
            "price_per_sqft": cls._to_float(platform_dict.get("price_per_sqft")),
            "avm_value": cls._to_float(platform_dict.get("avm_value")),
            "rental_est_low": cls._to_float(platform_dict.get("rental_est_low")),
            "rental_est_high": cls._to_float(platform_dict.get("rental_est_high")),
            "rental_est_mid": cls._to_float(platform_dict.get("rental_est_mid")),
            "walk_score": cls._to_float(platform_dict.get("walk_score")),
            "transit_score": cls._to_float(platform_dict.get("transit_score")),
            "bike_score": cls._to_float(platform_dict.get("bike_score")),
            "flood_factor": cls._to_float(platform_dict.get("flood_factor")),
            "fire_factor": cls._to_float(platform_dict.get("fire_factor")),
            "heat_factor": cls._to_float(platform_dict.get("heat_factor")),
            "wind_factor": cls._to_float(platform_dict.get("wind_factor")),
            "tax_annual": cls._to_float(platform_dict.get("tax_annual")),
            "tax_year": cls._to_int(platform_dict.get("tax_year")),
            "stories": cls._to_float(basic_info.get("numStories")),
            "year_renovated": cls._to_int(basic_info.get("yearRenovated")),
            "zoning": clean(amenities.get("Zoning")),
            # The parsed numbers live in typed Property columns
            # (price_drop_count, sale_to_list_pct, area_*). The human-readable
            # blocks are kept here too because the wording is the only record
            # of what those numbers meant on the day they were scraped, and
            # Redfin rewords this copy without notice.
            "offer_insights": cls._offer_insight_blocks(platform_dict.get("offer_insights")),
        }

    @staticmethod
    def _offer_insight_blocks(offer_insights: Any) -> Optional[list]:
        """Keep the raw header/title/copy triples, dropping anything malformed."""
        if not isinstance(offer_insights, (list, tuple)):
            return None
        blocks = [
            {"header": b.get("header"), "title": b.get("title"), "copy": b.get("copy")}
            for b in offer_insights
            if isinstance(b, dict)
        ]
        return blocks or None

    # ------------------------------------------------------------------
    # satellite table extractors
    # ------------------------------------------------------------------

    @classmethod
    def _extract_price_history(cls, platform_dict: Dict[str, Any]) -> list:
        """
        Every event Redfin reports, priced or not.

        This used to skip any event without a price, which quietly discarded
        five of Redfin's twelve event types — Listing Removed, Pending,
        Relisted, Contingent and Delisted never carry one, because they record
        a state transition rather than a number. Those are exactly the events
        worth the most for lead gen (a removed listing is an owner who wanted
        to sell and couldn't) and for validating the scoring weights (Pending
        and Sold are the only outcome labels in the dataset).

        A missing `event_date` is still a hard skip: `event_date` is NOT NULL,
        an event that can't be placed on the timeline can't be deduped against
        a later scrape, and it can't contribute to any before/after
        comparison. That's ~0.2% of events, and it's logged rather than
        silently dropped.
        """
        # event_history carries parsed datetimes; price_history is the raw-string
        # fallback if event_history isn't present for some reason.
        events = platform_dict.get("event_history") or platform_dict.get("price_history") or []
        result = []
        for e in events:
            if not isinstance(e, dict):
                continue
            event_date = cls._to_naive_utc(e.get("parsed_date") or e.get("date"))
            description = cls._clean_str(e.get("event"))
            if event_date is None:
                logger.debug(
                    "Skipping undated price-history event (event=%r, source_id=%r) — "
                    "event_date is required to place and dedupe it.",
                    description,
                    e.get("source_id"),
                )
                continue
            if not description:
                # Keep the row (the date/price are still real) but don't
                # invent a label for it — "listed" used to be the default,
                # which fabricated listing events that never happened.
                logger.warning("Price-history event with no description at %s; storing as Unknown.", event_date)
            result.append({
                "price": cls._sane_price(e.get("price")),
                "event": description or "Unknown",
                "event_type": canonical_event_type(description),
                "event_date": event_date,
                # The platform we scraped from. The event's own feed
                # ("Beaches MLS", "Public Records", ...) goes to event_source.
                "source": "redfin",
                "event_source": cls._clean_str(e.get("source")),
                "source_event_id": cls._clean_str(e.get("source_id")),
            })
        return cls._resolve_price_change_directions(result)

    @staticmethod
    def _resolve_price_change_directions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Turn Redfin's directionless "Price Changed" into reduced / increased,
        and tag every event as sale-side or rental-side.

        Redfin says a price changed but not which way, and "how many times has
        this seller cut the price" is the single most-used motivated-seller
        signal there is. Direction is recovered by walking the listing's own
        events oldest-first and comparing against the last price seen.

        The comparison is kept strictly within one market. Redfin files rental
        events in the same history as sale events, and rents are three orders
        of magnitude smaller than sale prices — so a single running
        `previous_price` across both would compare a $4,800 rent against a
        $700,000 list price and label it `increased`. That is not theoretical:
        24 such rows exist in the live set from the 4% of history that has been
        typed so far, and it would scale with a backfill. Two independent
        chains are tracked instead, and a bare "Price Changed" is attributed to
        whichever cycle is currently open — the market of the most recent
        listing-cycle event.

        Where there's no earlier price in the same market to compare against,
        the event stays `price_changed` rather than being assumed to be a cut.

        Mutates and returns `rows` in their original (scrape) order, adding an
        `is_rental_event` key to each.
        """
        # Which market the timeline is currently in. Sale is the default: a
        # history that opens with a bare "Price Changed" is far more likely to
        # be a sale listing than a rental, and Redfin's own page is a
        # for-sale/off-market page.
        in_rental_cycle = False
        last_price = {False: None, True: None}  # keyed by in_rental_cycle

        for row in sorted(rows, key=lambda r: r["event_date"]):
            event_type = row["event_type"]

            # A listing-cycle event switches (or confirms) the active market.
            # Everything else — including "Price Changed" — inherits it.
            if event_type in RENTAL_EVENT_TYPES:
                in_rental_cycle = True
            elif event_type in SALE_CYCLE_TYPES:
                in_rental_cycle = False

            row["is_rental_event"] = in_rental_cycle

            if event_type == PRICE_CHANGED:
                row["event_type"] = resolve_price_change_direction(
                    PRICE_CHANGED, row["price"], last_price[in_rental_cycle]
                )
            if row["price"] is not None:
                last_price[in_rental_cycle] = row["price"]

        return rows

    @classmethod
    def _extract_tax_history(cls, platform_dict: Dict[str, Any]) -> list:
        """
        Tax rows, with the assessment components kept separate.

        `assessed_value` is only populated when BOTH the land and improvement
        components are present, because it is defined as their sum. This used
        to treat a missing component as zero, which meant a parcel reporting
        only an improvement value recorded that improvement as the entire
        assessment — understated by the whole land component, which in Florida
        is routinely 20-40% of value. That's 533 of the 3,827 populated rows in
        the most recent run, and it silently skews every price-to-assessed
        ratio the motivated-seller and fix-and-flip scorers want to use.

        A wrong assessed value is worse than a missing one: the missing one is
        visible to a data-quality check, the wrong one is not. The components
        are stored alongside so a partial assessment is still inspectable.
        """
        rows = platform_dict.get("tax_history") or []
        result = []
        for row in rows:
            tax_year = cls._to_int(row.get("tax_year"))
            if not tax_year:
                continue
            land = cls._to_int(row.get("tax_able_land_value"))
            improvement = cls._to_int(row.get("tax_able_improvement_value"))
            if land is not None and improvement is not None:
                assessed_value = land + improvement
            else:
                assessed_value = None
            result.append({
                "tax_year": tax_year,
                "tax_amount": cls._to_int(row.get("tax_annual")),
                "land_value": land,
                "improvement_value": improvement,
                "assessed_value": assessed_value,
                "source": "redfin",
            })
        return result

    @classmethod
    def _extract_schools(cls, platform_dict: Dict[str, Any]) -> list:
        schools = platform_dict.get("schools") or []
        result = []
        for s in schools:
            name = s.get("name")
            if not name:
                continue
            result.append({
                "name": name,
                "school_type": s.get("type") or None,
                "grades": s.get("grades") or None,
                "rating": cls._to_int(s.get("rating")),
                "distance_miles": cls._to_float(s.get("distance")),
                "level": s.get("level") or None,
                "source": "redfin",
            })
        return result

    @classmethod
    def _extract_comps(cls, platform_dict: Dict[str, Any]) -> list:
        comps = platform_dict.get("avm_comps") or []
        result = []
        for c in comps:
            result.append({
                "comp_address": c.get("address") or None,
                "price": cls._to_int(c.get("price")),
                "bedrooms": cls._to_int(c.get("beds")),
                "bathrooms": cls._to_float(c.get("baths")),
                "sqft": cls._to_int(c.get("sqft")),
                "sold_date": cls._to_naive_utc(c.get("sold_date")),
                "source": "redfin",
            })
        return result

    @classmethod
    def _extract_pois(cls, platform_dict: Dict[str, Any]) -> list:
        places = platform_dict.get("places") or []
        result = []
        for p in places:
            name = p.get("name")
            if not name:
                continue
            result.append({
                "name": name,
                "category": p.get("category") or None,
                "popularity": cls._to_float(p.get("popularity")),
                # NOTE: unlike `schools`, this "distance" is a 0-1 proximity score in
                # Redfin's payload, not literal miles. Stored as-is under the
                # distance_miles column name for now — flag this before trusting it
                # in any radius-based scoring.
                "distance_miles": cls._to_float(p.get("distance")),
                "source": "redfin",
            })
        return result

    @classmethod
    def _extract_transport_stops(cls, platform_dict: Dict[str, Any]) -> list:
        raw = platform_dict.get("Transport") or platform_dict.get("transport") or []
        return [
            # stop_name is String(255); the longest observed is 60 chars, but
            # truncate rather than let an outlier abort the whole property.
            {"stop_name": name[:255], "source": "redfin"}
            for name in cls._coerce_stop_names(raw)
        ]

    @classmethod
    def _coerce_stop_names(cls, raw: Any) -> List[str]:
        """
        Accept a list of stop names, or the legacy `str(set)` form.

        The scraper used to emit `Transport` as a Python set, which the JSON
        column stored as its repr — "{'PGA BLVD at TOYS R US', 'Gardens Mall'}"
        — on all 10,935 existing scrapes. That form is lossy (a stop name
        containing an apostrophe breaks the round-trip) and non-deterministic
        (set ordering churns on every rescrape), so the scraper now emits a
        sorted JSON array. This stays tolerant of both, because the existing
        raw_scrapes rows still hold the old form and a later backfill has to
        read them.

        Duplicates are collapsed, order is preserved, and anything
        unparseable yields an empty list rather than raising.
        """
        values: Any = raw

        if isinstance(values, str):
            text = values.strip()
            if not text:
                return []
            parsed = None
            for loader in (ast.literal_eval, json.loads):
                try:
                    parsed = loader(text)
                    break
                except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                    continue
            if parsed is None:
                # literal_eval chokes on an apostrophe inside a name
                # ("{'JOE'S DINER at MAIN ST'}"). Split on the quote-comma-quote
                # boundary instead, which apostrophes inside a name can't fake.
                logger.debug("Falling back to string-splitting legacy Transport value: %.120s", text)
                parsed = [
                    part.strip().strip("'\"")
                    for part in re.split(r"['\"]\s*,\s*['\"]", text.strip("{}[]()"))
                ]
            values = parsed

        if isinstance(values, (list, tuple, set, frozenset)):
            seen = set()
            names = []
            for value in values:
                if not isinstance(value, str):
                    continue
                name = value.strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                names.append(name)
            return names

        logger.debug("Unrecognized Transport payload of type %s; storing no stops.", type(raw).__name__)
        return []

    @classmethod
    def _extract_location_score(cls, location_score: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not location_score:
            return None
        return {
            "pedestrian_score": cls._to_float(location_score.get("pedestrianFriendlyScore")),
            "cycling_score": cls._to_float(location_score.get("cyclingFriendlyScore")),
            "transit_score": cls._to_float(location_score.get("transitFriendlyScore")),
            "car_score": cls._to_float(location_score.get("carFriendlyScore")),
            "parks_score": cls._to_float(location_score.get("parksScore")),
            "groceries_score": cls._to_float(location_score.get("groceriesScore")),
            "shopping_score": cls._to_float(location_score.get("shoppingScore")),
            "nightlife_score": cls._to_float(location_score.get("nightlifeScore")),
            "restaurants_score": cls._to_float(location_score.get("restaurantsScore")),
            "cafes_score": cls._to_float(location_score.get("cafesScore")),
            "daycares_score": cls._to_float(location_score.get("daycaresScore")),
            "primary_schools_score": cls._to_float(location_score.get("primarySchoolsScore")),
            "high_schools_score": cls._to_float(location_score.get("highSchoolsScore")),
            "quiet_score": cls._to_float(location_score.get("quietScore")),
            "vibrant_score": cls._to_float(location_score.get("vibrantScore")),
            "wellness_score": cls._to_float(location_score.get("wellnessScore")),
            "source_updated_at": cls._to_naive_utc(location_score.get("lastUpdatedDate")),
            "source": "redfin",
        }

    @staticmethod
    def _extract_features(amenities: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not amenities:
            return None

        def clean(v):
            return v if v not in ("", None) else None

        return {
            "heating": clean(amenities.get("Heating Information")),
            "cooling": clean(amenities.get("Cooling Information")),
            "flooring": clean(amenities.get("Flooring")),
            "construction_material": clean(amenities.get("Construction Materials")),
            "roof": clean(amenities.get("Roof")),
            "foundation": clean(amenities.get("Foundation Details")),
            "interior_features": clean(amenities.get("Interior Features")),
            "appliances": clean(amenities.get("Appliances")),
            "laundry_features": clean(amenities.get("Laundry Features")),
            "water_source": clean(amenities.get("Water Source")),
            "sewer": clean(amenities.get("Sewer")),
            "utilities": clean(amenities.get("Utilities")),
            "furnished": clean(amenities.get("Furnished")),
            "direction_faces": clean(amenities.get("Direction Faces")),
            "raw_amenities": amenities,
        }

    @classmethod
    def _extract_market_snapshot(
        cls,
        platform_dict: Dict[str, Any],
        mls: Dict[str, Any],
        zip_code: Optional[str],
        area_price_drop_pct: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        # These come through as pre-formatted strings ("$350K", "98.2%", "+7.3%"),
        # so parsing is best-effort — treat this table as lower-confidence than the
        # rest and spot-check it once real data is flowing.
        keys = ("median_list_price", "median_sale_price", "avg_days_on_market", "sale_to_list_pct", "yoy_sale_price")
        if not any(platform_dict.get(k) for k in keys):
            return None

        return {
            "zip_code": zip_code,
            "source": "redfin",
            "snapshot_date": cls._parse_snapshot_date(mls),
            "median_list_price": cls._parse_dollar_shorthand(platform_dict.get("median_list_price")),
            "median_sale_price": cls._parse_dollar_shorthand(platform_dict.get("median_sale_price")),
            "avg_days_on_market": cls._parse_number(platform_dict.get("avg_days_on_market")),
            "sale_to_list_pct": cls._parse_percent(platform_dict.get("sale_to_list_pct")),
            "yoy_price_change_pct": cls._parse_percent(platform_dict.get("yoy_sale_price")),
            # Same number as properties.area_price_drop_pct — parsed once by
            # _extract_offer_insights and passed in, rather than re-parsed here.
            "price_drop_pct": area_price_drop_pct,
        }

    # ------------------------------------------------------------------
    # offer_insights — four human-readable blocks Redfin renders on the
    # listing page. Populated on 98% of scrapes and, until now, discarded
    # apart from a single percentage. The PRICE DROP title in particular is
    # a per-listing price-reduction count that Redfin computes for us.
    #
    # Every parser below returns None on no-match instead of raising: this
    # is marketing copy, the wording drifts without notice, and one reworded
    # sentence must never cost us the whole property. A known header whose
    # value won't parse is logged with the offending text so the drift is
    # visible instead of silent.
    # ------------------------------------------------------------------

    OFFER_INSIGHT_FIELDS = (
        "price_drop_count",
        "sale_to_list_pct",
        "area_price_drop_pct",
        "area_avg_days_to_pending",
        "area_median_list_price",
    )

    @classmethod
    def _extract_offer_insights(cls, offer_insights: Any) -> Dict[str, Any]:
        stats: Dict[str, Any] = {field: None for field in cls.OFFER_INSIGHT_FIELDS}
        if not offer_insights:
            return stats
        if not isinstance(offer_insights, (list, tuple)):
            logger.warning("offer_insights was %s, expected a list; skipping.", type(offer_insights).__name__)
            return stats

        for block in offer_insights:
            if not isinstance(block, dict):
                continue
            header = (cls._clean_str(block.get("header")) or "").upper()
            title = block.get("title")
            copy = block.get("copy")

            if header == "PRICE DROP":
                # "1 price drop" / "0 price drops" — this listing's own count.
                stats["price_drop_count"] = cls._parse_or_log(
                    cls._parse_drop_count, title, header, "price_drop_count"
                )
                # "17% of homes in this area have had a price drop in the past month."
                stats["area_price_drop_pct"] = cls._parse_or_log(
                    cls._parse_leading_percent, copy, header, "area_price_drop_pct"
                )
            elif header == "SALE-TO-LIST":
                # "96.0%" — this listing's sale-to-list ratio.
                stats["sale_to_list_pct"] = cls._parse_or_log(
                    cls._parse_percent, title, header, "sale_to_list_pct"
                )
            elif header == "DAYS ON MARKET":
                # "The average home goes pending in 76 days." (the title is
                # this listing's own DOM, which we already have.)
                stats["area_avg_days_to_pending"] = cls._parse_or_log(
                    cls._parse_days, copy, header, "area_avg_days_to_pending"
                )
            elif header == "LIST PRICE":
                # "Median list price in this area is $690,286. (42% lower than this home)"
                stats["area_median_list_price"] = cls._parse_or_log(
                    cls._parse_dollar_amount, copy, header, "area_median_list_price"
                )
            elif header:
                logger.warning(
                    "Unrecognized offer_insights header %r (title=%r, copy=%r) — "
                    "Redfin added a block we don't parse yet.",
                    header, title, copy,
                )

        return stats

    @staticmethod
    def _parse_or_log(parser, value: Any, header: str, field: str) -> Optional[Any]:
        """Run a parser, logging (not swallowing) a non-empty value it couldn't read."""
        parsed = parser(value)
        if parsed is None and value not in (None, ""):
            logger.warning(
                "Could not parse %s from offer_insights %r block: %r", field, header, value
            )
        return parsed

    @staticmethod
    def _parse_drop_count(value: Any) -> Optional[int]:
        """'1 price drop' / '0 price drops' -> 1 / 0."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return None
        match = re.search(r'(\d+)\s*price\s*drop', value, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_leading_percent(value: Any) -> Optional[float]:
        """'17% of homes in this area...' -> 17.0 (first percentage in the string)."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if not isinstance(value, str):
            return None
        match = re.search(r'([\d.]+)\s*%', value)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _parse_days(value: Any) -> Optional[int]:
        """'The average home goes pending in 76 days.' -> 76."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return None
        match = re.search(r'(\d+)\s*day', value, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_dollar_amount(value: Any) -> Optional[int]:
        """'Median list price in this area is $690,286. (...)' -> 690286."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        if not isinstance(value, str):
            return None
        match = re.search(r'\$\s*([\d,]+(?:\.\d+)?)', value)
        if not match:
            return None
        try:
            return int(round(float(match.group(1).replace(",", ""))))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # type coercion helpers — every raw value on its way into a typed
    # Property/satellite column goes through one of these. '' and other
    # junk becomes None instead of a raw DB insert error.
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_str(value: Any) -> Optional[str]:
        """Trim a scraped string, coercing '' / whitespace-only / non-strings to None."""
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    # Postgres int4 ceiling. Redfin emits it verbatim as an "unknown price"
    # sentinel — it is sitting on two live "Price Changed" events for a
    # $330,000 house in West Palm Beach. Left alone it reads as a 650,000%
    # price increase and destroys every percentage this property feeds.
    INT32_MAX = 2_147_483_647

    # No residential listing on Redfin is worth this much; anything at or
    # above it is a sentinel, a data-entry error, or a bulk portfolio sale
    # mis-filed against a single parcel. The largest genuine price in the
    # live set is $35,995,000, so this leaves an order of magnitude of room.
    MAX_PLAUSIBLE_PRICE = 500_000_000

    @classmethod
    def _sane_price(cls, value: Any) -> Optional[int]:
        """
        Coerce a price, rejecting sentinels and impossible magnitudes.

        Returns None rather than dropping the whole event: an event with an
        unusable price is still a real state transition on a real date, and
        `price_history.price` is nullable precisely so those survive. Silently
        storing the sentinel is the worse failure — it's invisible until some
        percentage calculation returns a nonsense number.
        """
        price = cls._to_int(value)
        if price is None:
            return None
        if price <= 0 or price >= cls.MAX_PLAUSIBLE_PRICE or price == cls.INT32_MAX:
            logger.warning("Discarding implausible price %r from price history.", value)
            return None
        return price

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None or value == "" or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        try:
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "" or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_naive_utc(value: Any) -> Optional[datetime]:
        """
        Coerce a variety of raw formats into a naive UTC datetime, or None.

        The scraper doesn't guarantee a clean Python datetime for every
        date-ish field — some come through as '' (missing), some as epoch
        milliseconds, some as ISO strings, some as already-parsed aware
        datetimes. asyncpg will reject anything here that isn't an actual
        datetime object bound to a TIMESTAMP column, so this is the single
        place that guarantees one.
        """
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        if isinstance(value, (int, float)):
            # Guess ms vs seconds by magnitude (Redfin's comp sold_date is ms).
            seconds = value / 1000 if value > 10_000_000_000 else value
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).replace(tzinfo=None)
                return parsed
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_dollar_shorthand(value: Any) -> Optional[float]:
        """Parse strings like '$350K' or '$1.2M' into a plain number."""
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value)
        match = re.match(r'^\$?([\d,.]+)\s*([KkMm])?$', str(value).strip())
        if not match:
            return None
        number = float(match.group(1).replace(',', ''))
        suffix = (match.group(2) or '').upper()
        if suffix == 'K':
            number *= 1_000
        elif suffix == 'M':
            number *= 1_000_000
        return number

    @staticmethod
    def _parse_percent(value: Any) -> Optional[float]:
        """Parse strings like '98.2%' or '+7.3%' into a plain float."""
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value)
        match = re.match(r'^([+-]?[\d.]+)\s*%?$', str(value).strip())
        if not match:
            return None
        return float(match.group(1))

    @staticmethod
    def _parse_number(value: Any) -> Optional[float]:
        """Parse a plain numeric string (e.g. '74') into a float."""
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except ValueError:
            return None

    @staticmethod
    def _parse_snapshot_date(mls: Dict[str, Any]):
        """Parse MLS's 'lastUpdatedString' (e.g. 'Jul 16, 2026 11:34 AM') into a date."""
        raw = (mls or {}).get("lastUpdatedString")
        if raw:
            try:
                return datetime.strptime(raw, "%b %d, %Y %I:%M %p").date()
            except ValueError:
                pass
        # Fall back to today if the MLS timestamp is missing or unparseable — this is
        # the date we observed these stats, even if we can't pin down the source's.
        return datetime.now(timezone.utc).date()
