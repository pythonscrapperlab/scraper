# AevoraeX engine runbook

All commands below are Windows PowerShell commands run from the repository root.
Do not use WSL. Never paste secrets into tickets, logs, screenshots, or committed
files. No E0 command sends email.

## Bootstrap and verification

```powershell
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m alembic upgrade head
ruff check .
mypy
pytest
```

`pytest` rebuilds only the database named `aevorex_test`. Its session fixture
terminates connections to that exact database, drops it, recreates it, applies the
complete Alembic chain, installs minimal local Supabase auth stubs, and applies all
three Supabase SQL migrations. It never drops `aevorex_db`.

## Required configuration

Copy `.env.example` to `.env`, then replace the three required placeholders:

- `DB_PASSWORD`
- `PROXY_USER`
- `PROXY_PASSWORD`

Settings validation fails immediately if any is missing. Keep `.env` local. The
Supabase URL/key values are optional until a publisher/deployment target is in use.
E3 publishing requires `SUPABASE_DIRECT_CONNECTION_URL`; keep it server-side. The
optional `HEALTHCHECKS_PUBLISHER_URL` is treated as a secret and never logged.

## Credential rotation

### Local PostgreSQL password

1. Take a database backup and confirm it can be read.
2. In pgAdmin or an authenticated `psql` session, change only the `aevorex` role's
   password. Enter the new value interactively; do not put it in shell history.
3. Update `DB_PASSWORD` in the local `.env` and any authorized secret store.
4. Restart long-running scheduler/process instances so pooled connections close.
5. Run `python -m alembic current` and one read-only health query.
6. Revoke/remove the old credential from every secret store and record the rotation
   date without recording the value.

### Proxy credentials

1. Rotate the credential in the proxy provider dashboard.
2. Update `PROXY_USER` and `PROXY_PASSWORD` in `.env`/the authorized secret store.
3. Restart long-running processes.
4. Run a provider connectivity check that logs only status, latency, and exception
   class—never the proxy URL or credentials.
5. Revoke the previous credential. The founder owns this rotation.

### Supabase database/key material

1. Rotate from the Supabase dashboard and update the authorized secret store.
2. Update `.env` without committing it.
3. Re-link the CLI only after confirming the intended project reference.
4. Run `npx --yes supabase@2.118.0 db push --dry-run`, review the exact migrations,
   then run the same command without `--dry-run`.
5. Never expose a service-role/secret key to the web client.

## Migrations

Local engine schema:

```powershell
python -m alembic current
python -m alembic upgrade head
```

Supabase serving schema, after an intentional link is already present:

```powershell
npx --yes supabase@2.118.0 db push --dry-run
npx --yes supabase@2.118.0 db push
```

Do not use `db reset --linked`; it is destructive. E0 migrations are ordered as
`0000_app_stubs.sql`, `0001_serving_v2.sql`, and `0002_rls.sql`.

## CLI run ledger

Every Click command creates one `public.runs` row. A successful row contains only
aggregate counts. A failure contains `error_class` only. If `init-db` is bootstrapping
a completely empty database, its row can only be inserted after the migration has
created `runs`; a migration failure before that point cannot be recorded in a table
that does not yet exist.

Useful metadata-only check:

```sql
select kind, status, started_at, finished_at, counts, error_class, duration_s
from runs
order by started_at desc
limit 20;
```

Do not add URLs, addresses, names, email addresses, phone numbers, raw payloads,
exception messages, or SQL parameters to run scope/counts or application logs.

## Publisher

The laptop opens an outbound PostgreSQL connection and writes the rebuildable
`serving.*` cache. It exposes no listener and never sends email.

```powershell
python main.py publisher push --market orlando-fl
python main.py publisher status
python main.py publisher drift
python main.py publisher prune --market orlando-fl --dry-run
python main.py publisher rebuild --market orlando-fl
python main.py publisher wake
```

`push` includes only search-snapshot rows linked to canonical local properties,
retains delisted rows through the exact 30-day boundary, recomposes every score,
and commits freshness last. Demo snapshots contain exactly three full rows and five
redacted stubs per lens. A score mismatch is rejected and marks the run `partial`.

The size guard warns at 350 MiB, refuses a new market at 420 MiB, and sends the
configured failure heartbeat at 450 MiB. `prune` previews by default; use `--apply`
only after reviewing the candidate count.

## Scheduler service (E4)

The `aevoraex-scheduler` Windows service runs `venv\Scripts\python.exe main.py scheduler run`
from the repository root. It holds a single-instance lock, so a second copy refuses to start.
Nothing calls the laptop; it pushes outbound only. **It never sends email.**

### What it does

| Trigger (market-local) | Chain |
|---|---|
| Check slots 07:00-21:00 every 120 min, plus 03:00 (+ hash stagger, 0-29 min) | search check -> push freshness; if anything was queued: detail refresh -> analyze -> full push |
| Nightly refresh 02:00 (+ stagger) | whole-city detail refresh -> analyze -> full push |
| 06:30 America/New_York `finalize` | wait for nightly refreshes, `market-stats` (if >20 h old), catch-up analyze, push if anything changed |
| Every 5 min | discovery (new city -> `warming` check at once), late detector, queue drainer |
| Every 10 min | heartbeat (Healthchecks + `serving.heartbeats`), retry failed pushes |
| Every 10 min | morning-brief sweep (queues briefs due 07:00-11:00 org-local) |
| Every 60 min | clock-drift check |

Active cities = enabled rows in `app.org_markets` plus `config/demo_markets.yaml`. Cadence,
windows, stagger and per-market overrides live in `config/scheduler.yaml` (restart to apply).
A city in `app.org_markets` with no Redfin region id in `scrapers/constants.py` cannot be
scheduled; the service logs it at start-up and `scheduler preview` lists it.

Limits: one check and one refresh at a time (they may overlap each other), one analyze, one
publish. Redfin failures (incomplete search, failed or blocked refreshes) back off
5 -> 10 -> 20 -> 40 -> 80 -> 120 min; a retry runs when the delay ends. A market that misses a
check slot by 30 min is flagged `late` locally and on Supabase and gets one catch-up check.

### First-time install

Prerequisites: working `venv`, `.env` (see `.env.example`), local PostgreSQL service,
`winget install NSSM.NSSM`. All commands are PowerShell, never WSL.

```powershell
python main.py scheduler preview --hours 48          # read the schedule and the capacity check
.\scripts\install-services.ps1 -DryRun               # prints every change, makes none
.\scripts\install-services.ps1 -Elevate -Start -ApplyPower   # one UAC prompt
.\scripts\service.ps1 status
```

Install is idempotent: re-running updates the service in place. A transcript is written to
`logs\install-services.log`. To remove it: `.\scripts\uninstall-services.ps1 -Elevate`
(logs, backups, `.env` and the database are never touched).

### Day-to-day

```powershell
.\scripts\service.ps1 status                          # no elevation needed
.\scripts\service.ps1 restart -Elevate                # after a code or config change
python main.py scheduler preview --hours 48           # dry run; add --offline to skip Supabase
python main.py scheduler run-now --market orlando-fl --job check
python main.py scheduler run-now --market orlando-fl --job nightly
python main.py scheduler run-now --job market-stats
python main.py scheduler clock-check
python main.py publisher status
```

`run-now` uses the same locks as the service and fails fast ("lock_busy") when a lane is
occupied; add `--wait` to queue behind it. It bypasses backoff and pause. Pause everything
without uninstalling: set `SCHEDULER_PAUSED=true` in `.env` and restart (the service keeps
heartbeating and discovering, and runs nothing).

Logs: `logs\scheduler.jsonl` (JSON, daily rotation, 30 days, no PII; exception text is never
written, only its class), `logs\service-stdout.log` / `service-stderr.log` (NSSM, size-rotated;
only crashes before logging starts land here), `logs\backup.jsonl`.

Ledger (metadata only, no listing data):

```sql
select kind, scope->>'city' as city, scope->>'trigger' as trigger, status,
       round(duration_s::numeric) as seconds, counts, error_class, started_at
from runs where started_at > now() at time zone 'utc' - interval '6 hours'
order by started_at desc;
```

Kinds: `check`, `refresh`, `analyze`, `market_stats`, `publish`, `scheduler` (one row per service
run). Waiting detail rows: `select market_slug, status, reason, count(*) from pending_refresh group by 1,2,3;`

### Healthchecks and Sentry

Everything is optional and silent when unset. In Healthchecks.io create:

| Check | `.env` key | Period / grace | Meaning |
|---|---|---|---|
| scheduler heartbeat | `HEALTHCHECKS_SCHEDULER_URL` | 10 min / 15 min | the service is alive (sends `/fail` if the clock drifts >= 30 s) |
| stage runs | `HEALTHCHECKS_RUNS_URL` | 1 day / 2 h | every stage pings `/start`, success or `/fail` with a run id and numeric counts; alert on `/fail` |
| backup | `HEALTHCHECKS_BACKUP_URL` | 1 day / 2 h | nightly dump verified |
| publisher | `HEALTHCHECKS_PUBLISHER_URL` | (existing) | push finished |

Ping URLs are secrets: keep them in `.env` only. Sentry: set `SENTRY_DSN` (and optionally
`SENTRY_ENVIRONMENT`). Events carry the job name, market slug and exception *class* only;
messages, locals, breadcrumbs, request and user data are stripped before sending.

### Clock drift

Slots are wall-clock times, so a wrong clock shifts every run. The service compares against
NTP (`time.windows.com`, then `pool.ntp.org`, then an HTTPS `Date` header) at start and hourly:
warn >= 5 s, fail >= 30 s (error log, Sentry warning, failing heartbeat). Fix:

```powershell
w32tm /resync            # elevated; or Settings > Time & language > Sync now
python main.py scheduler clock-check
```

### Power

```powershell
.\scripts\power.ps1                  # report only
.\scripts\power.ps1 -Apply -Elevate  # AC: never sleep/hibernate, lid does nothing, no USB/PCIe/Wi-Fi power saving
```

Add `-DisableHibernate` (frees `hiberfil.sys` on C:) and `-ActiveHours` (Windows Update avoids
06:00-00:00) if wanted. On this machine the lid-close setting is hidden by the OEM image; the
script tries to unhide it when elevated. Keep the laptop plugged in and ventilated. A reboot
costs freshness, never data: the service starts itself (delayed) after PostgreSQL and returns
orphaned detail rows to the queue.

### Backups, recovery, and the laptop-wipe case

`scripts\backup-local.ps1` runs nightly at 04:30 as SYSTEM (task `aevoraex-backup`). It writes
`D:\aevorex-backups\aevorex_db_YYYYMMDD_HHMMSS.dump` (custom format), verifies it with
`pg_restore --list`, keeps the newest seven (`BACKUP_KEEP`), refuses to start without twice
the last dump plus 1 GiB free, and logs `logs\backup.jsonl`. Manual run / dry run:

```powershell
.\scripts\backup-local.ps1 -DryRun
.\scripts\backup-local.ps1
```

A scheduled task running as SYSTEM is invisible to a non-elevated `Get-ScheduledTask`; check it
elevated, or look at `logs\backup.jsonl` and the dump timestamps.

**These dumps live on the same machine.** They cover a bad migration or a dropped table, not a
dead or stolen laptop. Supabase is *not* a backup: it holds only the capped serving subset and
no POIs, raw payloads or history older than 30 days. Until an off-machine copy exists, copy the
newest dump to cloud storage or an external drive weekly (set `BACKUP_DIR` to that drive to
write there directly).

Restore into the existing database (stop the service first):

```powershell
.\scripts\service.ps1 stop -Elevate
$env:PGPASSWORD = '<db password>'   # type it; do not save it in a script
& 'C:\Program Files\PostgreSQL\17\bin\pg_restore.exe' --clean --if-exists --no-owner -h localhost -U aevorex -d aevorex_db D:\aevorex-backups\<file>.dump
Remove-Item Env:\PGPASSWORD
python -m alembic current
```

Laptop-wipe recovery: install Windows, PostgreSQL 17 and Python 3.12; clone the repository; create
the `venv` and install `requirements.txt`; restore `.env` from your password store (rotate the DB,
proxy and Supabase credentials if the old disk may be exposed); create the `aevorex` role and
`aevorex_db`, then `pg_restore` the newest dump; `python -m alembic upgrade head`;
`python main.py publisher drift`; `python main.py publisher rebuild --all`;
`winget install NSSM.NSSM`; `.\scripts\install-services.ps1 -Elevate -Start -ApplyPower`.
Without any dump the local database cannot be rebuilt (only re-scraped), which is why the
off-machine copy matters.

### Disk

`D:` was 87% full with 6.7 GiB free at E4. Each dump is about 200 MiB, so seven dumps cost about
1.4 GiB. Check weekly: `Get-PSDrive D,C | Select Name,Free`. The raw-archive/POI-prune script
`scripts\free-disk.ps1` from AGENTS.md section 9 is not part of E4 (see `docs/decisions.md`).

### Failure playbook

| Symptom | Likely cause | Action |
|---|---|---|
| Market shows `late` on the web | missed slot (asleep, offline, service down) | `service.ps1 status`; the catch-up check runs by itself once the service is up |
| Market shows `failed` | last check incomplete (block/405/network) | wait for the backoff retry (logged as "Redfin backoff"); if it repeats, check the proxy dashboard and rotate credentials |
| `Another scheduler instance holds the service lock` | two services, or a stuck old process | `service.ps1 status`; stop the duplicate; the lock frees when its process exits |
| `lock_busy` from `run-now` | a stage of that kind is running | wait, or use `--wait` |
| Pushes failing, scraping fine | Supabase unreachable or size guard | heartbeat retries every 10 min; `python main.py publisher status` |
| `nightly_overrun` in the log / Sentry | the active set cannot finish 02:00-07:00 | read `scheduler preview` capacity, add cities more slowly, or lengthen the window |
| Clock drift `fail` | Windows time service stopped | `w32tm /resync`, then `scheduler clock-check` |
| Many `ServiceInterrupted` runs | service killed (reboot, crash) | normal after a restart; the swept rows are history, not data loss |

### 48-hour soak procedure

```powershell
python main.py scheduler soak-start --label e4-48h      # saves 'before' publisher status
# ...leave the service running for 48 hours...
python main.py scheduler soak-report --label e4-48h     # appends status before/after and per-run metrics to docs/progress.md
```

`soak-report --print-only` previews the Markdown without touching `docs/progress.md`.

## Alerts, morning brief, RLS proof and soak (E5)

Alerts are evaluated inside `publisher push` (same transaction as the scores) and the brief by the
scheduler's 10-minute sweep. Both only **queue** rows in `app.alert_events` / `app.email_queue`;
the web's sender delivers them and honours `send_after`. Nothing in this repository sends mail.

Check what was queued (counts only, no addresses):

```powershell
python main.py publisher status
python main.py scheduler soak-daily --print-only        # today's metrics, nothing written
```

Brief sizes by plan live in `config/scheduler.yaml` (`service.brief_counts`); restart to apply.
If a brief did not arrive: check the org's `tz`, that a member has `morning_brief` and `email` on
(or no settings row), that the market has scores, and that the sweep ran between 07:00 and 11:00
org-local (a later brief is skipped on purpose).

### Applying the E5 migrations

```powershell
npx --yes supabase@2.118.0 db push --dry-run
npx --yes supabase@2.118.0 db push
```

### RLS proof

```powershell
python -m pytest tests/rls -q                                   # local, always on
$env:AEVORAEX_LIVE_TESTS = '1'; python -m pytest tests/rls/test_live_supabase.py -q   # live, rolled back
```

The live admin-API variant additionally needs `SUPABASE_SERVICE_ROLE_KEY` in `.env` (tests only;
it creates two confirmed throwaway users, sends no e-mail, and deletes them). Without the key it is
skipped and says so.

### View performance

```powershell
$env:PYTHONPATH = '.'; python scripts/explain_views.py orlando-fl          # timings only
python scripts/explain_views.py orlando-fl --plans                           # full plans
```

### Seven-day soak

```powershell
powershell -File scripts\install-soak-task.ps1 -Label e5-7d       # daily 23:55; elevated = SYSTEM
python main.py scheduler soak-daily --label e5-7d                   # run one row by hand
```

One row per day is upserted into the "E5 seven-day soak" table in `docs/progress.md`. A day with no
row means the collector did not run; it is never back-filled. Without elevation the task runs only
while the user is logged on.


## Serving size: measure, rebuild, reclaim (E6)

### Measure

```powershell
$env:PYTHONPATH = '.'; python scripts/serving_sizes.py <label>   # writes logs/e6/<label>.json
```

It prints `pg_database_size`, KB per published property, and per serving table: total / heap / TOAST / index KiB and `n_live_tup` / `n_dead_tup` (from `pg_stat_user_tables`). Raw SQL, run in the Supabase SQL editor:

```sql
select pg_size_pretty(pg_database_size(current_database()));
select c.relname, pg_size_pretty(pg_total_relation_size(c.oid)) total,
       s.n_live_tup live, s.n_dead_tup dead
from pg_class c join pg_namespace n on n.oid = c.relnamespace
left join pg_stat_user_tables s on s.relid = c.oid
where n.nspname = 'serving' and c.relkind = 'r' order by pg_total_relation_size(c.oid) desc;
```

Estimate before pushing: `python scripts/measure_payload.py orlando-fl` (payload JSON per property, no network).

### Reclaim after a slimming release

Deleting rows frees space for reuse inside Postgres but does not shrink files; `VACUUM FULL` rewrites a table and does. **It takes an ACCESS EXCLUSIVE lock on the table for the duration**: the web's reads of that table wait (seconds for the small tables, tens of seconds for `scores`, `history`, `images`). Run it when nobody is watching, one table at a time, and never inside a transaction:

1. Stop the scheduler (`scripts/service.ps1 stop -Elevate`) so no push is mid-flight.
2. Apply pending Supabase migrations (`supabase db push --include-all`, or `python scripts/apply_supabase_migration.py <file>` where the CLI is missing).
3. `python main.py publisher rebuild --all` (deletes each market's tree and re-sends it from local truth; alert history survives because `app.alert_events.property_id` is `ON DELETE SET NULL`).
4. `python scripts/vacuum_serving.py` (VACUUM FULL ANALYZE each `serving.*` table, smallest first, prints the time each took).
5. `python scripts/serving_sizes.py after` and compare with `before`.
6. Start the scheduler again.

Steady state needs none of this: autovacuum reclaims dead tuples, and the publisher no longer rewrites unchanged rows.
