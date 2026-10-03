# Engineering decisions

## 2026-09-30 — E0 housekeeping and schema

- **Scoring is frozen.** E0 changes no scorer, weight, threshold, gate, percentile,
  confidence, rationale, grade, or score write. The five new breakdown fields are
  nullable JSONB outputs and remain empty until a later milestone.
- **Five canonical lenses.** Schema uses `motivated_seller`, `fix_flip`, `buy_hold`,
  `str`, and `airbnb`, matching the audited v3 engine.
- **Run scope is structured.** `runs.scope` and `runs.counts` are JSONB so future
  city/zip stages can add dimensions without altering the table. Counts accept only
  nested numeric, boolean, and null aggregates.
- **Run status is explicit.** CLI invocations transition from `running` to
  `succeeded`, `failed`, or `cancelled`; `duration_s` is measured from persisted UTC
  timestamps. Only the exception class is retained.
- **One run per CLI invocation.** Multi-source/state scrape loops aggregate into one
  row. `analyze` owns one parent row containing nested stage counts. The scheduler
  stays `running` until it stops.
- **No PII in telemetry.** Run rows and application logs omit URLs, addresses,
  contact values, exception messages, SQL parameters, and tracebacks. The existing
  retry table retains its required source URL but now stores only exception class.
- **Required credentials use `SecretStr`.** `DB_PASSWORD`, `PROXY_USER`, and
  `PROXY_PASSWORD` have no code defaults and fail settings validation when absent.
  SQLAlchemy URLs are built with `URL.create` so special characters are encoded.
- **Tracked credential copies are removed.** The region-ID utility now loads required
  proxy credentials from the environment and URL-encodes them. Two exploratory
  notebook cells retain placeholders only; no current secret value remains tracked.
- **Local migration history is repaired, not stamped.** The live DB referenced the
  previously untracked `f2a9c8d14e73` grade revision. That exact revision was
  recovered from local Git history and restored before E0 was applied.
- **Clean bootstraps are supported.** A historical migration's drift-only table/index
  drops are idempotent, allowing the full Alembic chain to apply to `aevorex_test`.
- **Legacy typing is staged.** Mypy runs over the package while existing modules are
  temporarily non-blocking; `aevorex.run_tracking` and future `freshness` and
  `publisher` packages are strict from their first line.
- **Serving v2 follows the binding engine contract.** After `AGENTS.md` was restored,
  `0001_serving_v2.sql` was reconciled to its section 6.1 table, column, and index
  names. The provisional deployed serving tables were confirmed empty, so a
  forward migration rebuilds only `serving.*` while preserving web-owned `app.*`.
- **RLS is market-membership based.** Authenticated reads require an enabled
  `app.org_markets` row joined to the caller's `app.org_members` row. Listing-agent
  contact additionally requires Pro, Growth, or Brokerage. `email_queue` has no
  authenticated policy and is service-role only.
- **Explicit grants accompany RLS.** Anon receives only demo snapshots and the
  section 6.3 public market columns, with active-market row filtering.
  Authenticated roles receive org-market-scoped serving reads; engine publishing
  uses the server-side service role.
- **No email delivery in E0.** `app.email_queue` is schema only. No worker, provider,
  SMTP call, or send path is introduced.
- **Remote deployment remains founder-controlled.** The founder connected the
  Supabase project and applied E0 manually. A read-only `migration list --linked`
  check on 2026-09-30 confirmed matching local and remote history for migrations
  `0000`, `0001`, and `0002`.
- **Applied migrations are repaired forward.** Because remote `0001` and `0002`
  were already recorded, changing those files alone would not update the project.
  The CLI-generated reconciliation migration recreates the verified-empty serving
  schema under the restored contract; it does not drop or replace `app.*`. The
  migration was applied and verified by remote schema/RLS readback.
- **Unowned security warnings are recorded, not silently changed.** The linked
  project's advisor reports the pre-existing `public.rls_auto_enable()` security-
  definer RPC as executable by anon/authenticated. It is outside E0 provenance and
  is documented for a separately authorized remediation.

## 2026-09-30 — E1 breakdown export

- **Discovery remains authoritative.** The checked-out `main` branch contained stale
  v2 scorer files while discovery and existing rows identified v3 as frozen. The exact
  audited v3 sources were recovered from the repository's existing stash before E1 was
  layered on; valuation and other packages remained out of scope.
- **Missing is not zero.** Every component is visible. Missing inputs export
  `subscore: 0` plus `available: false`, allowing the web to show the row while
  `recompose` preserves the scorer's available-weight renormalization.
- **Component-local anatomy is informational.** Bonuses or input haircuts already
  baked into a component carry types such as `component_bonus` or `input_haircut`;
  recomposition does not apply them twice.
- **Overlapping gates have one arithmetic cap.** Every candidate gate is exported,
  applied or not, and the scorer's selected minimum is exported once as the effective
  `cap` adjustment. This preserves both transparency and exact arithmetic.
- **Population metadata is second-pass data.** Score-time persistence writes the
  anatomy; percentile assignment then adds grade, percentile, tier and the selected
  city/state pool with its non-null lens population.
- **Frozen-score proof is independent.** A deterministic 64-property fixture market
  serializes all 320 v3 scores to a pinned byte digest, and its output was also compared
  byte-for-byte against the untouched pre-E1 scorer sources. Randomized recomposition
  covers 500 rows per lens.

## 2026-09-30 — E2 two-tier freshness

- **A completed check is an atomic search snapshot.** The Redfin GIS search payload is
  fetched through the existing rotating proxy until a successful short terminal page.
  A failed, malformed, repeated, or over-limit page rejects the entire snapshot; only a
  complete snapshot advances `last_checked_at`.
- **Search scope preserves the existing scraper filters.** Search requests use the
  checked-in city region IDs and Redfin's own current parameters for minimum price,
  120-day maximum time on market, supported property groups, and age-restricted / land-
  lease exclusions. Vero Beach is restored as region `18840`.
- **Absence state starts with a successful E2 observation.** Historical canonical rows
  are used for field diffs, but are not declared absent before E2 has actually observed
  them in that market. Two consecutive complete-snapshot absences confirm delisting.
- **Queue identity is market plus Redfin ID.** Repeated checks update one durable queue
  row, so unchanged inventory is idempotent. URLs remain operational queue data and are
  excluded from logs, events, and run telemetry.
- **Detail refresh reuses the existing path.** `PipelineRunner` remains the only
  fetch/parse/normalize/upsert implementation. Successful writes add `refreshed_at`;
  E2 invokes it at concurrency three and records aggregate 405/block counters only.
- **Backoff is durable and source-local.** Failed Redfin detail rows move through
  5, 10, 20, 40, 80, then 120-minute delays, capped at two hours.
- **Tier history is additive.** A separate `analysis_tiers` table records prior
  percentile/tier state. It does not feed any scorer. Tier moves use percentile 95/80,
  and `score_move` records absolute percentile movement of at least 10.
- **Incremental analysis does not rebuild baselines.** `analyze --changed` values and
  scores `needs_analysis` rows, then records tier transitions. The same API supports an
  explicit nightly market-stat rebuild for the future scheduler milestone.

## 2026-10-03 â€” E3 publisher

- **Search membership is authoritative.** A property enters a market cache only when
  `listing_presence` links its market/Redfin ID to a canonical local property.
- **Freshness commits last.** Identity, properties, validated scores, children,
  events, snapshots, and daily rollup commit before market freshness and the heartbeat.
- **Previous scores move only on change.** Idempotent republishes preserve `prev_*`;
  a changed value shifts the formerly published score/percentile into those columns.
- **Child collections are exact replacements.** Images are deterministically capped
  at 12; dependent facts are rebuilt so removed local data cannot linger remotely.
- **Public snapshots are allowlisted.** Each lens contains three addressed full rows
  with two component labels and five anonymous tier/price-band/DOM stubs.
- **The size guard uses binary MiB.** Warn at 350, stop new cities at 420, page at 450.
- **Wake is readiness-only.** It writes a heartbeat and optional Healthchecks ping;
  it schedules no work and never invokes the E5 email queue.
