"""
CLI entry point for Aevorex scraper.

Commands:
- scrape --source [platform] --state [state]
- retry --source [platform] --state [state]
- check --city [market-slug]
- refresh --city [market-slug] | --pending
- analyze --changed
- analyze            (market-stats -> value -> score, in the only order that works)
- market-stats / value / score   (the individual stages)
- scheduler
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

import click

from aevorex.config import settings
from aevorex.db import async_session_maker, close_engine
from aevorex.logging import setup_logging
from aevorex.run_tracking import JsonValue, tracked

T = TypeVar("T")

# Configure logging
setup_logging()
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """Aevorex scraper CLI."""
    pass


@cli.group("publisher")
def publisher_group() -> None:
    """Push the local source of truth to the Supabase serving cache."""


@publisher_group.command("push")
@click.option("--market", "market_slug", required=True, help="Market slug")
def publisher_push(market_slug: str) -> None:
    """Publish one market in serving-v2 transaction groups."""
    _run_command(
        "publisher_push",
        {"city": market_slug},
        lambda: _publisher_push(market_slug),
    )


@publisher_group.command("status")
def publisher_status() -> None:
    """Show schema, freshness, database size, and per-table row counts."""
    _run_command("publisher_status", {}, _publisher_status)


@publisher_group.command("rebuild")
@click.option("--market", "market_slug", default=None, help="One market slug")
@click.option("--all", "all_markets", is_flag=True, help="All configured Redfin markets")
def publisher_rebuild(market_slug: str | None, all_markets: bool) -> None:
    """Recreate one or all configured markets from local truth."""
    if bool(market_slug) == all_markets:
        raise click.UsageError("Choose exactly one of --market or --all")
    _run_command(
        "publisher_rebuild",
        {"city": market_slug, "all_markets": all_markets},
        lambda: _publisher_rebuild(market_slug, all_markets),
    )


@publisher_group.command("prune")
@click.option("--market", "market_slug", required=True, help="Market slug")
@click.option("--dry-run/--apply", default=True, help="Preview by default")
def publisher_prune(market_slug: str, dry_run: bool) -> None:
    """Preview or apply exact serving-retention pruning."""
    _run_command(
        "publisher_prune",
        {"city": market_slug, "dry_run": dry_run},
        lambda: _publisher_prune(market_slug, dry_run),
    )


@publisher_group.command("drift")
def publisher_drift() -> None:
    """Compare the linked serving schema with serving v2."""
    _run_command("publisher_drift", {}, _publisher_drift)


@publisher_group.command("wake")
def publisher_wake() -> None:
    """Write a publisher readiness heartbeat without scheduling work."""
    _run_command("publisher_wake", {}, _publisher_wake)


async def _with_publisher(method: str, *args: object, **kwargs: object) -> Any:
    from aevorex.publisher import Publisher

    publisher = Publisher()
    try:
        return await getattr(publisher, method)(*args, **kwargs)
    finally:
        await publisher.close()


async def _publisher_push(market_slug: str) -> dict[str, int | float | bool]:
    result = await _with_publisher("push", market_slug)
    telemetry = result.telemetry()
    click.echo(json.dumps({"market": result.market, "status": result.status, **telemetry}, sort_keys=True))
    return telemetry


async def _publisher_status() -> dict[str, int]:
    status = await _with_publisher("status")
    click.echo(json.dumps(status, indent=2, default=str, sort_keys=True))
    return {"tables": len(status["table_counts"]), "markets": len(status["markets"])}


async def _publisher_rebuild(
    market_slug: str | None, _all_markets: bool
) -> dict[str, int]:
    from sqlalchemy import select

    from aevorex.db.models import MarketFreshness
    from aevorex.publisher import Publisher

    if market_slug:
        slugs = [market_slug]
    else:
        async with async_session_maker() as session:
            slugs = list(
                (await session.execute(select(MarketFreshness.slug).order_by(MarketFreshness.slug)))
                .scalars()
                .all()
            )
    publisher = Publisher()
    rebuilt = 0
    partial = 0
    try:
        for slug in slugs:
            if slug is None:
                continue
            result = await publisher.rebuild(slug)
            rebuilt += 1
            partial += result.status == "partial"
    finally:
        await publisher.close()
    return {"rebuilt": rebuilt, "partial": partial}


async def _publisher_prune(market_slug: str, dry_run: bool) -> dict[str, int]:
    result = await _with_publisher("prune", market_slug, dry_run=dry_run)
    click.echo(json.dumps(result, sort_keys=True))
    return result


async def _publisher_drift() -> dict[str, int | bool]:
    result = await _with_publisher("drift")
    click.echo(json.dumps(result, indent=2, sort_keys=True))
    return {"ok": bool(result["ok"]), "column_drift": len(result["column_drift"])}


async def _publisher_wake() -> dict[str, int]:
    result = await _with_publisher("wake")
    click.echo(json.dumps(result, sort_keys=True))
    return result


@cli.command("check")
@click.option("--city", "market_slug", required=True, help="Market slug, e.g. orlando-fl")
def check_market(market_slug: str) -> None:
    """Run one complete Redfin search-results-only market check."""
    from aevorex.freshness.check import run_check

    logger.info("Starting search-level check: market=%s", market_slug)
    _run_command("check", {"city": market_slug}, lambda: run_check(market_slug))


@cli.command("refresh")
@click.option("--city", "market_slug", default=None, help="Refresh all active city listings")
@click.option("--pending", "pending_only", is_flag=True, help="Consume due queued listings")
def refresh_market(market_slug: str | None, pending_only: bool) -> None:
    """Refresh Redfin property details with concurrency three."""
    from aevorex.freshness.refresh import run_refresh

    if (market_slug is None) == (not pending_only):
        raise click.UsageError("Choose exactly one of --city or --pending")
    scope: dict[str, JsonValue] = {
        "city": market_slug,
        "pending": pending_only,
    }
    _run_command(
        "refresh",
        scope,
        lambda: run_refresh(market_slug=market_slug, pending_only=pending_only),
    )


@cli.command()
@click.option(
    "--source",
    type=click.Choice(["zillow", "redfin", "realtor", "all"]),
    required=True,
    help="Data source to scrape",
)
@click.option(
    "--state",
    multiple=True,
    type=click.Choice(["FL", "CA"]),
    required=True,
    help="State(s) to scrape (can be used multiple times)",
)
def scrape(source: str, state: tuple):
    """Run one or more scrapers for given state(s)."""
    states = list(state) if state else settings.target_states
    sources = ["zillow", "redfin", "realtor"] if source == "all" else [source]

    logger.info("Starting scrape for %s source(s) and %s state(s)", len(sources), len(states))
    _run_command(
        "scrape",
        {"sources": sources, "states": states},
        lambda: _scrape_many(sources, states),
    )


async def _scrape_many(sources: list[str], states: list[str]) -> dict[str, int]:
    """Run all requested scrapes within a single event loop."""
    counts = {"total_urls": 0, "success": 0, "failed": 0, "inserted": 0, "updated": 0}
    for source in sources:
        for state in states:
            result = await _scrape_single(source, state)
            stats = result.get("stats", {})
            counts["total_urls"] += int(stats.get("total_urls", 0))
            counts["success"] += int(stats.get("success", 0))
            counts["failed"] += int(stats.get("failed", 0))
            counts["inserted"] += int(result.get("inserted", 0))
            counts["updated"] += int(result.get("updated", 0))
    return counts


async def _scrape_single(source: str, state: str) -> dict[str, Any]:
    """Scrape a single source/state combination."""
    logger.info(f"Scraping {source} for {state}")

    # Import scraper and normalizer dynamically
    if source == "zillow":
        from aevorex.normalizers.zillow import ZillowNormalizer
        from aevorex.scrapers.zillow import ZillowScraper
        scraper = ZillowScraper()
        normalizer = ZillowNormalizer()
    elif source == "redfin":
        from aevorex.normalizers.redfin import RedfinNormalizer
        from aevorex.scrapers.redfin import RedfinScraper
        scraper = RedfinScraper()
        normalizer = RedfinNormalizer()
    elif source == "realtor":
        from aevorex.normalizers.realtor import RealtorNormalizer
        from aevorex.scrapers.realtor import RealtorScraper
        scraper = RealtorScraper()
        normalizer = RealtorNormalizer()
    else:
        raise ValueError(f"Unsupported source identifier: {source}")

    from aevorex.pipeline.runner import PipelineRunner

    runner = PipelineRunner(scraper, normalizer)

    async with async_session_maker() as session:
        result = await runner.run_scrape(session, state)
        logger.info(
            "Scrape completed: source=%s state=%s total=%s success=%s failed=%s "
            "inserted=%s updated=%s",
            source,
            state,
            result["stats"]["total_urls"],
            result["stats"]["success"],
            result["stats"]["failed"],
            result["inserted"],
            result["updated"],
        )
        return result


@cli.command()
@click.option(
    "--source",
    type=click.Choice(["zillow", "redfin", "realtor"]),
    required=True,
    help="Data source to retry",
)
@click.option(
    "--state",
    type=click.Choice(["FL", "CA"]),
    required=True,
    help="State to retry",
)
def retry(source: str, state: str):
    """Retry all failed URLs for a source/state."""
    logger.info(f"Retrying failed URLs: source={source}, state={state}")
    _run_command("retry", {"source": source, "state": state}, lambda: _retry_single(source, state))


async def _retry_single(source: str, state: str) -> dict[str, int]:
    """Retry failed URLs for a single source/state."""
    from sqlalchemy import select

    from aevorex.db.models import ScrapeError

    async with async_session_maker() as session:
        query = select(ScrapeError).where(ScrapeError.source == source)
        result = await session.execute(query)
        errors = result.scalars().all()

        if not errors:
            logger.info("No failed URLs to retry for source=%s state=%s", source, state)
            return {"queued": 0, "success": 0, "failed": 0}

        logger.info("Retrying %s failed URL(s)", len(errors))

        if source == "zillow":
            from aevorex.normalizers.zillow import ZillowNormalizer
            from aevorex.scrapers.zillow import ZillowScraper

            scraper = ZillowScraper()
            normalizer = ZillowNormalizer()
        elif source == "redfin":
            from aevorex.normalizers.redfin import RedfinNormalizer
            from aevorex.scrapers.redfin import RedfinScraper

            scraper = RedfinScraper()
            normalizer = RedfinNormalizer()
        else:
            from aevorex.normalizers.realtor import RealtorNormalizer
            from aevorex.scrapers.realtor import RealtorScraper

            scraper = RealtorScraper()
            normalizer = RealtorNormalizer()

        from aevorex.pipeline.runner import PipelineRunner

        runner = PipelineRunner(scraper, normalizer)
        scrape_result = await runner.run_scrape(
            session, state, urls=[error.url for error in errors]
        )
        stats = scrape_result["stats"]
        logger.info("Retry completed: success=%s failed=%s", stats["success"], stats["failed"])
        return {
            "queued": len(errors),
            "success": int(stats["success"]),
            "failed": int(stats["failed"]),
        }


@cli.command()
def scheduler():
    """Start the APScheduler job scheduler."""
    logger.info("Starting APScheduler...")
    _run_command("scheduler", {}, _start_scheduler)


async def _start_scheduler() -> dict[str, int]:
    if not settings.scheduler_enabled:
        raise RuntimeError("SchedulerDisabled")

    from aevorex.scheduler.jobs import start_scheduler

    await start_scheduler()
    return {"stopped": 1}


@cli.command("market-stats")
def market_stats():
    """
    Rebuild zip/city/county/state market baselines from sold comps and live listings.

    Run this before `value` and `score` — both read these baselines to express
    a property's numbers relative to its own market rather than to a fixed
    national threshold. Prefer `analyze`, which runs the three in order.
    """
    logger.info("Building market statistics...")
    _run_command("market-stats", {}, _build_market_stats)


async def _build_market_stats() -> dict:
    from aevorex.market.stats import build_market_stats

    async with async_session_maker() as session:
        stats = await build_market_stats(session)
        logger.info(
            "Market stats: %s cells written (%s sold, %s listing)",
            stats["cells_written"], stats["sold_cells"], stats["listing_cells"],
        )
        return stats


@cli.command()
@click.option("--all", "all_properties", is_flag=True,
              help="Revalue every property, not just those flagged needs_analysis.")
@click.option("--limit", type=int, default=None, help="Cap the number valued (for testing).")
def value(all_properties: bool, limit: int):
    """
    Derive market value, ARV, rehab, rent and carrying costs for each property.

    Reads the baselines written by `market-stats`, so run that first. Writes
    property_valuation, which every scorer then reads. Prefer `analyze`.
    """
    logger.info("Starting valuation pass...")
    _run_command(
        "value",
        {"all_properties": all_properties, "limit": limit},
        lambda: _value_all(all_properties, limit),
    )


async def _value_all(all_properties: bool = False, limit: int = None) -> dict:
    from aevorex.valuation.engine import ValuationEngine

    async with async_session_maker() as session:
        stats = await ValuationEngine().run(
            session, all_properties=all_properties, limit=limit
        )
        logger.info(
            "Valuation: total=%s valued=%s no_value=%s errors=%s",
            stats["total"], stats["valued"], stats["no_value"], stats["errors"],
        )
        return stats


@cli.command()
@click.option("--all", "all_properties", is_flag=True,
              help="Rescore every property, not just those flagged needs_analysis.")
def score(all_properties: bool):
    """
    Score every property against all 5 investment strategies, then rank them.

    Requires `value` to have run first: four of the five strategies read
    property_valuation and return NULL without it. Prefer `analyze`.
    """
    logger.info("Starting scoring run...")
    _run_command(
        "score",
        {"all_properties": all_properties},
        lambda: _score_all(all_properties),
    )


async def _score_all(all_properties: bool = False) -> dict:
    """Run the scoring pass within a single session/transaction-per-property."""
    from aevorex.scoring.runner import ScoringRunner

    async with async_session_maker() as session:
        stats = await ScoringRunner().run(session, all_properties=all_properties)
        logger.info(
            "Scoring result: total=%s scored=%s unvalued=%s errors=%s ranked=%s",
            stats["total"], stats["scored"], stats.get("unvalued"),
            stats["errors"], stats["ranked"],
        )
        return stats


@cli.command()
@click.option("--all", "all_properties", is_flag=True,
              help="Reprocess every property, not just those flagged needs_analysis.")
@click.option("--skip-market-stats", is_flag=True,
              help="Reuse the existing baselines instead of rebuilding them.")
@click.option("--changed", is_flag=True,
              help="Run E2 value + score for needs_analysis rows and record tier moves.")
def analyze(all_properties: bool, skip_market_stats: bool, changed: bool):
    """
    Run the full analysis chain: market-stats -> value -> score.

    WHY THIS EXISTS
    ---------------
    The three stages are strictly ordered and each depends on the one before:
    `value` reads the baselines `market-stats` writes, and four of the five
    scorers read the `property_valuation` rows `value` writes. Running `score`
    on freshly scraped inventory without the first two stages does not fail —
    it produces analysis rows in which only motivated_seller is populated and
    every other strategy carries a "couldn't score" rationale. That is exactly
    what happened to the first Palm Coast run: 416 properties scraped, 416
    analysis rows written, 0 valuations behind them.

    The ordering is also load-bearing for the `needs_analysis` flag. Both
    `value` and `score` select on it and `score` clears it, so scoring first
    leaves the valuation stage with nothing to do and no way to notice. Here
    the flag is read by valuation and only then consumed by scoring.
    """
    if changed and all_properties:
        raise click.UsageError("--changed and --all are mutually exclusive")
    logger.info("Starting analysis chain...")
    _run_command(
        "analyze",
        {
            "all_properties": all_properties,
            "skip_market_stats": skip_market_stats,
            "changed": changed,
        },
        lambda: _analyze_changed() if changed else _analyze_all(
            all_properties, skip_market_stats
        ),
    )


async def _analyze_changed() -> dict[str, object]:
    """Run the E2 incremental analysis stage without rebuilding nightly baselines."""
    from aevorex.freshness.analyze import run_changed_analysis

    return await run_changed_analysis(nightly=False)


async def _analyze_all(all_properties: bool = False,
                       skip_market_stats: bool = False) -> dict:
    """The chain itself. Aborts on the first stage that raises."""
    results: dict = {}
    if skip_market_stats:
        logger.info("Skipping market-stats rebuild (--skip-market-stats).")
    else:
        logger.info("Stage 1/3: market baselines")
        results["market_stats"] = await _build_market_stats()

    logger.info("Stage 2/3: valuation")
    results["valuation"] = await _value_all(all_properties=all_properties)

    logger.info("Stage 3/3: scoring")
    results["scoring"] = await _score_all(all_properties=all_properties)
    return results


def _run_command(
    kind: str,
    scope: Mapping[str, JsonValue],
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Run one CLI operation with a durable ledger row and sanitized failure."""
    async def execute() -> T:
        try:
            return await tracked(kind, scope, operation)
        finally:
            await close_engine()

    try:
        return asyncio.run(execute())
    except Exception as exc:
        error_class = type(exc).__name__
        logger.error("Command failed: kind=%s error_class=%s", kind, error_class)
        raise click.ClickException(f"Command failed ({error_class})") from None


@cli.command()
def init_db():
    """Initialize database (run migrations)."""
    from alembic import command
    from alembic.config import Config

    logger.info("Applying local Alembic migrations...")
    try:
        command.upgrade(Config("alembic.ini"), "head")
    except Exception as exc:
        error_class = type(exc).__name__
        logger.error("Database initialization failed: error_class=%s", error_class)
        raise click.ClickException(
            f"Database initialization failed ({error_class})"
        ) from None

    async def completed() -> dict[str, int]:
        return {"migrations_applied": 1}

    _run_command("init-db", {}, completed)


if __name__ == "__main__":
    cli()
