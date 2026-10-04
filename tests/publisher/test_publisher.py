from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import URL, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from aevorex.db.models import MarketFreshness, Property, PropertyImage
from aevorex.publisher.reasons import (
    _a,
    build_reasons,
    build_summary,
    headline,
    percent,
    plain,
    signed,
)
from aevorex.publisher.remote import RemoteStore
from aevorex.publisher.service import Publisher, apply_rejections
from aevorex.publisher.snapshots import (
    build_demo_snapshot,
    format_address,
    full_address,
    photo_urls,
)
from aevorex.publisher.types import MarketDefinition, PublishResult, classify_size
from tests.publisher.conftest import scored_property


@pytest.mark.parametrize(
    ("megabytes", "action"),
    [
        (349.999, "ok"),
        (350, "warn"),
        (419.999, "warn"),
        (420, "stop_new_cities"),
        (449.999, "stop_new_cities"),
        (450, "page"),
    ],
)
def test_size_guard_exact_thresholds(megabytes: float, action: str) -> None:
    assert classify_size(round(megabytes * 1024 * 1024)).action == action


def _scored_property(position: int) -> Property:
    item, analysis = scored_property(position)
    item.analysis = analysis
    item.last_seen_at = datetime(2026, 10, 3)
    return item


MARKET = MarketDefinition(slug="orlando-fl", city="Orlando", state="FL", region_id="13655",
                          timezone="America/New_York", is_demo=True)
OPEN_KEYS = {"address", "photos", "price", "beds", "baths", "sqft", "days_listed", "property_type",
             "year_built", "rank", "pool_size", "tier", "headline", "reasons", "summary"}
LENSES = ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")


def _freshness() -> MarketFreshness:
    return MarketFreshness(slug="orlando-fl", city="Orlando", state="FL", region_id="13655",
                           check_status="ok")


def _numeric(reasons: list[str]) -> int:
    return sum(any(ch.isdigit() for ch in text) for text in reasons)


@pytest.mark.parametrize("lens", LENSES)
def test_demo_snapshot_has_exact_shape_and_redaction(lens: str) -> None:
    payload = build_demo_snapshot(MARKET, lens, [_scored_property(p) for p in range(8)], _freshness())
    assert len(payload["full"]) == 3
    assert len(payload["stubs"]) == 5
    assert all(set(stub) == {"price_band", "days_listed"} for stub in payload["stubs"])
    first = payload["full"][0]
    assert set(first) == OPEN_KEYS
    assert first["address"] == "100 Publisher Street, Orlando, FL 32801"
    assert first["photos"] == [f"https://img.example.invalid/0/{i}.jpg" for i in range(5)]
    assert (first["rank"], first["pool_size"], first["tier"]) == (1, 8, "top")
    assert first["days_listed"] == 1 and first["property_type"] == "Single-family"
    assert first["headline"]["label"] and first["headline"]["value"]
    assert 3 <= len(first["reasons"]) <= 5 and _numeric(first["reasons"]) >= 3
    assert first["summary"] == "Sale priced 10% under its original ask."
    serialized = str(payload).casefold()
    for forbidden in ("agent", "broker", "phone", "breakdown", "e-mail", "@"):
        assert forbidden not in serialized


def test_headlines_per_lens_and_estimates_are_marked() -> None:
    rows = [_scored_property(p) for p in range(8)]
    heads = {lens: build_demo_snapshot(MARKET, lens, rows, _freshness())["full"][0]["headline"]
             for lens in LENSES}
    assert heads["motivated_seller"] == {"label": "Total price cut", "value": "−10%",
                                         "estimate": False}
    assert heads["fix_flip"]["label"] == "Estimated ARV" and heads["fix_flip"]["value"] == "$450,000"
    assert heads["fix_flip"]["gap_to_max_offer"] == 30_000
    assert heads["buy_hold"]["value"] == "7.5%"
    assert heads["str"]["value"] == "$4,000/mo"
    assert heads["airbnb"]["value"] == "$300/night"
    assert all(heads[lens]["estimate"] for lens in LENSES if lens != "motivated_seller")


def test_demo_excludes_unpresentable_rows_without_changing_pool_rank() -> None:
    rows = [_scored_property(position) for position in range(16)]
    rows[0].listing_status_normalized = "sold"
    rows[1].property_images = rows[1].property_images[:2]      # fewer than three photos
    rows[2].price = None
    rows[3].days_on_market = None
    rows[4].price_is_placeholder = True
    rows[5].address = "undisclosed address"
    rows[6].address = "00 cinnabar hills rd"
    rows[7].property_type = "Vacant Land"
    payload = build_demo_snapshot(MARKET, "motivated_seller", rows, _freshness())
    assert [row["rank"] for row in payload["full"]] == [8, 9, 10]
    assert payload["full"][0]["pool_size"] == 15              # the sold listing is not in the pool
    assert payload["full"][0]["headline"]["value"] == "−10%"
    assert payload["full"][0]["reasons"][0].startswith("Price cut twice in 9 days on market")


def test_ranking_follows_score_not_percentile() -> None:
    rows = [_scored_property(position) for position in range(9)]
    # Position 8 has the best score but the worst percentile: score decides.
    for row in rows[:8]:
        row.analysis.motivated_seller_percentile = 99.0
    best = rows[8].analysis
    best.motivated_seller_percentile = 96.0
    best.motivated_seller_score = 95.0
    best.motivated_seller_breakdown = {"components": [
        {"key": "one", "weight": 1.0, "subscore": 95.0}], "adjustments": []}
    payload = build_demo_snapshot(MARKET, "motivated_seller", rows, _freshness())
    assert payload["full"][0]["address"].startswith("108 ")
    assert payload["full"][0]["rank"] == 1


def test_a_card_needs_three_numeric_reasons() -> None:
    rows = [_scored_property(position) for position in range(9)]
    rows[0].analysis.motivated_seller_factors = {"price_reduction_count": 1}
    payload = build_demo_snapshot(MARKET, "motivated_seller", rows, _freshness())
    assert payload["full"][0]["rank"] == 2
    assert all(_numeric(row["reasons"]) >= 3 for row in payload["full"])


def test_fix_flip_never_claims_a_bargain_above_the_maximum_offer() -> None:
    item = _scored_property(0)
    factors = {"arv": 500_000, "max_allowable_offer": 200_000, "projected_profit": -9_000,
               "cash_invested": 60_000, "roi_pct": -15.0, "valuation_confidence": 0.9}
    item.analysis.fix_flip_factors = factors
    head = headline(item, "fix_flip", factors)
    assert head is not None and head["gap_to_max_offer"] == -100_000
    assert head["detail"] == "Asking is $100,000 over our max offer of $200,000"
    text = " ".join(build_reasons(item, "fix_flip", factors, item.analysis.fix_flip_breakdown)).lower()
    assert "under our maximum offer" not in text and "profit" not in text


def test_photos_are_the_first_five_in_listing_order() -> None:
    item = _scored_property(0)
    item.property_images = [
        PropertyImage(url=f"https://img.example.invalid/{i}.jpg", sort_order=order)
        for i, order in [(6, 6), (3, 3), (1, 1), (0, 0), (2, 2), (4, 4), (5, 5), (3, 7)]
    ]
    assert photo_urls(item) == [f"https://img.example.invalid/{i}.jpg" for i in range(5)]


@pytest.mark.parametrize(("raw", "expected"), [
    ("2627 s bayshore dr unit 1202", "2627 S Bayshore Dr #1202"),
    ("1075 nw 100th st", "1075 NW 100th St"),
    ("3500 coral way 1010", "3500 Coral Way #1010"),
    ("4825 sw 152nd ct unit e29", "4825 SW 152nd Ct #E29"),
    ("8990 sw 24th st 226", "8990 SW 24th St #226"),
    ("1850 bay rd #3g", "1850 Bay Rd #3G"),
    ("200 state rd 7", "200 State Rd 7"),
    ("12 mcarthur ave", "12 McArthur Ave"),
])
def test_address_formatting(raw: str, expected: str) -> None:
    assert format_address(raw) == expected


@pytest.mark.parametrize("street", ["undisclosed address", "00 cinnabar hills rd", "oak st", ""])
def test_unusable_streets_have_no_full_address(street: str) -> None:
    item = _scored_property(0)
    item.address = street
    assert full_address(item) is None
    item.address = "5 oak st"
    item.zip_code = ""
    assert full_address(item) is None


def test_number_formatting_keeps_real_zeros() -> None:
    assert percent(60.0) == "60%" and percent(10.0) == "10%" and percent(5.9) == "5.9%"
    assert percent(11.2) == "11%" and plain(100.0, 0) == "100" and plain(5.0) == "5"
    assert signed(-1.1) == "−1.1" and signed(0.0) == "0"
    assert [_a(x) for x in ("8.8%", "11%", "18%", "110%", "233%", "80%", "7.5%")] == [
        "an 8.8%", "an 11%", "an 18%", "a 110%", "a 233%", "an 80%", "a 7.5%"]


def test_implausible_market_growth_is_not_stated() -> None:
    item = _scored_property(0)
    factors = dict(item.analysis.buy_hold_factors, market_yoy_price_change_pct=154.0)
    assert not any("year over year" in text for text in
                   build_reasons(item, "buy_hold", factors, item.analysis.buy_hold_breakdown))


def test_summary_skips_header_instructions_other_states_and_contact_data() -> None:
    rationale = ("Airbnb grade C (51/100). Short-term-rental legality is UNVERIFIED — Florida regulates "
                 "sub-30-day letting. Confirm the local ordinance before relying on this score. "
                 "Proxy economics: $690/night gives $117,933 gross. Call the listing agent at 407-555-0100. "
                 "Key risks: none.")
    assert build_summary(rationale, "CA") == "Proxy economics: $690/night gives $117,933 gross."
    assert build_summary(rationale, "FL").startswith("Short-term-rental legality is UNVERIFIED")
    assert "Confirm" not in build_summary(rationale, "FL")
    assert build_summary(None) == "" and build_summary("   ") == ""


def test_distress_phrases_are_allowlisted_before_publication() -> None:
    item = _scored_property(0)
    item.distress_signals = {"is_probate_or_estate": ["Estate Sale", "call Bob 407-555-0100"],
                             "is_as_is": ["sold as-is"]}
    factors = {**item.analysis.motivated_seller_factors, "distress_signals": ["probate_or_estate"]}
    reasons = build_reasons(item, "motivated_seller", factors, item.analysis.motivated_seller_breakdown)
    assert "Listing says 'estate sale' and 'sold as-is'" in reasons
    assert "407" not in " ".join(reasons) and "Bob" not in " ".join(reasons)


def test_recompose_rejection_marks_run_partial() -> None:
    item = _scored_property(0)
    item.analysis.motivated_seller_breakdown["components"][0]["subscore"] = 1
    publisher = object.__new__(Publisher)
    rows, rejected = publisher._score_rows([item])
    result = PublishResult(market="orlando-fl")
    apply_rejections(result, rejected)
    assert rows == []
    assert result.status == "partial"
    assert result.telemetry()["rejected_scores"] == 1


@pytest.mark.asyncio
@pytest.mark.database
async def test_remote_batch_upsert_is_idempotent(aevorex_test_db) -> None:
    database = aevorex_test_db
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=database.user,
            password=database.password,
            host=database.host,
            port=database.port,
            database=database.database,
        )
    )
    store = RemoteStore(engine, batch_size=2)
    await store.initialize()
    market_id = uuid4()
    row = {
        "id": market_id,
        "city": "Orlando",
        "state": "FL",
        "region_id": "13655",
        "slug": "orlando-fl",
        "tz": "America/New_York",
        "zips": ["32801"],
    }
    async with store.transaction() as connection:
        await store.upsert(connection, "markets", [row], conflict=("slug",))
        await store.upsert(connection, "markets", [row], conflict=("slug",))
    async with engine.connect() as connection:
        markets = store.table("markets")
        count = await connection.scalar(
            select(func.count()).where(markets.c.slug == "orlando-fl")
        )
    assert count == 1
    await store.close()


@pytest.mark.asyncio
@pytest.mark.database
async def test_score_previous_values_move_only_when_current_changes(aevorex_test_db) -> None:
    database = aevorex_test_db
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=database.user,
            password=database.password,
            host=database.host,
            port=database.port,
            database=database.database,
        )
    )
    store = RemoteStore(engine)
    await store.initialize()
    market_id = uuid4()
    property_id = uuid4()
    async with store.transaction() as connection:
        await store.upsert(
            connection,
            "markets",
            [{
                "id": market_id,
                "city": "Orlando",
                "state": "FL",
                "region_id": "13655",
                "slug": "score-history-fl",
                "tz": "America/New_York",
            }],
            conflict=("slug",),
        )
        await store.upsert(
            connection,
            "properties",
            [{
                "id": property_id,
                "market_id": market_id,
                "address": "Test",
                "city": "Orlando",
                "state": "FL",
                "zip": "32801",
            }],
            conflict=("id",),
        )
    base = {
        "property_id": property_id,
        "lens": "motivated_seller",
        "score": 60,
        "grade": "C",
        "percentile": 70,
        "confidence": 1,
        "tier": "rest",
        "prev_score": None,
        "prev_percentile": None,
        "version": "v3",
        "computed_at": datetime(2026, 10, 3, tzinfo=UTC),
    }
    async with store.transaction() as connection:
        await store.upsert(
            connection,
            "scores",
            [base],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        await store.upsert(
            connection,
            "scores",
            [base],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        changed = {**base, "score": 65, "grade": "B", "percentile": 82, "tier": "strong"}
        await store.upsert(
            connection,
            "scores",
            [changed],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        stale = {**base, "lens": "fix_flip"}
        await store.upsert(
            connection,
            "scores",
            [stale],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        assert await store.delete_market_scores_not_in(
            connection,
            market_id,
            [(property_id, "motivated_seller")],
        ) == 1
        await store.upsert(
            connection,
            "scores",
            [changed],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
    async with engine.connect() as connection:
        scores = store.table("scores")
        row = (
            await connection.execute(
                select(
                    scores.c.score,
                    scores.c.percentile,
                    scores.c.prev_score,
                    scores.c.prev_percentile,
                ).where(scores.c.property_id == property_id)
            )
        ).one()
    assert tuple(row) == (65, 82, 60, 70)
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(scores)) == 1
    await store.close()
