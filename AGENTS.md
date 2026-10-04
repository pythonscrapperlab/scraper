# AGENTS.md — AevoraeX engine (pipeline · freshness · publisher · scheduler)

Binding manual for any coding agent in this repository (`aevorex/` package; brand name is **AevoraeX**). Read fully first. Facts learned go in §12. Reasoning goes in `docs/decisions.md`. The discovery report at `docs/DISCOVERY.md` is the ground truth about what exists; this file tells you what to build on it.

---

## 1. System shape

```
THIS REPO — Windows 11 laptop, always on, outbound only
────────────────────────────────────────────────────────
aevorex/ pipeline (Redfin scrape → normalize → market-stats → valuation → 5-lens scoring)  ← behaviour frozen
local PostgreSQL 17 `aevorex_db`                                                          ← source of truth
NEW  freshness/   two-tier checks: search-level `check` every 2h, detail `refresh` nightly/on change
NEW  publisher/   pushes serving data + change events + demo snapshots to Supabase; never serves
NEW  scheduler/   Windows service (NSSM + APScheduler) driving checks/refresh/analyze per active city
                                   │ outbound Postgres connection (service role)
                                   ▼
Supabase Free (US East): schemas serving.* (engine writes) and app.* (web writes)   ─►  Next.js web reads via RLS
```

Principles
- **The laptop pushes; nothing calls it.** Downtime degrades freshness honestly (`late`/`failed`), never data.
- **Local Postgres is truth; the cloud is a 500 MB serving cache** rebuildable with `publisher rebuild`.
- **Scoring behaviour is frozen.** You may *add outputs* (structured breakdowns) and *add stages* (search-level checks, change events); you may not change weights, gates, inputs, or the score a property receives. Prove with fixture tests.
- **Say only what the data supports.** Everything the web shows must map to a column here.

---

## 2. Ground truth from discovery (do not re-derive; correct §12 if it changes)

- Source: **Redfin only** (HTML + embedded JSON; MLS facts arrive via Redfin). Zillow/Realtor.com are stubs with zero rows.
- Scope: CLI runs by **state → hard-coded city region IDs** (`scrapers/constants.py`). No zip scope. Median zip holds ~17 listings; a city holds hundreds to ~2,000.
- Identity: `redfin_id` → normalized `apn`+state → normalized address+zip. Unit numbers are stripped in address normalization (over-merge risk for condos); APN is state-scoped not county-scoped.
- Volume/runtime: 10,273 properties; scrape ≈ 1.6 s/URL sequential (`SCRAPER_CONCURRENT_URLS=1`); full `analyze` ≈ 17 min; San Jose full scrape took 140 min.
- Scores: **five lenses** in `property_analysis`: `motivated_seller`, `fix_flip`, `buy_hold`, `str` (= **mid-term 30+ day**), `airbnb` (nightly). Each has `_score`, `_grade` (A≥80 B≥65 C≥50 D≥35 F), `_percentile` (city pool, state fallback <30 rows), `_confidence`, `_rationale` (plain English), `_factors` (metrics JSON), `_flags` (caveats). Config `v3` in `aevorex/scoring/config.py`.
- **Scores are non-additive**: weighted component blend → gates/caps → multipliers → confidence shrink toward 50. Distributions are compressed (motivated-seller max 78.7; Orlando has 0 rows ≥ 80).
- Present: photos (URLs, ~35/property), features, schools, POIs (847k rows), location scores, walk/transit/bike, climate factors, HOA, tax history (amounts, partial assessed values), sold comps, valuation (market value, ARV, rehab range, rent estimate, NOI, cap rate, MAO), price/status history, listing agent/broker in `properties.meta`, distress flags from listing text, AI summary of photos.
- Absent: owner name/mailing, deeds/tenure (proxy: last sale event), tax delinquency status, permits, liens, code violations, court probate, STR municipal rules, ADR/occupancy data, run table, delist detection, change events.
- Machine: i5-8265U, 16 GB RAM, **~3 GB free on D:**; DB 848 MB (raw_scrapes 189 MB, POIs 162 MB, analysis 126 MB, images 79 MB). Webshare rotating proxy enabled. Credential defaults hard-coded in `config.py`.

---

## 3. Product decisions this engine serves (owner-approved 2026-09-26)

- **Five lenses, honestly named** — keys are the DB codes everywhere (web included): `motivated_seller` "Motivated seller", `fix_flip` "Fix & flip", `buy_hold` "Buy & hold", `str` "Mid-term rental (30+ days)", `airbnb` "Airbnb (nightly)".
- **Market = city** (`city`, `state`, Redfin region id, list of zips). Zips are filters. Plan limits are in cities.
- **Owner details are not a feature.** Pro ships "listing agent & broker contact" from `meta`. Owner/records data is a roadmap item requiring a new source.
- **Score presentation = anatomy, not a ledger**: components with weight and sub-score, adjustments (caps/multipliers/bonuses/confidence shrink), percentile in the city pool, grade, confidence, flags, rationale. No "baseline + Σ signals" anywhere.
- **"Worth a call" is a percentile tier within the market**, not an absolute score: `top` = percentile ≥ 95, `strong` ≥ 80, else `rest`. Absolute scores are shown, never used for colour.
- **Freshness is two-tier and stated honestly**: search-level *check* every 2 h (07:00–21:00 market-local) catches new listings, price cuts and status changes; detail *refresh* nightly and immediately for changed listings. Every surface shows `last_checked_at` and `last_refreshed_at`.
- Sources copy is "Redfin listings (MLS data as distributed via Redfin)". Coverage copy is a list of live cities.

---

## 4. Pipeline changes allowed (additive only)

1. **Structured breakdown export** — each scorer in `aevorex/scoring/*.py` gains a `breakdown()` result alongside its score, persisted to `property_analysis.<lens>_breakdown` (JSON, new nullable columns via Alembic). Shape in §6.2. The scorer's numeric result must be unchanged; a fixture test recomputes the stored score from `(components, adjustments)` using a pure function `recompose(breakdown) -> score` and asserts equality within 0.01 for ≥ 500 rows per lens.
2. **Search-level check** — a `freshness.check(city)` stage that requests only Redfin search results for the city region (paginated), extracts `redfin_id, price, status, dom, listed_at`, diffs against `properties`, writes `change_events`, marks disappeared listings for a delist confirmation pass (two consecutive absences → `delisted`), and enqueues detail fetches for new/changed ids. Detail fetch reuses the existing property-page path unchanged.
3. **Run bookkeeping** — a local `runs` table (kind `check|refresh|analyze|market_stats`, city, counts, timings, status, error class). Every CLI path records a run.
4. **Concurrency** — `SCRAPER_CONCURRENT_URLS` may be raised to 3–4 for detail fetches through the proxy, with per-source backoff. Log block/405 rates per run.
5. **Analyze after change** — after a check/refresh, run `value` and `score` for `needs_analysis` rows only (already supported), then `market-stats` nightly.
6. **Housekeeping** — keep only the latest `raw_scrapes` row per property (archive the rest to compressed JSONL on another drive), stop syncing POIs to cloud, and add a nightly local `pg_dump`. Rotate the DB and proxy credentials; remove hard-coded defaults from `config.py`.

Anything else touching `aevorex/scoring`, `valuation`, `market`, or normalizers requires an explicit owner instruction.

---

## 5. Cloud — Supabase Free

- One project, US East, session pooler (5432), service-role DB password only in `.env`. Two schemas: `serving.*` (engine writes; web reads via RLS) and `app.*` (web writes; engine reads `app.org_markets`, `app.thresholds`, `app.notification_settings`; writes `app.alert_events`, `app.email_queue`).
- Migrations: SQL in `supabase/migrations/`, applied with the Supabase CLI. `serving.schema_version` checked by the publisher on start.
- Size guard: warn 350 MB, stop adding cities 420 MB, page at 450 MB. Not synced: POIs, transport stops, raw payloads, descriptions longer than 1,500 chars (truncated), images beyond the first 12 per property.

---

## 6. Serving schema v2

### 6.1 Tables
```sql
serving.markets        (id uuid pk, city text, state text, region_id text, slug text unique  -- 'orlando-fl'
                        , tz text, zips text[], active bool, is_demo bool,
                        check_cadence_minutes int default 120,
                        last_checked_at timestamptz, next_check_at timestamptz, last_refreshed_at timestamptz,
                        check_status text check (check_status in ('ok','late','failed','warming')),
                        listings_active int, changed_last_check int, pool_size jsonb  -- {lens: n scored}
                        , source_status jsonb, updated_at)
serving.runs           (id uuid pk, market_id fk, kind text, trigger text, started_at, finished_at, status text,
                        counts jsonb, error_class text, duration_s int)
serving.properties     (id uuid pk  -- local properties.id
                        , market_id fk, redfin_id text, apn text, address text, unit text, city, state, zip text,
                        lat, lng, county, property_type, beds int, baths numeric, sqft int, lot_sqft numeric,
                        year_built int, year_renovated int, stories numeric, hoa_monthly numeric,
                        price int, price_is_placeholder bool, price_per_sqft numeric, dom int, dom_mls int,
                        listing_status text, listing_status_normalized text, listed_at, listing_url text,
                        first_seen_at, last_seen_at, delisted_at, refreshed_at,
                        flags jsonb  -- {is_foreclosure,is_reo,is_short_sale,is_auction,is_probate_or_estate,is_as_is,
                                     --  is_vacant,is_tenant_occupied,is_cash_only,is_age_restricted,is_rental_restricted,allows_str}
                        , climate jsonb  -- {flood,fire,heat,wind}
                        , mobility jsonb -- {walk,transit,bike}
                        , description text, ai_summary text, photo_count int, updated_at)
serving.scores         (property_id fk, lens text, score numeric, grade text, percentile numeric, confidence numeric,
                        tier text check (tier in ('top','strong','rest')), prev_score numeric, prev_percentile numeric,
                        rationale text, flags jsonb, breakdown jsonb, version text, computed_at,
                        primary key (property_id, lens))
serving.valuation      (property_id pk fk, market_value, market_value_method, arv, arv_method, price_to_value_ratio,
                        comp_count, comp_median_ppsf, comp_p75_ppsf, rehab_low, rehab_mid, rehab_high, condition_class,
                        rent_estimate_monthly, rent_method, gross_yield, annual_taxes, annual_insurance, annual_hoa,
                        annual_operating_expenses, noi_annual, cap_rate, max_allowable_offer, valuation_confidence,
                        flags jsonb, version text, computed_at)
serving.agents         (property_id pk fk, listing_agent text, listing_agent_phone text, listing_broker text,
                        mls_id text, source text)                       -- RLS: Pro+ only
serving.images         (property_id fk, sort_order int, url text, primary key (property_id, sort_order))  -- first 12
serving.comps          (id uuid pk, property_id fk, address text, price int, beds int, baths numeric, sqft int, sold_date date)
serving.history        (id uuid pk, property_id fk, event_type text, event text, price int, event_date timestamptz,
                        event_source text, is_rental bool)
serving.features       (property_id pk fk, features jsonb)             -- flattened property_features + amenities
serving.neighbourhood  (property_id pk fk, schools jsonb, location_scores jsonb, transport_count int)
serving.tax_history    (property_id fk, tax_year int, tax_amount int, assessed_value int, primary key (property_id, tax_year))
serving.change_events  (id uuid pk, property_id fk, market_id fk, kind text check (kind in
                        ('new','price_cut','price_increase','status','relisted','delisted','tier_up','tier_down','score_move')),
                        detail jsonb, observed_at timestamptz)
serving.market_daily   (market_id fk, day date, listings_active int, tier_counts jsonb, primary key (market_id, day))
serving.demo_snapshots (market_slug text, lens text, payload jsonb, generated_at, primary key (market_slug, lens))
serving.heartbeats     (id serial, at timestamptz, note text)
serving.schema_version (version int)
```
Indexes: `properties(market_id, listing_status_normalized)`, `properties(zip)`, `scores(lens, percentile desc)`, `scores(lens, tier)`, `change_events(market_id, observed_at desc)`, `history(property_id, event_date desc)`.

### 6.2 `scores.breakdown` shape (the contract with the web)
```json
{
  "lens": "motivated_seller", "version": "v3",
  "score": 62.4, "grade": "C", "percentile": 97.1, "confidence": 0.82, "tier": "top",
  "pool": {"level": "city", "key": "orlando-fl", "n": 1953},
  "components": [
    {"key": "distress", "label": "Distress signals", "weight": 0.30, "subscore": 92,
     "drivers": [{"label": "Probate/estate wording in listing", "value": true, "field": "is_probate_or_estate"}]},
    {"key": "price_cuts", "label": "Price-cut velocity and depth", "weight": 0.22, "subscore": 71,
     "drivers": [{"label": "2 cuts in 41 days", "value": 2, "field": "price_reduction_count"},
                 {"label": "Cumulative cut 9.1%", "value": 9.1, "field": "cumulative_price_cut_pct"}]}
  ],
  "adjustments": [
    {"type": "multiplier", "label": "Vacant", "value": 1.08, "reason": "is_vacant"},
    {"type": "cap",        "label": "Thin comp set", "value": 55, "applied": false},
    {"type": "confidence_shrink", "label": "Confidence 0.82 → pulled toward 50", "value": 0.82}
  ],
  "flags": ["no_ownership_or_legal_distress_data_available"],
  "rationale": "Motivated-seller score 62/100. 2 price cuts …"
}
```
Rules: every weight and every gate that exists in `scoring/config.py` for that lens appears (applied or not), so the web can render "not triggered" rows honestly. `recompose(breakdown) == score` is tested. Labels are plain English; no snake_case reaches the UI except in `field`.

### 6.3 RLS (web relies on it)
- Anon: `demo_snapshots`; `markets` public columns (`slug, city, state, tz, active, is_demo, last_checked_at, next_check_at, last_refreshed_at, check_status, listings_active, changed_last_check`).
- Authenticated: `properties, scores, valuation, images, comps, history, features, neighbourhood, tax_history, change_events, runs, market_daily` **only for markets the user's org has active** (`app.org_markets.market_slug` join).
- `agents`: same **and** `app.orgs.plan in ('pro','growth','brokerage')`.
- Service role bypasses (publisher only).

---

## 7. Freshness — how a market stays honest

- **check** (every 120 min, 07:00–21:00 market-local; once at 03:00): Redfin search results for the city region only. Diff → `change_events` (`new`, `price_cut`, `price_increase`, `status`, `relisted`); two consecutive absences → `delisted`. Enqueue detail refresh for new/changed. Update `markets.last_checked_at/next_check_at/check_status/listings_active/changed_last_check`. A check is `ok` only if every result page was fetched; else `failed`, and `last_checked_at` is **not** advanced.
- **refresh** (nightly 02:00 local, plus immediate for the enqueued ids): full property pages, existing path; `properties.refreshed_at`; `markets.last_refreshed_at` when the nightly completes.
- **analyze** after each check/refresh for `needs_analysis` rows; `market-stats` nightly after refresh. After analyze: recompute `tier`, write `tier_up/tier_down/score_move` (|Δpercentile| ≥ 10) change events.
- Budget: a check of a 2,000-listing city must complete in < 3 min (search pages only). A nightly refresh may take up to 90 min per city at concurrency 3; if the active-city set cannot complete nightly within 02:00–07:00, the scheduler reports it and the owner adds cities more slowly.

---

## 8. Publisher — `publisher push --market orlando-fl`

Per city, after check/refresh/analyze, in table-group transactions:
1. Upsert `markets` (from config + local counts).
2. Upsert `properties` for listings active or delisted < 30 days; prune older (cascade).
3. Upsert `scores` with `prev_*`, `tier`, `breakdown`, `rationale`; reject rows failing the recompose test (mark run `partial`, log property id only).
4. Upsert `valuation`, `agents`, `images` (first 12), `comps`, `history`, `features`, `neighbourhood`, `tax_history`.
5. Copy new `change_events` since last push.
6. **Alerts**: for each `app.org_markets` on this slug × `app.thresholds` (org, lens, min_percentile or tier): properties that moved into the tier/threshold since `prev_percentile` (or are `new` and qualify) → `app.alert_events` + `app.email_queue` (`threshold_alert`), honouring channel and quiet hours.
7. **Demo snapshot** if `is_demo`: per lens, top 3 rows full (address, facts, score, grade, percentile, tier, first two components' labels), next 5 stubs (tier, price band, dom), freshness block; no agent fields, no full breakdown.
8. `market_daily` rollup; freshness columns written last; size guard; heartbeat row + Healthchecks ping (`/fail` on exception).

Also: `publisher rebuild --market|--all`, `publisher status`, `publisher prune --dry-run`, `publisher drift`, `publisher wake`. Idempotent; batch upserts; < 60 s for a 2,000-listing city.

Morning brief: at 07:00 org-local the scheduler enqueues `morning_brief` per org per market with top N (plan) by the org's default lens, changes in 24 h, freshness. The engine never sends mail.

---

## 9. Scheduler & Windows

- APScheduler service `aevoraex-scheduler` via NSSM; auto-restart; logs JSON, daily rotation, no PII; Sentry; Healthchecks heartbeat (10 min) and per-run pings; clock-drift check.
- Active cities = `app.org_markets` (orgs `trialing|active`) ∪ `config/demo_markets.yaml` (start with: Orlando FL, Miami FL, Tampa FL, Vero Beach FL, San Jose CA). New city → `warming` check within 5 min.
- Concurrency: 1 check + 1 refresh at a time (laptop). Late detector every 5 min. Backoff 5→10→20→40 min, cap 2 h, on 405/blocks.
- `scripts/power.ps1`, `scripts/install-services.ps1`, `scripts/backup-local.ps1` (nightly `pg_dump` to a drive with space), `scripts/free-disk.ps1` (raw archive + POI/transport prune, dry-run first).

---

## 10. Quality bar

- `ruff`, `mypy --strict` on new packages; `pytest` against a local `aevorex_test` Postgres. Required tests: breakdown recompose (≥500 rows/lens); check diff matrix; delist two-absence rule; publisher idempotency; prune boundaries; tier and alert crossing; snapshot redaction (no agent fields, exactly 3 full + 5 stubs); size guard; scheduler window/stagger/DST; late detector.
- No PII in logs; credentials only in `.env`; `.env.example` complete.
- `docs/runbook.md` covers install, services, disk, credential rotation, rebuild, laptop-wipe recovery.

---

## 11. Milestones (one Codex session each; prompts in `ENGINE_PROMPTS.md`)

| # | Milestone | Delivers |
|---|---|---|
| E0 | Housekeeping & schema | free disk (raw archive, POI prune plan), credential rotation, remove defaults, local `runs` table, Alembic for `_breakdown` columns, Supabase project + `serving` v2 + `app` stubs + RLS, tooling, `.env.example`, runbook start |
| E1 | Breakdown export | `breakdown()` per scorer, persisted, `recompose` + tests, `docs/breakdown.md` per lens with every component/gate/multiplier from `scoring/config.py` |
| E2 | Two-tier freshness | `freshness.check`, change events, delist rule, detail enqueue, concurrency 3, run bookkeeping, analyze-after-change, timings recorded |
| E3 | Publisher | all §8 steps except alerts/brief; one city pushed; `status/rebuild/prune/drift`; snapshot redaction tests |
| E4 | Scheduler on Windows | §9 complete, installed, 48-hour run on demo cities with `publisher status` before/after |
| E5 | Alerts, brief, RLS proof, soak | alert events + email queue, morning-brief enqueue, RLS tests with anon/Starter/Pro JWTs, 7-day soak log, `docs/handover-web.md` mapping every web `DataApi` method to serving views |

---

## 12. Facts log (append)
- 2026-10-04 — Public snapshot v3 (`aevorex/publisher/snapshots.py`, `reasons.py`): per lens, 3 open + 5 teaser rows chosen from active scored listings ordered by lens **score** (`rank`/`pool_size` are the position in the whole city pool, so the first card can be #2). A row is shown only with a real non-placeholder price, days listed, a full street address (no "undisclosed address", no street number 00), >=3 photos (first 5 kept), not vacant land/timeshare, a computable headline, and >=3 numeric favourable reasons; rows that fail are skipped, never padded. Reasons come from scorer factors and valuation only; the one piece of listing text allowed is a regex-validated distress/urgency phrase. Summary is 1-2 sentences of the scorer rationale minus header, "Confirm..." instructions, and any sentence naming another state (the airbnb rationale hard-codes Florida). `scripts/check_demo_snapshots.py` is the pre-publish gate. Known data limits at publish: all five cities' freshness shows `failed` (last check 2026-10-03); top rental-lens rows contain modelled rents with implausible yields (e.g. $10,500/mo on a $350k house) and all three Orlando fix & flip cards are projected losses at asking, because top-of-ranking selection concentrates model outliers.
- 2026-10-04 — Public snapshot v2: three active/new scored listings with positive price, DOM, first photo, formatted address, active scored-city rank/pool, top/strong tier, lens metric and allowlisted reasons; five teasers have only price_band/dom. No contacts or full breakdown. `scripts/publish_demo_snapshots.py` writes only snapshots, with no alerts/email; cloud schema v3 has an unchanged four-column snapshot table, validated by this utility without bypassing the full publisher's older v2 guard.
- 2026-09-26 — Discovery complete (`docs/DISCOVERY.md`). Decisions: five lenses; market = city; owner data dropped for agent/broker contact; anatomy not ledger; percentile tiers; two-tier freshness; Supabase Free; no FastAPI; laptop pushes only.
- 2026-09-30 — E0 added required credential settings, local CLI run bookkeeping, nullable five-lens breakdown outputs, tooling, and Supabase serving-v2/app-stub/RLS migrations. The linked project's provisional serving tables were empty when the restored section 6 contract was reconciled; forward migration `20260929202642` applied the correction and remote readback confirmed schema version 2 with RLS on all 17 serving tables.
- 2026-09-30 — E1 restored the audited frozen v3 scoring sources from the repository stash after `main` was found to contain stale v2 scorer files, added complete five-lens breakdown exports and pure recomposition, and populated all 10,273 local analysis rows. Across 46,791 non-null lens scores, recompose failures were zero.
- 2026-09-30 — E2 added atomic Redfin search-only city checks, durable pending-detail and two-absence delist state, local change/tier events, concurrency-3 detail refresh through the existing property pipeline, and changed-row analysis. Live checks completed Orlando (2,539 listings, 8 pages) in 25.28 s and Vero Beach (556 listings, 2 pages) in 17.96 s; immediate repeats produced zero events and zero queue writes.
- 2026-10-03 — E3 added the outbound-only serving-v2 publisher, exact 30-day retention, recomposition-gated score publishing, redacted demo snapshots, size guard, status/rebuild/prune/drift/wake commands, and live Orlando/Vero Beach pushes. Idempotent pushes completed in 42.52 s and 26.20 s with zero rejected scores; the final cloud cache was 88.09 MiB. No email or alert delivery was added.
- 2026-10-03 — E4 added the `aevoraex-scheduler` Windows service (NSSM + APScheduler): market-local slots with hash stagger and DST handling, advisory-lock lanes, shared Redfin backoff, late detector, queue drainer, run-now, dry-run preview, clock-drift check, JSON/PII-scrubbed logging, Sentry and Healthchecks hooks, nightly verified `pg_dump` task and power script. First live pass found that days-on-market drift re-queued ~every listing daily (2,243 of Orlando's 2,565); DOM-only changes no longer queue a fetch. Miami (11458) and Tampa (18142) were enabled in `constants.py`. Soak results are in `docs/progress.md`.
- 2026-10-03 — E5 added enqueue-only threshold alerts (cache-state crossing, atomic with scores, quiet hours, idempotent) and a 07:00-11:00 org-local morning-brief sweep; the RLS proof suite found and fixed missing `serving.markets(id, slug)` grants for `authenticated`; policies were rewritten uncorrelated (Orlando shortlist 1,363 ms -> 28 ms); added `serving.v_shortlist/v_property/v_agent/v_market_public` (security invoker), `docs/schema.md`, `docs/handover-web.md` and the daily soak collector. The seven-day soak is in progress; the Auth admin-API RLS variant needs `SUPABASE_SERVICE_ROLE_KEY` and has not run.

