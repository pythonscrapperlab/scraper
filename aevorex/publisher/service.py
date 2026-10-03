"""E3 publisher orchestration from local truth to Supabase serving v2."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from aevorex.alerts.alerts import Candidate, enqueue_threshold_alerts, market_score_states
from aevorex.alerts.rules import ScoreState
from aevorex.config import settings
from aevorex.db.models import (
    ChangeEvent,
    ListingPresence,
    MarketFreshness,
    Property,
    PropertyFeature,
    PropertyValuation,
)
from aevorex.db.session import async_session_maker
from aevorex.publisher.markets import market_definition
from aevorex.publisher.remote import EXPECTED_COLUMNS, RemoteStore, publisher_engine
from aevorex.publisher.snapshots import build_demo_snapshot
from aevorex.publisher.types import MarketDefinition, PublishResult, classify_size
from aevorex.scoring.recompose import recompose

LOGGER = logging.getLogger(__name__)
LENSES = ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")
ACTIVE_STATUSES = {"active", "coming_soon", "new", "pending", "contingent"}


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("Expected datetime value")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(value: Any, fallback: Any) -> Any:
    return value if isinstance(value, type(fallback)) else fallback


def _market_id(slug: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://aevoraex.com/markets/{slug}")


def retained_delisted(delisted_at: datetime | None, now: datetime) -> bool:
    """Retain through the exact 30-day boundary; prune only strictly older rows."""
    if delisted_at is None:
        return True
    comparable_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
    comparable_delisted = (
        delisted_at.replace(tzinfo=None) if delisted_at.tzinfo is not None else delisted_at
    )
    return comparable_delisted >= comparable_now - timedelta(days=30)


def apply_rejections(result: PublishResult, property_ids: Sequence[str]) -> None:
    """Mark recomposition rejections partial without adding PII to telemetry."""
    if not property_ids:
        return
    result.status = "partial"
    result.rejected_property_ids.extend(property_ids)


class Publisher:
    """Idempotent, outbound-only serving-cache publisher."""

    def __init__(
        self,
        *,
        local_sessions: async_sessionmaker[AsyncSession] = async_session_maker,
        remote_engine: AsyncEngine | None = None,
        now: datetime | None = None,
    ) -> None:
        self.local_sessions = local_sessions
        self.store = RemoteStore(
            remote_engine or publisher_engine(), batch_size=settings.publisher_batch_size
        )
        self._fixed_now = now
        self._cadence_minutes = 120

    @property
    def now(self) -> datetime:
        """Current UTC time, optionally fixed for deterministic tests."""
        return self._fixed_now or datetime.now(UTC)

    async def push(
        self, market_slug: str, *, trigger: str = "cli", cadence_minutes: int = 120
    ) -> PublishResult:
        """Publish one market in ordered transactions; freshness commits last."""
        self._cadence_minutes = cadence_minutes
        definition = market_definition(market_slug)
        await self.store.initialize()
        run_id = uuid4()
        started_at = self.now
        result = PublishResult(market=definition.slug)
        try:
            properties, freshness = await self._load_local(definition)
            market_id = _market_id(definition.slug)
            async with self.store.transaction() as remote:
                guard = classify_size(await self.store.database_size(remote))
                result.size_action = guard.action
                exists = await self.store.market_exists(remote, definition.slug)
                if guard.action in {"stop_new_cities", "page"} and not exists:
                    raise RuntimeError("SupabaseSizeGuardNewMarket")
                await self.store.upsert(
                    remote,
                    "markets",
                    [self._market_row(definition, market_id, properties)],
                    conflict=("slug",),
                )
                await self.store.upsert(
                    remote,
                    "runs",
                    [{
                        "id": run_id,
                        "market_id": market_id,
                        "kind": "publisher",
                        "trigger": trigger,
                        "started_at": started_at,
                        "status": "running",
                        "counts": {},
                    }],
                    conflict=("id",),
                )

            property_rows = [self._property_row(item, market_id) for item in properties]
            property_ids = [cast(UUID, item.id) for item in properties]
            async with self.store.transaction() as remote:
                result.counts["properties"] = await self.store.upsert(
                    remote, "properties", property_rows, conflict=("id",)
                )
                result.counts["pruned"] = await self.store.delete_market_properties(
                    remote, market_id, property_ids
                )

            score_rows, rejected = self._score_rows(properties)
            apply_rejections(result, rejected)
            if rejected:
                for property_id in rejected:
                    LOGGER.warning("Publisher rejected score breakdown: property_id=%s", property_id)
            async with self.store.transaction() as remote:
                # Alerts commit atomically with the scores they were derived from: a failed
                # alert write rolls the scores back too, so the crossing is retried, never lost.
                previous_states = await market_score_states(remote, market_id)
                result.counts["scores"] = await self.store.upsert(
                    remote,
                    "scores",
                    score_rows,
                    conflict=("property_id", "lens"),
                    preserve_previous_scores=True,
                )
                alert_counts = await enqueue_threshold_alerts(
                    remote,
                    definition.slug,
                    self._alert_candidates(properties, score_rows, previous_states),
                    self.now,
                    baseline=not previous_states,
                )
                result.counts.update(alert_counts.as_counts())
                result.counts["scores_pruned"] = await self.store.delete_market_scores_not_in(
                    remote,
                    market_id,
                    [
                        (row["property_id"], str(row["lens"]))
                        for row in score_rows
                    ],
                )

            child_rows = self._child_rows(properties)
            async with self.store.transaction() as remote:
                for name in (
                    "valuation", "agents", "features", "neighbourhood"
                ):
                    await self._replace_one_to_one(remote, name, market_id, child_rows[name])
                    result.counts[name] = len(child_rows[name])
                for name, conflict in (
                    ("images", ("property_id", "sort_order")),
                    ("comps", ("id",)),
                    ("history", ("id",)),
                    ("tax_history", ("property_id", "tax_year")),
                ):
                    await self._replace_many(remote, name, market_id, child_rows[name], conflict)
                    result.counts[name] = len(child_rows[name])

            event_rows = await self._event_rows(definition.slug, market_id, set(property_ids))
            async with self.store.transaction() as remote:
                result.counts["change_events"] = await self.store.upsert(
                    remote, "change_events", event_rows, conflict=("id",)
                )

            snapshots = []
            if definition.is_demo:
                for lens in LENSES:
                    snapshots.append(
                        {
                            "market_slug": definition.slug,
                            "lens": lens,
                            "payload": build_demo_snapshot(
                                definition, lens, properties, freshness
                            ),
                            "generated_at": self.now,
                        }
                    )
            daily = self._market_daily(market_id, score_rows, properties)
            async with self.store.transaction() as remote:
                result.counts["demo_snapshots"] = await self.store.upsert(
                    remote,
                    "demo_snapshots",
                    snapshots,
                    conflict=("market_slug", "lens"),
                )
                result.counts["market_daily"] = await self.store.upsert(
                    remote, "market_daily", [daily], conflict=("market_id", "day")
                )

            result.counts["heartbeats"] = 1
            finished_at = self.now
            async with self.store.transaction() as remote:
                await remote.execute(
                    update(self.store.table("markets"))
                    .where(self.store.table("markets").c.id == market_id)
                    .values(**self._freshness_values(freshness, properties, score_rows))
                )
                await remote.execute(
                    self.store.table("heartbeats").insert().values(
                        at=finished_at, note=f"publisher:{definition.slug}:{result.status}"
                    )
                )
                await remote.execute(
                    update(self.store.table("runs"))
                    .where(self.store.table("runs").c.id == run_id)
                    .values(
                        finished_at=finished_at,
                        status=result.status,
                        counts=result.telemetry(),
                        duration_s=max(0, int((finished_at - started_at).total_seconds())),
                    )
                )
            await self._ping_healthcheck(failed=result.size_action == "page")
            return result
        except BaseException as exc:
            await self._finish_failed_run(run_id, type(exc).__name__)
            await self._ping_healthcheck(failed=True)
            raise

    async def status(self) -> dict[str, Any]:
        """Return current schema version, database size, markets, and all table counts."""
        await self.store.initialize()
        async with self.store.engine.connect() as remote:
            size = classify_size(await self.store.database_size(remote))
            markets = (
                await remote.execute(
                    select(
                        self.store.table("markets").c.slug,
                        self.store.table("markets").c.check_status,
                        self.store.table("markets").c.listings_active,
                        self.store.table("markets").c.last_checked_at,
                        self.store.table("markets").c.last_refreshed_at,
                    ).order_by(self.store.table("markets").c.slug)
                )
            ).mappings().all()
            return {
                "schema_version": 2,
                "database_size_bytes": size.size_bytes,
                "database_size_mb": size.size_mb,
                "size_action": size.action,
                "markets": [dict(row) for row in markets],
                "table_counts": await self.store.table_counts(remote),
            }

    async def prune(self, market_slug: str, *, dry_run: bool = True) -> dict[str, int]:
        """Report or remove remote rows outside the exact 30-day retention set."""
        definition = market_definition(market_slug)
        await self.store.initialize()
        properties, _ = await self._load_local(definition)
        keep_ids = [item.id for item in properties]
        market_id = _market_id(definition.slug)
        table = self.store.table("properties")
        async with self.store.transaction() as remote:
            statement = select(func.count()).where(table.c.market_id == market_id)
            if keep_ids:
                statement = statement.where(table.c.id.not_in(keep_ids))
            count = int(await remote.scalar(statement) or 0)
            if not dry_run:
                await self.store.delete_market_properties(remote, market_id, keep_ids)
        return {"candidates": count, "deleted": 0 if dry_run else count}

    async def rebuild(self, market_slug: str) -> PublishResult:
        """Delete one market's rebuildable property tree, then publish it again."""
        definition = market_definition(market_slug)
        await self.store.initialize()
        market_id = _market_id(definition.slug)
        async with self.store.transaction() as remote:
            await self.store.delete_market_properties(remote, market_id, ())
            snapshots = self.store.table("demo_snapshots")
            await remote.execute(delete(snapshots).where(snapshots.c.market_slug == definition.slug))
        return await self.push(definition.slug, trigger="rebuild")

    async def drift(self) -> dict[str, Any]:
        """Compare the live serving schema with the reflected v2 table contract."""
        await self.store.initialize()
        async with self.store.engine.connect() as remote:
            columns = await self.store.schema_columns(remote)
        expected = EXPECTED_COLUMNS
        missing_tables = sorted(expected.keys() - columns.keys())
        extra_tables = sorted(columns.keys() - expected.keys())
        column_drift = {
            name: {
                "missing": sorted(expected[name] - columns.get(name, set())),
                "extra": sorted(columns.get(name, set()) - expected[name]),
            }
            for name in sorted(expected.keys() & columns.keys())
            if expected[name] != columns[name]
        }
        return {
            "ok": not missing_tables and not extra_tables and not column_drift,
            "missing_tables": missing_tables,
            "extra_tables": extra_tables,
            "column_drift": column_drift,
        }

    async def wake(self) -> dict[str, int]:
        """Write a readiness heartbeat only; never schedule work or send email."""
        await self.store.initialize()
        async with self.store.transaction() as remote:
            await remote.execute(
                self.store.table("heartbeats").insert().values(at=self.now, note="publisher:wake")
            )
        await self._ping_healthcheck(failed=False)
        return {"heartbeats": 1}

    async def push_freshness(self, market_slug: str) -> bool:
        """Update only the freshness columns of an already-published market.

        Used after a check that changed nothing: the web must still see the new
        ``last_checked_at`` without paying for a full-city push. Returns ``False`` when the
        market has never been published (the caller must then run a full push).
        """
        definition = market_definition(market_slug)
        await self._ensure_initialized()
        async with self.local_sessions() as session:
            freshness = await session.get(MarketFreshness, definition.slug)
        if freshness is None:
            return False
        values = {
            "last_checked_at": _aware(freshness.last_checked_at),
            "next_check_at": _aware(freshness.next_check_at),
            "last_refreshed_at": _aware(freshness.last_refreshed_at),
            "check_status": freshness.check_status,
            "listings_active": freshness.listings_active,
            "changed_last_check": freshness.changed_last_check,
            "source_status": {"redfin": freshness.check_status},
            "updated_at": self.now,
        }
        return await self._update_market(definition.slug, values, note="freshness")

    async def mark_status(
        self,
        market_slug: str,
        check_status: str,
        *,
        next_check_at: datetime | None = None,
    ) -> bool:
        """Set ``late``/``failed``/``warming``/``ok`` without touching freshness times."""
        if check_status not in {"ok", "late", "failed", "warming"}:
            raise ValueError("Unsupported check_status")
        await self._ensure_initialized()
        values: dict[str, Any] = {
            "check_status": check_status,
            "source_status": {"redfin": check_status},
            "updated_at": self.now,
        }
        if next_check_at is not None:
            values["next_check_at"] = _aware(next_check_at)
        return await self._update_market(market_slug, values, note=f"status:{check_status}")

    async def heartbeat(self, note: str = "scheduler") -> None:
        """Insert one heartbeat row. Unlike ``wake`` it pings no Healthchecks URL."""
        await self._ensure_initialized()
        beats = self.store.table("heartbeats")
        async with self.store.transaction() as remote:
            await remote.execute(beats.insert().values(at=self.now, note=note[:80]))
            # One row per 10 minutes would grow without bound on a 500 MB plan: keep 14 days.
            await remote.execute(delete(beats).where(beats.c.at < self.now - timedelta(days=14)))

    async def _ensure_initialized(self) -> None:
        if not self.store.tables:
            await self.store.initialize()

    async def _update_market(self, slug: str, values: dict[str, Any], *, note: str) -> bool:
        markets = self.store.table("markets")
        async with self.store.transaction() as remote:
            result = await remote.execute(
                update(markets).where(markets.c.slug == slug).values(**values)
            )
            if not result.rowcount:
                return False
            await remote.execute(
                self.store.table("heartbeats").insert().values(
                    at=self.now, note=f"publisher:{slug}:{note}"[:80]
                )
            )
        return True

    async def close(self) -> None:
        await self.store.close()

    async def _load_local(
        self, definition: MarketDefinition
    ) -> tuple[list[Property], MarketFreshness]:
        cutoff = self.now.replace(tzinfo=None) - timedelta(days=30)
        async with self.local_sessions() as session:
            freshness = await session.get(MarketFreshness, definition.slug)
            if freshness is None:
                raise RuntimeError("MarketHasNoCompletedFreshnessState")
            statement = (
                select(Property)
                .join(ListingPresence, ListingPresence.property_id == Property.id)
                .where(ListingPresence.market_slug == definition.slug)
                .where(
                    (ListingPresence.is_delisted.is_(False))
                    | (Property.delisted_at >= cutoff)
                )
                .options(
                    selectinload(Property.analysis),
                    selectinload(Property.valuation),
                    selectinload(Property.property_images),
                    selectinload(Property.comps),
                    selectinload(Property.price_history),
                    selectinload(Property.tax_history),
                    selectinload(Property.schools),
                    selectinload(Property.transport_stops),
                    selectinload(Property.location_score),
                    selectinload(Property.features),
                )
                .order_by(Property.id)
            )
            properties = list((await session.execute(statement)).scalars().unique().all())
            return properties, freshness

    def _market_row(
        self, definition: MarketDefinition, market_id: UUID, properties: Sequence[Property]
    ) -> dict[str, Any]:
        return {
            "id": market_id,
            "city": definition.city,
            "state": definition.state,
            "region_id": definition.region_id,
            "slug": definition.slug,
            "tz": definition.timezone,
            "zips": sorted({item.zip_code for item in properties if item.zip_code}),
            "active": True,
            "is_demo": definition.is_demo,
            "check_cadence_minutes": self._cadence_minutes,
            "updated_at": self.now,
        }

    def _property_row(self, item: Property, market_id: UUID) -> dict[str, Any]:
        return {
            "id": item.id,
            "market_id": market_id,
            "redfin_id": item.redfin_id,
            "apn": item.apn,
            "address": item.address,
            "unit": None,
            "city": item.city,
            "state": item.state,
            "zip": item.zip_code,
            "lat": item.latitude,
            "lng": item.longitude,
            "county": item.county,
            "property_type": item.property_type,
            "beds": item.bedrooms,
            "baths": item.bathrooms,
            "sqft": item.sqft,
            "lot_sqft": item.lot_size * 43560 if item.lot_size is not None else None,
            "year_built": item.year_built,
            "year_renovated": item.year_renovated,
            "stories": item.stories,
            "hoa_monthly": item.hoa_monthly,
            "price": item.price,
            "price_is_placeholder": bool(item.price_is_placeholder),
            "price_per_sqft": item.price_per_sqft,
            "dom": item.days_on_market,
            "dom_mls": item.days_on_market_mls,
            "listing_status": item.listing_status,
            "listing_status_normalized": item.listing_status_normalized,
            "listed_at": _aware(item.listed_at),
            "listing_url": item.listing_url,
            "first_seen_at": _aware(item.created_at),
            "last_seen_at": _aware(item.last_seen_at),
            "delisted_at": _aware(item.delisted_at),
            "refreshed_at": _aware(item.refreshed_at),
            "flags": {
                "is_foreclosure": item.is_foreclosure,
                "is_reo": item.is_reo,
                "is_short_sale": item.is_short_sale,
                "is_auction": item.is_auction,
                "is_probate_or_estate": item.is_probate_or_estate,
                "is_as_is": item.is_as_is,
                "is_vacant": item.is_vacant,
                "is_tenant_occupied": item.is_tenant_occupied,
                "is_cash_only": item.is_cash_only,
                "is_age_restricted": item.is_age_restricted,
                "is_rental_restricted": item.is_rental_restricted,
                "allows_str": item.allows_short_term_rental,
            },
            "climate": {
                "flood": item.flood_factor,
                "fire": item.fire_factor,
                "heat": item.heat_factor,
                "wind": item.wind_factor,
            },
            "mobility": {
                "walk": item.walk_score,
                "transit": item.transit_score,
                "bike": item.bike_score,
            },
            "description": item.description[:1500] if item.description else None,
            "ai_summary": item.ai_summary,
            "photo_count": len(item.property_images),
            "updated_at": _aware(item.updated_at),
        }

    def _score_rows(self, properties: Sequence[Property]) -> tuple[list[dict[str, Any]], list[str]]:
        rows: list[dict[str, Any]] = []
        rejected: list[str] = []
        for item in properties:
            analysis = item.analysis
            if analysis is None:
                continue
            for lens in LENSES:
                score = getattr(analysis, f"{lens}_score")
                if score is None:
                    continue
                breakdown = getattr(analysis, f"{lens}_breakdown")
                try:
                    if not isinstance(breakdown, dict) or abs(recompose(breakdown) - score) > 0.01:
                        raise ValueError("ScoreBreakdownMismatch")
                    grade = getattr(analysis, f"{lens}_grade")
                    percentile = getattr(analysis, f"{lens}_percentile")
                    confidence = getattr(analysis, f"{lens}_confidence")
                    if grade is None or percentile is None or confidence is None:
                        raise ValueError("ScoreMetadataMissing")
                except (KeyError, TypeError, ValueError):
                    rejected.append(str(item.id))
                    continue
                rows.append({
                    "property_id": item.id,
                    "lens": lens,
                    "score": score,
                    "grade": grade,
                    "percentile": percentile,
                    "confidence": confidence,
                    "tier": "top" if percentile >= 95 else "strong" if percentile >= 80 else "rest",
                    "prev_score": None,
                    "prev_percentile": None,
                    "rationale": getattr(analysis, f"{lens}_rationale"),
                    "flags": _json(getattr(analysis, f"{lens}_flags"), []),
                    "breakdown": breakdown,
                    "version": analysis.scoring_config_version or "v3",
                    "computed_at": _aware(analysis.computed_at),
                })
        return rows, sorted(set(rejected))

    def _alert_candidates(
        self,
        properties: Sequence[Property],
        score_rows: Sequence[dict[str, Any]],
        previous: dict[tuple[str, str], ScoreState],
    ) -> list[Candidate]:
        """Scored rows of still-listed properties, paired with their pre-push cache state."""
        listed = {
            str(item.id): item
            for item in properties
            if item.delisted_at is None and item.listing_status_normalized in ACTIVE_STATUSES
        }
        candidates: list[Candidate] = []
        for row in score_rows:
            item = listed.get(str(row["property_id"]))
            if item is None:
                continue
            key = (str(row["property_id"]), str(row["lens"]))
            computed_at = row["computed_at"] or self.now
            candidates.append(Candidate(
                property_id=key[0],
                lens=key[1],
                new=ScoreState(float(row["percentile"]), str(row["tier"])),
                old=previous.get(key),
                score=float(row["score"]),
                grade=str(row["grade"]),
                computed_at=computed_at,
                address=str(item.address),
                price=cast(int | None, item.price),
            ))
        return candidates

    def _child_rows(self, properties: Sequence[Property]) -> dict[str, list[dict[str, Any]]]:
        rows: dict[str, list[dict[str, Any]]] = {
            name: [] for name in (
                "valuation", "agents", "images", "comps", "history", "features",
                "neighbourhood", "tax_history"
            )
        }
        for item in properties:
            if item.valuation is not None:
                value: PropertyValuation = item.valuation
                rows["valuation"].append({
                    "property_id": item.id, "market_value": value.market_value,
                    "market_value_method": value.market_value_method, "arv": value.arv,
                    "arv_method": value.arv_method, "price_to_value_ratio": value.price_to_value_ratio,
                    "comp_count": value.comp_count, "comp_median_ppsf": value.comp_median_ppsf,
                    "comp_p75_ppsf": value.comp_p75_ppsf, "rehab_low": value.rehab_cost_low,
                    "rehab_mid": value.rehab_cost_mid, "rehab_high": value.rehab_cost_high,
                    "condition_class": value.condition_class,
                    "rent_estimate_monthly": value.rent_estimate_monthly,
                    "rent_method": value.rent_method, "gross_yield": value.gross_yield,
                    "annual_taxes": value.annual_taxes, "annual_insurance": value.annual_insurance,
                    "annual_hoa": value.annual_hoa,
                    "annual_operating_expenses": value.annual_operating_expenses,
                    "noi_annual": value.noi_annual, "cap_rate": value.cap_rate,
                    "max_allowable_offer": value.max_allowable_offer,
                    "valuation_confidence": value.valuation_confidence,
                    "flags": _json(value.data_quality_flags, []),
                    "version": value.valuation_version or "unknown",
                    "computed_at": _aware(value.computed_at),
                })
            meta = _json(item.meta, {})
            if any(meta.get(key) for key in ("listing_agent", "listing_agent_phone", "listing_broker", "mls_id")):
                rows["agents"].append({
                    "property_id": item.id,
                    "listing_agent": meta.get("listing_agent"),
                    "listing_agent_phone": meta.get("listing_agent_phone"),
                    "listing_broker": meta.get("listing_broker"),
                    "mls_id": meta.get("mls_id"),
                    "source": meta.get("source") or item.primary_source,
                })
            for order, image in enumerate(
                sorted(item.property_images, key=lambda value: (value.sort_order, str(value.id)))[:12]
            ):
                rows["images"].append(
                    {"property_id": item.id, "sort_order": order, "url": image.url}
                )
            for comp in item.comps:
                rows["comps"].append({
                    "id": comp.id, "property_id": item.id, "address": comp.comp_address,
                    "price": comp.price, "beds": comp.bedrooms, "baths": comp.bathrooms,
                    "sqft": comp.sqft, "sold_date": comp.sold_date.date() if comp.sold_date else None,
                })
            for event in item.price_history:
                rows["history"].append({
                    "id": event.id, "property_id": item.id, "event_type": event.event_type,
                    "event": event.event, "price": event.price, "event_date": _aware(event.event_date),
                    "event_source": event.event_source, "is_rental": event.is_rental_event,
                })
            features: PropertyFeature | None = item.features
            if features is not None:
                flattened = dict(_json(features.raw_amenities, {}))
                for key in (
                    "heating", "cooling", "flooring", "construction_material", "roof",
                    "foundation", "interior_features", "appliances", "laundry_features",
                    "water_source", "sewer", "utilities", "furnished", "direction_faces",
                ):
                    value = getattr(features, key)
                    if value is not None:
                        flattened[key] = value
                rows["features"].append({"property_id": item.id, "features": flattened})
            schools = [
                {
                    "name": school.name, "type": school.school_type, "grades": school.grades,
                    "rating": school.rating, "distance_miles": school.distance_miles,
                    "level": school.level,
                }
                for school in item.schools
            ]
            location = item.location_score
            location_scores = {}
            if location is not None:
                for key in (
                    "pedestrian_score", "cycling_score", "transit_score", "car_score",
                    "parks_score", "groceries_score", "shopping_score", "nightlife_score",
                    "restaurants_score", "cafes_score", "daycares_score",
                    "primary_schools_score", "high_schools_score", "quiet_score",
                    "vibrant_score", "wellness_score",
                ):
                    location_scores[key] = getattr(location, key)
            rows["neighbourhood"].append({
                "property_id": item.id, "schools": schools,
                "location_scores": location_scores,
                "transport_count": len(item.transport_stops),
            })
            taxes_by_year = {tax.tax_year: tax for tax in item.tax_history}
            for tax in taxes_by_year.values():
                rows["tax_history"].append({
                    "property_id": item.id, "tax_year": tax.tax_year,
                    "tax_amount": tax.tax_amount, "assessed_value": tax.assessed_value,
                })
        return rows

    async def _replace_one_to_one(
        self, remote: AsyncConnection, name: str, market_id: UUID, rows: list[dict[str, Any]]
    ) -> None:
        table = self.store.table(name)
        properties = self.store.table("properties")
        ids = select(properties.c.id).where(properties.c.market_id == market_id)
        await remote.execute(delete(table).where(table.c.property_id.in_(ids)))
        await self.store.upsert(remote, name, rows, conflict=("property_id",))

    async def _replace_many(
        self,
        remote: AsyncConnection,
        name: str,
        market_id: UUID,
        rows: list[dict[str, Any]],
        conflict: Sequence[str],
    ) -> None:
        table = self.store.table(name)
        properties = self.store.table("properties")
        ids = select(properties.c.id).where(properties.c.market_id == market_id)
        await remote.execute(delete(table).where(table.c.property_id.in_(ids)))
        await self.store.upsert(remote, name, rows, conflict=conflict)

    async def _event_rows(
        self, slug: str, market_id: UUID, property_ids: set[UUID]
    ) -> list[dict[str, Any]]:
        if not property_ids:
            return []
        async with self.local_sessions() as session:
            events = (
                await session.execute(
                    select(ChangeEvent)
                    .where(ChangeEvent.market_slug == slug)
                    .where(ChangeEvent.property_id.in_(property_ids))
                    .order_by(ChangeEvent.observed_at, ChangeEvent.id)
                )
            ).scalars().all()
        return [
            {
                "id": event.id, "property_id": event.property_id, "market_id": market_id,
                "kind": event.kind, "detail": event.detail, "observed_at": _aware(event.observed_at),
            }
            for event in events
        ]

    def _market_daily(
        self, market_id: UUID, score_rows: Sequence[dict[str, Any]], properties: Sequence[Property]
    ) -> dict[str, Any]:
        counters: dict[str, Counter[str]] = {lens: Counter() for lens in LENSES}
        for row in score_rows:
            counters[str(row["lens"])][str(row["tier"])] += 1
        return {
            "market_id": market_id,
            "day": self.now.date(),
            "listings_active": sum(
                item.delisted_at is None and item.listing_status_normalized in ACTIVE_STATUSES
                for item in properties
            ),
            "tier_counts": {
                lens: {tier: counters[lens][tier] for tier in ("top", "strong", "rest")}
                for lens in LENSES
            },
        }

    def _freshness_values(
        self,
        freshness: MarketFreshness,
        properties: Sequence[Property],
        score_rows: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        pools = Counter(str(row["lens"]) for row in score_rows)
        return {
            "last_checked_at": _aware(freshness.last_checked_at),
            "next_check_at": _aware(freshness.next_check_at),
            "last_refreshed_at": _aware(freshness.last_refreshed_at),
            "check_status": freshness.check_status,
            "listings_active": freshness.listings_active,
            "changed_last_check": freshness.changed_last_check,
            "pool_size": {lens: pools[lens] for lens in LENSES},
            "source_status": {"redfin": freshness.check_status},
            "updated_at": self.now,
        }

    async def _finish_failed_run(self, run_id: UUID, error_class: str) -> None:
        try:
            if not self.store.tables:
                return
            async with self.store.transaction() as remote:
                await remote.execute(
                    update(self.store.table("runs"))
                    .where(self.store.table("runs").c.id == run_id)
                    .values(
                        finished_at=self.now,
                        status="failed",
                        error_class=error_class,
                        duration_s=0,
                    )
                )
        except Exception as finish_error:
            LOGGER.error(
                "Unable to finalize publisher run: error_class=%s",
                type(finish_error).__name__,
            )

    async def _ping_healthcheck(self, *, failed: bool) -> None:
        secret = settings.healthchecks_publisher_url
        if secret is None:
            return
        url = secret.get_secret_value().rstrip("/") + ("/fail" if failed else "")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception as exc:
            LOGGER.warning("Healthchecks ping failed: error_class=%s", type(exc).__name__)
