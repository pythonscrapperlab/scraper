# Progress

## 2026-09-30 — E0 housekeeping and schema

Status: implemented and verified locally and on the linked Supabase project.

- Created branch `chore/e0-housekeeping-schema`.
- Recovered and re-read the 2026-09-26 discovery baseline and its missing grade
  migration from local Git history; later reconciled E0 against the restored
  binding `AGENTS.md`.
- Removed credential defaults; added secret-safe typed Supabase settings and a
  complete `.env.example`.
- Removed two additional tracked proxy-credential copies from the region-ID utility
  and notebook; a current-value scan now covers tracked and untracked text files.
- Added the local JSONB run ledger and five nullable breakdown columns; applied the
  Alembic head to the existing local database.
- Routed all eight Click commands through lifecycle tracking. Logs and run rows now
  exclude exception text, tracebacks, listing URLs, and contact data.
- Added `pyproject.toml`, `requirements.in`, a reproducible pip-compiled
  `requirements.txt`, Ruff, staged Mypy strictness, pytest settings, and an isolated
  `aevorex_test` fixture.
- Added app stubs, serving-v2 tables/indexes, explicit grants, and RLS. All SQL and
  policies compile on a clean PostgreSQL test database.
- Added credential rotation, migration, verification, and PII-safe operations to
  `docs/runbook.md`; recorded all provisional choices in `docs/decisions.md`.
- The founder connected the Supabase project and applied the migrations manually.
  Read-only CLI verification on 2026-09-30 showed matching local and remote
  migration history for `0000`, `0001`, and `0002`.
- The restored contract exposed differences in the provisional serving schema.
  Remote serving tables were verified empty; clean installs now match sections
  6.1/6.3. Forward migration `20260929202642` was applied to the linked project;
  readback confirms schema version 2, 17 serving tables, RLS on every table, 16
  serving policies, and exactly 12 anon-visible market columns.
- Supabase's security advisor reports a pre-existing `public.rls_auto_enable()`
  `SECURITY DEFINER` function executable by anon/authenticated. E0 did not create or
  alter it; remediation remains a separately scoped security follow-up.
- Final verification: `ruff check .` passed; Mypy reported no issues across 56
  source files (strict for E0/new-package modules); Pytest passed 268/268 tests,
  including clean Alembic + Supabase migration replay and RLS compilation.

## 2026-09-30 — E1 breakdown export

Status: implemented; final repository-wide verification recorded in the handoff.

- Created branch `feat/e1-breakdown`.
- Reconciled the stale v2 files on `main` with the binding v3 discovery baseline by
  recovering the exact audited scorer sources already present in the repository stash.
- Added scorer-native breakdowns for all five lenses with components, driver fields,
  candidate gates, effective caps, multipliers, bonuses and confidence shrink.
- Added pure `recompose()` and deterministic tests covering 500 randomized rows per
  lens plus a byte-identity fixture-market digest against the pre-E1 v3 scorers.
- Persisted breakdowns during score and enriched them during percentile assignment
  with grade, tier and city/state pool metadata.
- Added one web-facing specification per lens under `docs/breakdown/`.
- Ran local `score --all`: 10,273 scored/ranked, 0 errors, 108 unvalued. Every lens
  has 10,273 v3 breakdowns; all 46,791 non-null scores recomposed with zero failures.

## 2026-09-30 — E2 two-tier freshness

Status: implemented and verified locally; no Supabase writes and no email delivery.

- Created branch `feat/e2-freshness` and added the strict-typed `aevorex.freshness`
  package without changing scorer calculations, weights, gates, or inputs.
- Added local market freshness, listing presence, pending refresh, change event, and
  prior-tier state plus additive `properties.refreshed_at` / `delisted_at` columns.
- Implemented complete Redfin search-only GIS pagination through the existing proxy,
  preserving the existing search filters and rejecting partial/repeated page sets.
- Implemented price/status/relist/new diffs, two-successive-absence delisting, durable
  queue upserts, and idempotent repeated snapshots.
- Reused the existing detail pipeline at concurrency three, with aggregate-only 405 /
  block rates and durable 5→10→20→40→80→120 minute retry scheduling.
- Added changed-row valuation/scoring orchestration and additive percentile-tier history
  for `tier_up`, `tier_down`, and |percentile delta| ≥ 10 `score_move` events.
- Added `check --city`, `refresh --city`, `refresh --pending`, and `analyze --changed`.
- Live complete-check timings: Orlando 2,539 listings / 8 pages in 25.28 s; Vero Beach
  556 listings / 2 pages in 17.96 s. Immediate repeats took 14.50 s and 5.19 s and
  produced exactly 0 new events and 0 queue writes in both markets (target < 180 s).
- Final verification: `ruff check .` passed; Mypy reported no issues across 65 source
  files; Pytest passed 294/294 tests, including clean migration replay, frozen-score
  regression coverage, database-backed idempotency, and the two-absence rule.

## 2026-10-03 â€” E3 publisher

Status: implemented; live push and final verification are recorded in the handoff.

- Created `feat/e3-publisher` from the merged E2 baseline.
- Added strict-typed serving-v2 publishing with bounded upserts, exact child
  replacement, score recomposition gating, previous-score preservation, timezone and
  ZIP derivation, size guard, heartbeat, and freshness-last completion.
- Added exact three-full/five-stub public demo snapshots and redaction coverage.
- Added `push`, `status`, `rebuild`, `prune`, `drift`, and `wake`; no alert,
  morning-brief, scheduler, or email-delivery path was added.
- Added focused tests for idempotency, prune boundaries, redaction, size thresholds,
  and partial status after recomposition rejection.
- Live idempotent pushes completed in 42.52 s for Orlando (1,196 properties, 5,841
  scores) and 26.20 s for Vero Beach (446 properties, 1,819 scores), with zero
  recomposition rejections. Remote database size after both markets was 88.09 MiB.
- Final verification: `ruff check .` passed; Mypy reported no issues across 71 source
  files; Pytest passed 305/305 tests, including 11 focused publisher tests.

## 2026-10-03 — E4 scheduler on Windows

Status: implemented, installed and running; the 48-hour soak is **in progress** (see below).

- Branch `feat/e4-scheduler`. New strict-typed `aevorex.scheduler` package replaces the legacy
  state-wide scheduler: market-local slots with hash stagger and DST handling, `config/scheduler.yaml`
  with per-market overrides, active-city discovery (`app.org_markets` ∪ demo markets), advisory-lock
  lanes, shared Redfin backoff, late detector, queue drainer, `run-now`, dry-run `preview` with a
  nightly-capacity check, clock-drift check, PII-scrubbed JSON logs, Sentry and Healthchecks hooks.
- Windows: `scripts/install-services.ps1`, `uninstall-services.ps1`, `service.ps1`, `power.ps1`,
  `backup-local.ps1`; service `aevoraex-scheduler` (NSSM, delayed auto-start, restart on exit) and
  task `aevoraex-backup` (04:30, SYSTEM) installed. First backup: 198 MiB, verified, 69 s.
- Additive publisher helpers: `push_freshness`, `mark_status`, `heartbeat` (14-day prune).
- Miami (11458) and Tampa (18142) enabled; live checks: Miami 4,299 listings / 13 pages, Tampa 2,204 / 7.
- Problems found live and fixed (details in `docs/decisions.md`): DOM drift re-queued ~every listing
  daily (E2 amendment); `run_scrape` held a whole URL list in memory so a stop lost hours of fetching
  (refresh now in 150-URL batches); a check job stayed busy while waiting on the refresh lane
  (post-check refresh is its own job); the startup sweep failed the service's own ledger row;
  an after-restart queue sat idle (queue drainer).
- Early service restarts (first ~25 minutes of the soak) were deliberate fixes and show as
  `CancelledError` refresh rows in the report.
- Measured at concurrency 3: ~1.4 MB/page, 1.9 s/URL, ~0.53 URL/s. A full refresh of the five demo
  cities (~10,700 listings) is ~5.6 h, longer than the 02:00-07:00 budget; ~7,300 first-time fetches
  (Miami/Tampa/San Jose warming) drain over ~3.8 h. Proxy bandwidth is the open cost question.
- Soak `e4-48h` started 2026-10-03 14:29 local (09:29Z); `logs/soak/e4-48h.json` holds the
  publisher status *before* (Orlando and Vero Beach only, 1,642 properties, 86.8 MiB). Task
  `aevoraex-soak-report-e4` runs `main.py scheduler soak-report --label e4-48h` at 2026-10-05
  14:29 local and appends status before/after plus per-run metrics here. Not yet available.
- Final verification: `ruff check .` passed; Mypy clean (strict on `aevorex.scheduler`);
  Pytest all passing, including DST, stagger, late detector, backoff, lock, discovery, batching and
  publisher-helper tests.

## 2026-10-03 — E5 alerts, brief, RLS proof, views, soak

Status: implemented and verified; the seven-day soak is **in progress** (Day 0 recorded below).

- Branch `feat/e5-alerts-soak` from the merged E4 baseline. No scoring, valuation, weight, gate or
  normalizer file was touched; alerts only read percentiles and tiers that already exist.
- Alerts (`aevorex/alerts/`): pure crossing and quiet-hours rules; publisher step 6 compares the
  cache's pre-push (percentile, tier) with the new score, honours the org threshold (`min_percentile`
  or `tier`), channel and quiet hours, is idempotent, and commits with the scores. Morning brief:
  a 10-minute scheduler sweep queues one brief per member per org-market per org-local day
  (07:00-11:00 window; 5/10/15/25 properties by plan).
- RLS proof (`tests/rls`): anon sees only demo snapshots and the 12 public market columns; Starter
  cannot read agents; users cannot read other orgs' markets; only the service role writes; a user
  in both a Starter and a Pro org gets agents only where the Pro org subscribes. **It found a
  real defect**: signed-in users could not read `properties`, `scores` etc. because the E0 policies
  join `serving.markets` columns `authenticated` had no grant on. Fixed (`20261003115500`).
  Deployed policies re-proven live (rolled-back fixtures). The Auth admin-API variant is written
  but **not run**: no `SUPABASE_SERVICE_ROLE_KEY` is configured.
- Views: `serving.v_shortlist`, `v_property`, `v_agent`, `v_market_public` (security invoker).
  EXPLAIN ANALYZE on Orlando (1,196 listings): shortlist 1,363 ms -> 28 ms after rewriting the RLS
  policies as uncorrelated subqueries and adding `change_events(property_id, observed_at desc)`.
  Documented in `docs/schema.md`. All four E5 migrations are applied to the linked project.
- Live checks: an alert crossing enqueued exactly once and a repeat enqueued nothing on the real
  project (rolled back); the brief sweep ran against live data with no orgs enqueued.
- `docs/handover-web.md` written. The per-`DataApi`-method table is **empty** until the interface is
  pasted; the by-need map is complete.
- Soak collector `scheduler soak-daily` and task `aevoraex-soak-daily-e5-7d` (daily 23:55, registered
  without elevation, so it runs only while the user is logged on).
- Verification: see the E5 handoff report.

## E5 seven-day soak

Soak `e5-7d` started 2026-10-03 10:20Z. One row per day from `main.py scheduler soak-daily`; a day with no row means the collector did not run - nothing is back-filled.

| day (local) | window UTC | checks ok/failed | refreshes ok/failed | refresh p50 / max min | block rate | late events | alerts / briefs queued | local DB MiB | cloud DB MiB |
|---|---|---|---|---:|---:|---:|---|---:|---:|
| 2026-10-03 | 10-02 18:55Z → 10-03 18:55Z | 23/9 | 0/6 | 4.3 / 240.0 | n/a | 8 | 0 / 0 | 1314.3 | 189.77 |
