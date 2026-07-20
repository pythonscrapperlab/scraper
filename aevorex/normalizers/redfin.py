"""Redfin normalizer — maps Redfin API data to properties schema."""

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aevorex.db.deduplicator import Deduplicator
from aevorex.normalizers.base import BaseNormalizer


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

            lot_sqft = self._to_float(platform_dict.get("lot_sqft") or basic_info.get("lotSqFt"))
            lot_size_acres = round(lot_sqft / 43560, 4) if lot_sqft else None

            normalized: Dict[str, Any] = {
                "redfin_id": redfin_id,
                # Normalized here (not just at upsert time) so a duplicate check done
                # earlier in the pipeline, before upsert() runs, still sees the
                # canonical form.
                "apn": Deduplicator.normalize_apn(apn) if apn else None,
                "county": county,
                "address": self.normalize_address(address),
                "city": city,
                "state": state,
                "zip_code": self.normalize_zip(zip_code),
                "latitude": self._to_float(platform_dict.get("latitude")),
                "longitude": self._to_float(platform_dict.get("longitude")),
                "price": self._to_int(platform_dict.get("list_price")) or self._to_int(platform_dict.get("price")),
                "bedrooms": self._to_int(platform_dict.get("beds")) or self._to_int(platform_dict.get("bedrooms")),
                "bathrooms": self._to_float(platform_dict.get("baths")) or self._to_float(platform_dict.get("bathrooms")),
                "sqft": self._to_int(platform_dict.get("sqft")),
                "lot_size": lot_size_acres,
                "year_built": self._to_int(platform_dict.get("year_built")) or self._to_int(basic_info.get("yearBuilt")),
                "property_type": basic_info.get("propertyTypeName") or platform_dict.get("property_type") or None,
                "listing_status": platform_dict.get("listing_status") or platform_dict.get("status") or None,
                "days_on_market": self._to_int(platform_dict.get("days_on_market")),
                "listing_url": platform_dict.get("listing_url") or None,
                "description": platform_dict.get("description") or None,
                "listed_at": self._to_naive_utc(platform_dict.get("listed_at")),
                "primary_source": "redfin",
                "meta": self._build_meta(platform_dict, basic_info, mls, amenities),
                "price_history": self._extract_price_history(platform_dict),
                "tax_history": self._extract_tax_history(platform_dict),
                "property_images": platform_dict.get("property_images") or [],
                "open_houses": platform_dict.get("open_houses") or [],
                "schools": self._extract_schools(platform_dict),
                "comps": self._extract_comps(platform_dict),
                "pois": self._extract_pois(platform_dict),
                "transport_stops": self._extract_transport_stops(platform_dict),
                "location_score": self._extract_location_score(location_score),
                "features": self._extract_features(amenities),
                "market_snapshot": self._extract_market_snapshot(platform_dict, mls, zip_code),
            }

            return normalized

        except Exception as e:
            print(f"Error normalizing Redfin data: {e}")
            return None

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
        }

    # ------------------------------------------------------------------
    # satellite table extractors
    # ------------------------------------------------------------------

    @classmethod
    def _extract_price_history(cls, platform_dict: Dict[str, Any]) -> list:
        # event_history carries parsed datetimes; price_history is the raw-string
        # fallback if event_history isn't present for some reason.
        events = platform_dict.get("event_history") or platform_dict.get("price_history") or []
        result = []
        for e in events:
            price = cls._to_int(e.get("price"))
            event_date = cls._to_naive_utc(e.get("parsed_date") or e.get("date"))
            if price is None or event_date is None:
                continue
            result.append({
                "price": price,
                "event": e.get("event") or "listed",
                "event_date": event_date,
                "source": e.get("source") or "redfin",
            })
        return result

    @classmethod
    def _extract_tax_history(cls, platform_dict: Dict[str, Any]) -> list:
        rows = platform_dict.get("tax_history") or []
        result = []
        for row in rows:
            tax_year = cls._to_int(row.get("tax_year"))
            if not tax_year:
                continue
            land = cls._to_float(row.get("tax_able_land_value"))
            improvement = cls._to_float(row.get("tax_able_improvement_value"))
            assessed_value = (land or 0) + (improvement or 0) if (land is not None or improvement is not None) else None
            result.append({
                "tax_year": tax_year,
                "tax_amount": cls._to_int(row.get("tax_annual")),
                "assessed_value": cls._to_int(assessed_value) if assessed_value is not None else None,
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
                "category": None,  # Redfin doesn't categorize these; leave for a future classifier pass
                "popularity": cls._to_float(p.get("popularity")),
                # NOTE: unlike `schools`, this "distance" is a 0-1 proximity score in
                # Redfin's payload, not literal miles. Stored as-is under the
                # distance_miles column name for now — flag this before trusting it
                # in any radius-based scoring.
                "distance_miles": cls._to_float(p.get("distance")),
                "source": "redfin",
            })
        return result

    @staticmethod
    def _extract_transport_stops(platform_dict: Dict[str, Any]) -> list:
        stops = platform_dict.get("Transport") or platform_dict.get("transport") or []
        return [{"stop_name": name, "source": "redfin"} for name in stops if name]

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
        cls, platform_dict: Dict[str, Any], mls: Dict[str, Any], zip_code: Optional[str]
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
            "price_drop_pct": cls._extract_price_drop_pct(platform_dict.get("offer_insights")),
        }

    # ------------------------------------------------------------------
    # type coercion helpers — every raw value on its way into a typed
    # Property/satellite column goes through one of these. '' and other
    # junk becomes None instead of a raw DB insert error.
    # ------------------------------------------------------------------

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

    @staticmethod
    def _extract_price_drop_pct(offer_insights: Any) -> Optional[float]:
        if not offer_insights:
            return None
        for item in offer_insights:
            if item.get("header") == "PRICE DROP":
                match = re.search(r'([\d.]+)\s*%', item.get("copy") or "")
                if match:
                    return float(match.group(1))
        return None