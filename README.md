# Aevorex Scraper

Real estate intelligence scraper for Aevorex SaaS platform. Scrapes property listings from Zillow, Redfin, and Realtor.com, normalizes and deduplicates data into PostgreSQL.

## Architecture

6-layer architecture with clear separation of concerns:

1. **CLI** (`main.py`) — Click subcommands
2. **Transport** (`transport/`) — HTTP, Playwright, Selenium, proxy management
3. **Scrapers** (`scrapers/`) — Platform-specific extraction
4. **Normalizers** (`normalizers/`) — Map to unified schema
5. **Pipeline** (`pipeline/runner.py`) — Orchestration & deduplication
6. **Database** (`db/`) — Models, session, migrations

## Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Docker (optional, for Postgres)

### Installation

1. Clone repository:
```bash
cd d:\aevorex\scraper
```

2. Create virtual environment:
```bash
python -m venv venv
venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and configure:
```bash
cp .env.example .env
```

5. Set up PostgreSQL (using Docker):
```bash
docker run --name aevorex_db -e POSTGRES_DB=aevorex \
  -e POSTGRES_USER=aevorex -e POSTGRES_PASSWORD=aevorex_password \
  -p 5432:5432 -d postgres:16
```

6. Initialize database migrations:
```bash
alembic upgrade head
```

## Usage

### Single Scrape

Scrape Zillow for Florida:
```bash
python main.py scrape --source zillow --state FL
```

Scrape multiple states:
```bash
python main.py scrape --source zillow --state FL --state CA
```

Scrape all sources:
```bash
python main.py scrape --source all --state FL
```

### Retry Failed URLs

```bash
python main.py retry --source zillow --state FL
```

### Scheduler

The scheduler is a Windows service (`aevoraex-scheduler`, installed with NSSM). It drives
per-city freshness checks, nightly refreshes, analysis and Supabase publishing. Operator
commands (see `docs/runbook.md`):
```bash
python main.py scheduler preview --hours 48     # dry run: what would fire, and does it fit?
python main.py scheduler run-now --market orlando-fl --job check
python main.py scheduler clock-check
python main.py scheduler run                    # the service entry point (NSSM runs this)
```

## Project Structure

```
aevorex/
├── db/
│   ├── __init__.py
│   ├── models.py          # SQLAlchemy ORM models (8 tables)
│   ├── session.py         # Async engine & session factory
│   └── deduplicator.py    # Dedup logic
├── transport/
│   ├── http_client.py     # httpx wrapper with retries
│   ├── browser_playwright.py  # Async Playwright
│   ├── browser_selenium.py    # Sync Selenium fallback
│   └── proxy_manager.py   # Proxy rotation (disabled)
├── scrapers/
│   ├── base.py            # Abstract BaseScraper
│   ├── zillow.py          # Zillow __NEXT_DATA__ extraction
│   ├── redfin.py          # Redfin API scraper
│   └── realtor.py         # Realtor JSON-LD scraper
├── normalizers/
│   ├── base.py            # Abstract BaseNormalizer
│   ├── zillow.py          # Zillow → schema
│   ├── redfin.py          # Redfin → schema
│   └── realtor.py         # Realtor → schema
├── pipeline/
│   └── runner.py          # Orchestration + dedup
├── scheduler/
│   ├── service.py         # APScheduler service (aevoraex-scheduler)
│   ├── jobs.py            # check -> refresh -> analyze -> publish chain
│   └── slots.py           # market-local slots shared by service and preview
├── config.py              # Settings from .env
├── main.py                # Click CLI entry point
└── __init__.py
```

## Database Schema

### Core Tables

- **raw_scrapes** — Raw JSON blob per scrape (never lose source)
- **properties** — Single deduplicated table (one row per property)
- **price_history** — Price changes per source
- **tax_history** — Tax assessments per source
- **property_images** — Images per source
- **open_houses** — Open house events per source
- **leads** — Generated investment leads
- **scrape_errors** — Failed URLs with full traceback

Deduplication key: `(normalized_address + zip_code)` or platform IDs
Primary source priority: Realtor > Redfin > Zillow

## Development

### Create New Scraper

1. Implement `scrapers/[platform].py` extending `BaseScraper`
2. Implement `normalizers/[platform].py` extending `BaseNormalizer`
3. Register in `main.py` CLI

### Run Tests

```bash
pytest
```

### Code Quality

Format:
```bash
black aevorex/ main.py
```

Lint:
```bash
ruff check aevorex/ main.py
```

Type check:
```bash
mypy aevorex/ main.py
```

### Migrations

Generate migration:
```bash
alembic revision --autogenerate -m "descriptive message"
```

Apply migrations:
```bash
alembic upgrade head
```

## Configuration

Key settings in `.env`:

- `DB_*` — Database connection
- `SCRAPER_*` — General scraper settings
- `PLAYWRIGHT_*` — Playwright browser options
- `SELENIUM_*` — Selenium options
- `PROXY_*` — Proxy settings (disabled by default)
- `ZILLOW_ENABLED`, `REDFIN_ENABLED`, `REALTOR_ENABLED` — Enable/disable scrapers
- `TARGET_STATES` — FL, CA (comma-separated)
- `SCHEDULER_PAUSED`, `SCHEDULER_CONFIG_PATH` — Scheduler kill switch and `config/scheduler.yaml` (cadence, windows, stagger, per-market overrides)
- `SENTRY_DSN`, `HEALTHCHECKS_*`, `BACKUP_DIR`, `BACKUP_KEEP` — optional observability and backups

## Conventions

- All database operations use **SQLAlchemy async** + `asyncpg`
- UUIDs for all primary keys
- UTC timestamps only
- State abbreviations only: FL, CA
- Platform-specific fields in `meta` JSONB column
- Scraper runs are sequential (no concurrency) to avoid detection
- Every failed URL saved to `scrape_errors` table
- Proxies disabled by default (`PROXY_ENABLED=false`)

## Next Steps

1. Implement Zillow scraper (extract __NEXT_DATA__)
2. Implement Redfin scraper (GIS API)
3. Implement Realtor scraper (JSON-LD)
4. Add lead scoring logic
5. Add frontend (Next.js + Mapbox)
6. Deploy to Railway/Render

## License

Proprietary — Aevorex
