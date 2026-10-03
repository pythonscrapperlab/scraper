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

## 2026-10-03 — E4 scheduler on Windows

- **The legacy state-wide scheduler is gone.** `scheduler/jobs.py` scraped whole states on a
  six-hour interval and was disabled by default. It is replaced by per-city slot jobs; the
  `SCHEDULER_*_INTERVAL_HOURS` settings are removed. `main.py scheduler` is now a command
  group (`run`, `preview`, `run-now`, `clock-check`, `soak-start`, `soak-report`).
- **One slot function drives both the service and the preview.** `slots.slots_for_day` is the
  only place fire times are computed; `SlotTrigger` adapts it to APScheduler and
  `scheduler preview` calls it directly, so the preview cannot disagree with the service.
  A test proves the trigger sequence equals the plan.
- **Slots are market-local wall-clock times.** Daytime checks run 07:00-21:00 every 120 min
  (inclusive of 21:00), plus one 03:00 check; the nightly refresh is 02:00. DST: a wall time
  inside the spring-forward gap fires once, an hour later (02:00 -> 03:00); a fall-back
  ambiguous time fires once, at its first occurrence. Every day keeps exactly nine check slots.
- **Stagger is a stable hash, not jitter.** `sha256(slug + sorted zips) % 30` minutes is added
  to every slot of a market, so restarts and previews agree and cities spread across the hour.
  The zip list comes from `uscities.csv` (static), not from live inventory, so it cannot drift.
- **Concurrency is enforced by PostgreSQL advisory locks, not in-process semaphores.** The
  service and `run-now` are separate processes. Lanes: `check`, `refresh`, `analyze`
  (market-stats, valuation, scoring) and `publish`, plus a `service` single-instance lock. A
  check and a refresh may overlap (AGENTS.md section 9); two checks or two refreshes may not.
- **One chain, one implementation.** `JobRunner` is called by APScheduler, `run-now` and the
  tests. Stage order: check -> push freshness (light) -> if anything was queued: refresh ->
  analyze -> full push. A check that found nothing costs one row update, not a 40-70 s push.
- **A queue-wide refresh publishes every market it touched.** `refresh_pending` claims due
  rows for all markets, so after it the chain publishes each market that had rows waiting, not
  only the one whose check triggered it.
- **A queue drainer runs every 5 minutes.** Rows left by a restart, a cancelled run or a
  failed attempt are worked off without waiting for the next check or the nightly refresh.
  Rows stuck in `processing` are returned to `pending` only while holding the refresh lane,
  when nothing can legitimately be mid-flight.
- **Redfin backoff is one shared state.** Any incomplete check, failed refresh, refresh with
  zero successes, or block rate >= 50% counts as a source failure: 5 -> 10 -> 20 -> 40 -> 80
  minutes, capped at 120 (the same series as the E2 per-row backoff). A one-shot retry job is
  scheduled at the end of the delay. Manual `run-now` bypasses backoff and pause.
- **Supabase being down never stops scraping.** Failed pushes are remembered in `publish_debt`
  and retried on the 10-minute heartbeat. Remote status writes are best-effort.
- **Late means a missed slot, not a missed cadence.** A market is `late` when the latest
  scheduled check slot is older than 30 minutes and no completed check came at or after it.
  Slots before a market was discovered are ignored, so a new city is `warming`. `failed` is
  never downgraded to `late`. A late market triggers one catch-up check (unless backing off).
- **`next_check_at` is the real next slot.** The E2 check stamped `now + 120 min`, which is
  wrong across the 21:00-03:00 and 03:00-07:00 gaps; the scheduler overwrites it after each
  successful check.
- **Market-stats is once per night, after the refreshes.** The 06:30 America/New_York
  `finalize` job waits (up to 150 minutes) for in-flight nightly refreshes, then rebuilds
  baselines if the last run is older than 20 hours and runs a catch-up analyze. As in E2,
  rebuilt baselines affect only rows analysed afterwards; no full revaluation is triggered.
- **E2 amendment: days-on-market drift no longer queues a detail fetch.** DOM rises by one
  every day, so the first check of each day re-queued essentially every listing (2,243 of
  Orlando's 2,565) and doubled the nightly refresh. DOM now queues only if it appeared,
  vanished or decreased, or the listing date moved. Price, status, relist and new events are
  unchanged. Cost matters: the proxy is residential and metered per GB.
- **Active cities honour `app.org_markets.enabled` only.** The E0 `app.orgs` stub has a plan
  but no trial/active status, so AGENTS.md's "orgs trialing|active" filter cannot be applied.
  A market in `app.org_markets` with no Redfin region id is reported as unschedulable, never
  silently ignored. A Supabase read failure keeps the last known org markets.
- **Miami and Tampa are enabled in `scrapers/constants.py`** (region ids 11458 and 18142, from
  the previously commented entries) because two of the five demo cities could not run
  otherwise. First live checks: Miami 4,299 listings / 13 pages, Tampa 2,204 / 7.
- **Logging is JSON, scrubbed and class-only.** Daily rotation (30 days). URLs, e-mail
  addresses, phone numbers and street addresses are redacted from every message; exception
  text and tracebacks are never written, only the class. Sentry runs with PII off, no
  breadcrumbs or locals, exception messages blanked, and `before_send` scrubbing.
- **Every external integration is optional.** Sentry, the Healthchecks heartbeat/run/backup
  URLs and the Supabase org read are no-ops when unset; `SUPABASE_DIRECT_CONNECTION_URL`
  unset means demo markets only and nothing is published.
- **The service runs as LocalSystem.** It needs no user profile or interactive session, and
  starts delayed-automatic after PostgreSQL. NSSM restarts it 15 s after any exit; Windows
  recovery actions are a second safety net. Stop is Ctrl+C with a 60 s window; in-flight
  jobs are cancelled after 10 s and their ledger rows close as failed.
- **Backups are same-machine and stay so until an off-machine target exists.** The nightly
  task dumps to `D:\aevorex-backups` (not the DB's own directory tree), verifies with
  `pg_restore --list`, and keeps seven. This protects against a bad migration, not against
  losing the laptop; the runbook names that gap.
- **`scripts/free-disk.ps1` is not delivered here.** AGENTS.md lists it under section 9 but
  assigns the disk work to E0; it does not exist in the repository, and E4 was not asked to
  build it. Disk headroom (6.7 GiB free on D:) is tracked in the runbook.
- **E2 amendment: detail refresh works in batches of 150.** `PipelineRunner.run_scrape`
  fetches and parses its whole URL list in memory and writes only afterwards, so a 7,303-URL
  refresh made no durable progress for hours and lost everything (and the proxy bandwidth) on
  any restart or cancel - the "one crash lost all processed data" gotcha. `run_refresh` now
  feeds it `REFRESH_BATCH_SIZE` URLs at a time and settles the queue rows after each batch.
  Proven by tests: a cancel mid-run keeps finished batches and orphans only the rest.
- **The post-check refresh is its own job.** A refresh can wait hours for the single refresh
  lane. If it ran inside the check job, APScheduler would skip every later check slot of that
  market ("max instances reached") and the market would go `late` while the service was busy.
  In the service the check job ends after the light push and schedules `post-check:<market>`;
  `run-now` still runs the whole chain inline.
- **Measured detail-refresh throughput (live, 2026-10-03, concurrency 3):** about 1.4 MB per
  property page, 3.9 s to fetch, 0.25 s to parse, 0.13 s to write; a 150-URL batch took 281 s
  (1.9 s/URL, about 0.53 URL/s). Fetch time dominates; the CPU is about 20% busy. A full refresh
  of the five demo cities (about 10,700 listings) is therefore about 5.6 hours, longer than the
  02:00-07:00 budget; `scheduler preview` reports this once whole-city refresh runs exist.
  This is recorded, not hidden: the founder decides between more concurrency (AGENTS.md allows
  4, watch the 405/block rate), fewer cities, or refreshing only listings that changed.

## E5 - alerts, brief, RLS proof, views, soak

- **Alerts compare against the cache, not `prev_percentile`.** AGENTS.md 8.6 words the rule as
  "moved into the tier since `prev_percentile`". `prev_percentile` only moves when the percentile
  changes, so a crossing from days ago would still look like one on every later push. Instead the
  publisher reads the cache's (percentile, tier) per property and lens *before* the score upsert and
  alerts only when a score qualifies now and did not then. Unchanged pushes alert nothing; a
  `dedupe_key` unique index is a second line of defence.
- **Alerts commit with the scores.** If the alert write fails the score upsert rolls back too, so
  the crossing is seen again on the retry. A failed push is visible (publish debt) and a lost alert
  is not.
- **A market's first push is a silent baseline.** With no earlier cache state everything is a
  "crossing"; a rebuild would otherwise mail every qualifying listing. Counted as
  `alert_baseline_suppressed`.
- **Thresholds: percentile wins over tier.** `app.thresholds` keeps one row per org per lens;
  `min_percentile` (inclusive) overrides `tier` when both are set. The alert's recorded tier may be
  `rest` for a percentile rule, so the `app.alert_events.tier` check was widened.
- **Quiet hours only defer e-mail.** In-app alert rows are written immediately; the queue row gets
  `send_after` = the next quiet-end in the org's timezone. Default window 21:00-07:00 org-local,
  per user, disable-able. Nonexistent DST-gap times map forward; fall-back is unambiguous because
  only the end instant is computed.
- **Morning brief is a sweep, not 07:00 cron jobs per org.** Orgs have different timezones and the
  laptop sleeps. A 10-minute sweep queues any brief that is due (07:00-11:00 org-local) and not yet
  queued, keyed by (market, local date, user). It catches up after sleep, is DST-safe by
  construction, and skips (and counts) a brief that would land after 11:00 rather than send stale
  news. Brief size by plan: 5, 10, 15, 25 (config). One e-mail per member per org-market; there is
  no in-app brief because `app.alert_events` is per lens and tier.
- **`app.orgs` gained `tz` and `default_lens`.** The stub had neither, and "org-local" and "the org's
  default lens" are both in the spec. Defaults are New York and `motivated_seller`; the web should
  set `tz` at onboarding.
- **RLS proof found a real defect.** See `docs/schema.md`: `authenticated` could not evaluate the
  E0 policies because they join columns of `serving.markets` it had no grant on. Fixed with a
  column grant, not a SECURITY DEFINER function, so no policy runs with elevated rights.
- **RLS rewritten for speed, not meaning.** Correlated per-row `EXISTS` policies made a 1,196-row
  shortlist cost 1.4 s; uncorrelated `IN` subqueries bring it to 28 ms. The proof suite covers both.
  Plan gating for `agents` is per subscribed market: a user in a Starter org and a Pro org sees agents
  only in the markets the Pro org subscribes to.
- **Views are `security_invoker`.** A definer view would bypass RLS and make the Starter/Pro rule
  depend on the view's owner. `v_shortlist` is wide (five lenses per row) because the web asks for
  "property + five scores"; at this size the lateral pivot costs milliseconds.
- **Live RLS proof needs no admin key to run, and uses it when present.** The default suite
  switches roles inside rolled-back transactions on local Postgres. `AEVORAEX_LIVE_TESTS=1` repeats
  it against the project through the direct connection (also rolled back). The Auth admin-API variant
  (confirmed users, no e-mail, deleted afterwards) is implemented but **not yet run**: no
  `SUPABASE_SERVICE_ROLE_KEY` is configured in `.env`.
- **The soak is a collector, not a claim.** A seven-day soak cannot be completed in one working
  session. `scheduler soak-daily` appends one row per day to `docs/progress.md`, replaces a day's
  row if re-run, and never back-fills a day it did not observe. The first row's trailing 24 h
  includes the E4 service restarts of 2026-10-03.


## E6 - stabilise and slim (branch `fix/e6-stabilise-slim`, 2026-10-04)

Audit 2026-10-03 found correctness bugs and a publisher that sent far more than the web renders. Fixed in nine steps, one commit each.

### Step 1 - valuation v2 restore

- **The `backup/stash*` branches do not exist**; the work lives only in `stash@{0..2}`. Restored from `stash@{0}` (dated 2026-09-30) directly. `tests/valuation/test_v3_valuation.py` was in stash 0's untracked part (`stash@{0}^3`), not stash 2. All stashes are left in place.
- Restored `valuation/{engine,rehab,rent,comps}.py` (rehab, rent, comps locality filter, ARV exit cap, uncorroborated shrink) and `VALUATION_VERSION = "v2"`. Re-applied the E0 logging rule: class-only logging, no `exc_info=True`. No scorer was touched.
- Why it regressed: `main` still carried the v1 valuation files, so since E5 the service has written `v1` rows (1,397 at 2026-10-04, all one 2026-10-03 10:30 batch) next to 9,926 `v2` rows (all computed 2026-09-19 22:15-22:26). Those v1 rows were **not** re-valued: re-valuing changes their scores, which is an owner call. They are reported by the soak.
- Reproduction proof (`scripts/prove_valuation_v2.py`, read-only, rolls back, output in `docs/valuation-v2-repro.md`): of 200 stored-v2 rows, **158 reproduce market_value, arv, rehab_mid, rent within 1% with identical flags; 42 differ.**
  - 8 differ because the property row and comps changed after 2026-09-19.
  - 34 have no per-property input change. 19 are rent-only, driven by the market-wide zip x bedroom rent index (median of rental `price_history`, 24-month rolling window, +2,784 properties since). Replaying the index as of the stored run time removes 7 of them. The other 15 no-change rows are market-baseline drift (`market_stats` is recomputed nightly with no history, so it cannot be replayed) or code evolution.
  - **Limit of the proof:** stash 0 post-dates the stored run by 11 days of uncommitted work. Code drift between those two points cannot be ruled out and cannot be recovered. The honest statement is: the restored code reproduces 79% exactly on today's data and explains the rest by documented data drift, not proof of byte-identity.

### Follow-ups logged (out of scope for E6)

- Mid-term lens seasonal cap (product decision pending).
- The two MAO formulas (valuation `max_allowable_offer` vs the fix-flip scorer's).
- Dead config fields.
- Percentile pools that include delisted rows.
- Scheduler backoff / `publish_debt` persistence across restarts.
- Ingestion parser bugs.
- Stale README / CLAUDE.md.
- Backfill of the 1,397 `v1` valuation rows (changes their scores).
