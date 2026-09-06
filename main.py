"""
CLI entry point for Aevorex scraper.

Commands:
- scrape --source [platform] --state [state]
- retry --source [platform] --state [state]
- analyze            (market-stats -> value -> score, in the only order that works)
- market-stats / value / score   (the individual stages)
- scheduler
"""

import asyncio
import logging
import sys

import click

from aevorex.config import settings
from aevorex.db import async_session_maker, close_engine
from aevorex.logging import setup_logging

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

    logger.info(f"Starting scrape: sources={sources}, states={states}")

    asyncio.run(_scrape_many(sources, states))


async def _scrape_many(sources: list[str], states: list[str]) -> None:
    """Run all requested scrapes within a single event loop."""
    for source in sources:
        for state in states:
            await _scrape_single(source, state)


async def _scrape_single(source: str, state: str) -> None:
    """Scrape a single source/state combination."""
    logger.info(f"Scraping {source} for {state}")

    # Import scraper and normalizer dynamically
    if source == "zillow":
        from aevorex.scrapers.zillow import ZillowScraper
        from aevorex.normalizers.zillow import ZillowNormalizer
        scraper = ZillowScraper()
        normalizer = ZillowNormalizer()
    elif source == "redfin":
        from aevorex.scrapers.redfin import RedfinScraper
        from aevorex.normalizers.redfin import RedfinNormalizer
        scraper = RedfinScraper()
        normalizer = RedfinNormalizer()
    elif source == "realtor":
        from aevorex.scrapers.realtor import RealtorScraper
        from aevorex.normalizers.realtor import RealtorNormalizer
        scraper = RealtorScraper()
        normalizer = RealtorNormalizer()
    else:
        logger.error(f"Unknown source: {source}")
        return

    from aevorex.pipeline.runner import PipelineRunner

    runner = PipelineRunner(scraper, normalizer)

    async with async_session_maker() as session: # type: ignore
        try:
            result = await runner.run_scrape(session, state)
            logger.info(f"Scrape result for {source}/{state}:")
            logger.info(f"  Total: {result['stats']['total_urls']}")
            logger.info(f"  Success: {result['stats']['success']}")
            logger.info(f"  Failed: {result['stats']['failed']}")
            logger.info(f"  Errors: {result['stats']['errors']}")
            logger.info(f"  Inserted: {result['inserted']}")
            logger.info(f"  Updated: {result['updated']}")

            if result["errors"]:
                print(result["errors"])
                logger.warning(f"  {len(result['errors'])} errors encountered")

        except Exception as e:
            logger.error(f"Error scraping {source}/{state}: {e}", exc_info=True)


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
    asyncio.run(_retry_single(source, state))


async def _retry_single(source: str, state: str) -> None:
    """Retry failed URLs for a single source/state."""
    from sqlalchemy import select

    from aevorex.db.models import ScrapeError

    async with async_session_maker() as session: # type: ignore
        try:
            # Fetch all failed URLs for this source/state
            query = select(ScrapeError).where(
                (ScrapeError.source == source)
                # Could filter by state if we store it in ScrapeError
            )
            result = await session.execute(query)
            errors = result.scalars().all()

            if not errors:
                logger.info(f"No failed URLs to retry for {source}/{state}")
                return

            logger.info(f"Found {len(errors)} failed URLs to retry")

            # Scrape them again
            if source == "zillow":
                from aevorex.scrapers.zillow import ZillowScraper
                from aevorex.normalizers.zillow import ZillowNormalizer
                scraper = ZillowScraper()
                normalizer = ZillowNormalizer()
            elif source == "redfin":
                from aevorex.scrapers.redfin import RedfinScraper
                from aevorex.normalizers.redfin import RedfinNormalizer
                scraper = RedfinScraper()
                normalizer = RedfinNormalizer()
            elif source == "realtor":
                from aevorex.scrapers.realtor import RealtorScraper
                from aevorex.normalizers.realtor import RealtorNormalizer
                scraper = RealtorScraper()
                normalizer = RealtorNormalizer()

            from aevorex.pipeline.runner import PipelineRunner

            runner = PipelineRunner(scraper, normalizer)

            urls = [err.url for err in errors]
            result = await runner.run_scrape(session, state, urls=urls)

            logger.info(f"Retry result: {result['stats']['success']} succeeded, {result['stats']['failed']} failed")

        except Exception as e:
            logger.error(f"Error retrying {source}/{state}: {e}", exc_info=True)


@cli.command()
def scheduler():
    """Start the APScheduler job scheduler."""
    logger.info("Starting APScheduler...")

    if not settings.scheduler_enabled:
        logger.error("Scheduler is disabled in config (SCHEDULER_ENABLED=false)")
        return

    from aevorex.scheduler.jobs import start_scheduler

    asyncio.run(start_scheduler())


@cli.command("market-stats")
def market_stats():
    """
    Rebuild zip/city/county/state market baselines from sold comps and live listings.

    Run this before `value` and `score` — both read these baselines to express
    a property's numbers relative to its own market rather than to a fixed
    national threshold. Prefer `analyze`, which runs the three in order.
    """
    logger.info("Building market statistics...")
    _run_stage(_build_market_stats())


async def _build_market_stats() -> dict:
    from aevorex.market.stats import build_market_stats

    async with async_session_maker() as session:  # type: ignore
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
    _run_stage(_value_all(all_properties, limit))


async def _value_all(all_properties: bool = False, limit: int = None) -> dict:
    from aevorex.valuation.engine import ValuationEngine

    async with async_session_maker() as session:  # type: ignore
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
    _run_stage(_score_all(all_properties))


async def _score_all(all_properties: bool = False) -> dict:
    """Run the scoring pass within a single session/transaction-per-property."""
    from aevorex.scoring.runner import ScoringRunner

    async with async_session_maker() as session:  # type: ignore
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
def analyze(all_properties: bool, skip_market_stats: bool):
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
    logger.info("Starting full analysis chain...")
    _run_stage(_analyze_all(all_properties, skip_market_stats))


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


def _run_stage(coro) -> None:
    """
    Drive one async stage, and exit non-zero if it fails.

    The stages used to swallow their own exceptions and log them, which meant
    a chained run would carry on past a broken stage and a CI or cron caller
    would see success. A failed stage must stop the chain.
    """
    try:
        asyncio.run(coro)
    except Exception as e:
        logger.error("Stage failed: %s", e, exc_info=True)
        sys.exit(1)


@cli.command()
def init_db():
    """Initialize database (run migrations)."""
    logger.info("Initializing database...")
    logger.info("Run: alembic upgrade head")


if __name__ == "__main__":
    cli()
