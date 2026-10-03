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
