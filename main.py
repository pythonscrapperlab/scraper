"""
CLI entry point for Aevorex scraper.

Commands:
- scrape --source [platform] --state [state]
- retry --source [platform] --state [state]
- scheduler
"""

import asyncio
import logging

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


@cli.command()
def init_db():
    """Initialize database (run migrations)."""
    logger.info("Initializing database...")
    logger.info("Run: alembic upgrade head")


if __name__ == "__main__":
    cli()
