# Aevorex — Project Context for Claude Code

## What this is
AI-powered real estate intelligence SaaS. Legally sourced property data 
scored for fix-and-flip, Airbnb/STR, buy-and-hold, and motivated-seller 
investment strategies. Target markets: Florida and California.

Core features:
1. Lead gen with analytics from Zillow/Redfin/Realtor scraping, scored 
   across the 4 strategies above
2. Link-based property analytics (paste a URL, get a deal score, ROI, comps)

Future direction (don't scope-limit around this — it's coming): AI-generated 
marketing videos, 3D layout generation from 2D floor plans.

## Stack
- **Backend:** FastAPI, Celery, Redis, Playwright, Scrapy, Pandas, scikit-learn
- **Frontend:** Next.js, shadcn/ui, Tailwind, Recharts, Mapbox, Zustand, Clerk
- **Data:** PostgreSQL via Supabase/Neon, async SQLAlchemy 2.x, Alembic migrations
- **Hosting:** Railway/Render
- **Scraping:** httpx, Playwright, Selenium, Scrapy, Celery/Redis, Webshare 
  residential rotating proxies via a unified ProxyManager

## Database
Single deduplicated `properties` table across sources (dedup by address+zip 
or platform IDs, per-platform ID columns, `primary_source` priority is 
Realtor > Redfin > Zillow), plus satellite tables: `raw_scrapes`, 
`price_history`, `tax_history`, `property_images`, `open_houses`, `schools`, 
`property_comps`, `points_of_interest`, `transport_stops`, `location_scores`, 
`property_features`, `market_snapshots`, `leads`, `scrape_errors`.

Dedup match order: platform ID first, then normalized APN scoped by state, 
then normalized address+zip as fallback.

## Conventions already in use — follow these patterns, don't reinvent them
- Async SQLAlchemy 2.x sessions throughout; existence/merge checks use 
  `.scalars().first()`, not `.scalar_one_or_none()` (see gotchas below)
- Config-driven behavior: weights/thresholds/settings externalized to 
  config.py/config.yaml, not hardcoded, with per-market override support
- Pipelines commit incrementally (per-URL / per-batch), not once at the end
- Normalizers coerce empty-string/junk scraped values to `None` before 
  typed DB inserts, rather than crashing

## Known gotchas (hard-won, don't reintroduce these)
- A scraper's `platform`/`source` field returning a lowercased class name 
  (e.g. `redfinscraper` instead of `redfin`) silently broke source 
  attribution in production — prefer explicit `SOURCE` constants over 
  derived values
- `.scalar_one_or_none()` on dedup/upsert queries crashed in production 
  when duplicate rows existed — use `.scalars().first()` for existence 
  checks
- A single commit at the end of a long-running scrape meant one crash 
  lost all already-processed data — commit incrementally
- The error-logging fallback itself once crashed on overly long exception 
  messages exceeding a DB column limit — truncate/guard error logging paths
- Live schema drift (model has columns the DB doesn't yet) has bitten us 
  before — always pair model changes with a migration in the same task

## How I want you to work
- I'm not deeply experienced in real estate — when you're building 
  domain logic (scoring, valuation, market analysis), reason through it 
  like a senior professional in that field would, and explain your 
  reasoning, don't just hand me code
- Don't hold back on scope or sophistication to keep things "safe" or 
  minimal — I want the most thorough, well-reasoned result you're 
  capable of, not the smallest diff
- Before writing new code touching the DB, explore the actual current 
  schema/models rather than assuming — call out anything you need that 
  doesn't exist yet instead of building around invented fields
- This is pre-revenue and evolving fast — favor clean, extensible 
  structure over premature optimization, but don't skip data-quality 
  handling (flag gaps/missing data explicitly rather than silently 
  guessing)