# Serving schema and web views (E5)

Authoritative contract for the tables is `AGENTS.md` section 6; this document records what E5
added on top of it and how the web should read it. Migrations live in `supabase/migrations/`
and were applied to the linked project with `supabase db push` on 2026-10-03.

| Migration | Purpose |
|---|---|
| `20261003115500_e5_rls_policy_columns` | Fix: `authenticated` may read `serving.markets.id, slug` (see "RLS finding") |
| `20261003120000_e5_alerts` | Additive `app.*` columns for alerts, quiet hours, brief |
| `20261003120100_e5_views` | The four `serving.v_*` views and `change_events(property_id, observed_at)` index |
| `20261003121000_e5_fast_policies` | Same policies, rewritten so Postgres evaluates them once per statement |
| `20261004090000_e6_serving_v3` | `serving.schema_version` = 3; `app.alert_events.property_id` nullable, FK `ON DELETE SET NULL` |
| `20261004091000_e6_security` | Revoke execute on `public.rls_auto_enable()` from `public`, `anon`, `authenticated` |

## Views

All four are `security_invoker = true`: they run with the caller's privileges, so the base-table
RLS applies and a view can never show a row the caller could not read directly. Columns are an
explicit list, so a column added to a base table later does not leak through a view.

| View | Who | Grain | Contents |
|---|---|---|---|
| `serving.v_market_public` | anon, authenticated | one row per active market | the 12 public market columns: `slug, city, state, tz, active, is_demo, last_checked_at, next_check_at, last_refreshed_at, check_status, listings_active, changed_last_check` |
| `serving.v_shortlist` | authenticated | one row per **listed** property (`delisted_at is null`) | identity and facts, `price`, `dom`, `refreshed_at`, `photo_count`; for each lens `<lens>_score`, `<lens>_grade`, `<lens>_percentile`, `<lens>_tier` (20 columns, `null` when that lens could not be scored); `latest_change_kind`, `latest_change_at` |
| `serving.v_property` | authenticated | one row per property (including delisted within 7 days) | all `properties` columns, the whole `valuation` row (its `flags` as `valuation_flags`), `features`, `schools`, `location_scores`, `transport_count`, market freshness (`market_last_checked_at`, `market_last_refreshed_at`, `market_tz`), and `scores` - a JSON object keyed by lens holding `score, grade, percentile, confidence, tier, prev_score, prev_percentile, rationale, flags, breakdown, version, computed_at` |
| `serving.v_agent` | authenticated, **Pro+ only by RLS** | one row per property with agent data | `listing_agent, listing_agent_phone, listing_broker, mls_id, source`, `market_slug` |

Lens keys are the DB codes: `motivated_seller`, `fix_flip`, `buy_hold`, `str` (mid-term, 30+ days),
`airbnb` (nightly). Absent data stays `null`; nothing is defaulted or estimated in a view.
`tier` is `top` (percentile >= 95), `strong` (>= 80) or `rest`, within the property's market.
Child tables that have no view (`images`, `comps`, `history`, `tax_history`, `change_events`,
`market_daily`, `runs`) are read directly - see `docs/handover-web.md`.

## RLS finding fixed in E5

The E0 policies for every "subscribed" table join `serving.markets` on `sm.id` and `sm.slug`.
A policy subquery runs with the *caller's* privileges, and `authenticated` only held column
grants on the 12 public columns, so a signed-in user's read of `properties`, `scores`, `valuation`
and the rest failed with `permission denied for table markets`. The RLS proof tests found it
before any web user did. `grant select (id, slug) on serving.markets to authenticated` fixes it
without exposing `region_id`, `zips`, `pool_size` or `source_status`; rows remain limited by
`markets_select_subscribed`. `anon` is unchanged: exactly the 12 public columns.

## Performance: EXPLAIN ANALYZE on Orlando (1,196 listed properties, 5,841 scores)

Measured on the live project as a throwaway Pro org member (RLS fully evaluated), in a transaction
that is rolled back; reproduce with `PYTHONPATH=. python scripts/explain_views.py orlando-fl --plans`.

| Query | Before (ms) | After (ms) |
|---|---:|---:|
| `v_shortlist` top 50 by `motivated_seller_percentile` | 1,363 | 28 |
| `v_shortlist` where `fix_flip_tier = 'top'` | 162 | 13 |
| `v_shortlist` zip + price filter | 3.7 | 1.3 |
| `v_property` single property | 24 | 12 |
| `v_agent` for the market | 70 | 18 |
| `v_market_public` | 4.4 | 0.2 |

The cost was RLS, not the views: the 0002 policies were correlated `EXISTS` subqueries re-run for
every score row (about 186,000 buffer hits for one shortlist). The fast-policy migration rewrites
them as uncorrelated `IN (...)` subqueries that Postgres hashes once per statement; the full RLS
proof suite passes unchanged against both versions. One index was added,
`change_events(property_id, observed_at desc)`, which serves the "latest change" lateral join.
The remaining sequential scans are the whole-market `agents` read and the one-row-per-market table;
both are the correct plan at this size.

## `app.*` additions (engine needs; web owns the final shape)

| Table | Added | Why |
|---|---|---|
| `app.orgs` | `tz text default 'America/New_York'`, `default_lens text default 'motivated_seller'` | brief at 07:00 org-local, lens of the brief |
| `app.thresholds` | `min_percentile numeric (0,100]`; `tier` now nullable; check that one of them is set | "min_percentile or tier"; percentile wins when both are set |
| `app.notification_settings` | `quiet_hours_enabled bool default true`, `quiet_start time default '21:00'`, `quiet_end time default '07:00'` | quiet hours, org-local; `start > end` crosses midnight |
| `app.alert_events` | `dedupe_key text` (unique per org); `tier` check now allows `rest` | idempotency; a percentile threshold can fire for a `rest` tier |
| `app.email_queue` | `send_after timestamptz default now()`, `dedupe_key text` (unique per org); pending index on `(status, send_after)` | quiet-hour deferral and idempotency |

## Alert and brief semantics

- A threshold alert fires when a score **enters** the org's threshold on a push: it qualified after
  the push and did not before it (including a new listing that qualifies). Staying above the line
  or falling below it never alerts, and repeating a push alerts nothing.
- Evaluation happens inside the same transaction as the score upsert, so a failed alert write rolls
  the scores back and the crossing is retried by the next push rather than lost.
- The first push of a market (or a rebuild) has no earlier state to compare with and enqueues
  nothing; the count is reported as `alert_baseline_suppressed`.
- E-mail is queued per member (`tier_alerts` and `email` on), deferred through quiet hours with
  `send_after`; an in-app `alert_events` row is written once per org when any member has
  `tier_alerts` and `in_app` on. The engine never sends mail.
- Morning brief: a sweep every 10 minutes queues one `morning_brief` per member per org-market per
  org-local day from 07:00 until 11:00 local (later than that it is skipped and counted as
  `brief_too_late`, never sent stale). The count of properties depends on plan:
  starter 5, pro 10, growth 15, brokerage 25 (`config/scheduler.yaml`).

## Serving v3 payload contract (E6)

Version 3 changes the **content** of existing columns, not the column set. The publisher sends only what the
web renders, and only when it changed (a property whose row, scores or child rows hash identically to the last
successful push is not touched).

| Table / column | v3 content |
|---|---|
| `properties.description` | Contact details removed, then capped at **600 characters** (word boundary, ends with an ellipsis). Removed: phone numbers, e-mail addresses, web addresses, social handles and "call/text/contact/e-mail/ask for/speak with `<person>`" phrases (replaced by "contact the listing agent"; numbers and addresses by `[contact removed]`). Generic wording such as "call home" or "contact the listing agent" is kept. Not detected: spelled-out digits and "name at example dot com". `listing_url` is unchanged (public Redfin page). |
| `properties.ai_summary` | Same redaction, no cap (longest observed is 668 characters). |
| `properties.dom`, `dom_mls`, `last_seen_at`, `refreshed_at`, `updated_at` | Written whenever the row is written, but **a change in these alone does not trigger a write**. On an otherwise unchanged listing they are as of the last real change: derive days-on-market from `listed_at` where exactness matters, and use the market's `last_refreshed_at` as the freshness surface. |
| `features.features` | A curated object of at most **25** named keys, only those with a value (values are trimmed strings, at most 160 characters). There is **no** raw amenities blob. See the list below. |
| `history` | Sale and listing events only (**never rentals**; the web filters them out anyway), newest first, **at most 15 events within the last 7 years**. |
| `tax_history` | The latest **5** tax years. |
| `comps` | At most **6** sold comps, most recent sale first. |
| `images` | Unchanged: first 12. |
| `scores.breakdown` | Same shape as `AGENTS.md` 6.2. Drivers with a `null` value are dropped; a driver's `field` is kept only when it names a column the web already holds (`serving.properties`, `serving.valuation`, or a flag key), otherwise it is removed. Components, weights, subscores and adjustments are untouched, so `recompose(breakdown)` still equals `score`. |
| `change_events` | Only events newer than the market's last successful push are sent (the first push of a market sends the last 14 days). |
| `market_daily`, `runs`, `heartbeats` | Pruned remotely to 90, 30 and 7 days. |
| Delisted properties | Kept **7 days** after `delisted_at` (was 30), then removed with their children. |

### The 25 feature keys

Structured (13): `heating`, `cooling`, `flooring`, `construction_material`, `roof`, `foundation`,
`interior_features`, `appliances`, `laundry_features`, `water_source`, `sewer`, `utilities`, `furnished`.

From the listing's amenities (12): `style`, `levels`, `exterior_features`, `parking_features`,
`garage_spaces`, `pool`, `fireplace`, `waterfront`, `pets_allowed`, `hoa_includes`, `hoa_amenities`,
`property_condition`.

Deliberately not published: `Directions`, `Attribution Contact`, virtual-tour URLs, universal property ids, APNs,
school names (already in `neighbourhood`), and every other raw key. Adding a key is a code change in
`aevorex/publisher/slim.py` and a size decision.

### Alert history survives prunes

`app.alert_events.property_id` is nullable with `ON DELETE SET NULL`. A pruned or rebuilt property keeps its
alert rows (address and price live in `payload`); the web must handle a `null` `property_id` by not linking.
