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

## Views

All four are `security_invoker = true`: they run with the caller's privileges, so the base-table
RLS applies and a view can never show a row the caller could not read directly. Columns are an
explicit list, so a column added to a base table later does not leak through a view.

| View | Who | Grain | Contents |
|---|---|---|---|
| `serving.v_market_public` | anon, authenticated | one row per active market | the 12 public market columns: `slug, city, state, tz, active, is_demo, last_checked_at, next_check_at, last_refreshed_at, check_status, listings_active, changed_last_check` |
| `serving.v_shortlist` | authenticated | one row per **listed** property (`delisted_at is null`) | identity and facts, `price`, `dom`, `refreshed_at`, `photo_count`; for each lens `<lens>_score`, `<lens>_grade`, `<lens>_percentile`, `<lens>_tier` (20 columns, `null` when that lens could not be scored); `latest_change_kind`, `latest_change_at` |
| `serving.v_property` | authenticated | one row per property (including delisted within 30 days) | all `properties` columns, the whole `valuation` row (its `flags` as `valuation_flags`), `features`, `schools`, `location_scores`, `transport_count`, market freshness (`market_last_checked_at`, `market_last_refreshed_at`, `market_tz`), and `scores` - a JSON object keyed by lens holding `score, grade, percentile, confidence, tier, prev_score, prev_percentile, rationale, flags, breakdown, version, computed_at` |
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
