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
