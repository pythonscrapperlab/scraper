"""APScheduler jobs for periodic scraping."""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aevorex.config import settings

logger = logging.getLogger(__name__)


class ScraperJobs:
    """Scheduled scraper jobs."""

    def __init__(self):
        """Initialize jobs."""
        self.scheduler = AsyncIOScheduler()

    def register_jobs(self):
        """Register all scraper jobs."""
        if settings.zillow_enabled:
            self.scheduler.add_job(
                self._scrape_zillow,
                "interval",
                hours=settings.scheduler_zillow_interval_hours,
                id="zillow_scraper",
            )
            logger.info(f"Registered Zillow scraper job (every {settings.scheduler_zillow_interval_hours}h)")

        if settings.redfin_enabled:
            self.scheduler.add_job(
                self._scrape_redfin,
                "interval",
                hours=settings.scheduler_redfin_interval_hours,
                id="redfin_scraper",
            )
            logger.info(f"Registered Redfin scraper job (every {settings.scheduler_redfin_interval_hours}h)")

        if settings.realtor_enabled:
            self.scheduler.add_job(
                self._scrape_realtor,
                "interval",
                hours=settings.scheduler_realtor_interval_hours,
                id="realtor_scraper",
            )
            logger.info(f"Registered Realtor scraper job (every {settings.scheduler_realtor_interval_hours}h)")

    async def _scrape_zillow(self):
        """Zillow scraper job."""
        logger.info("Running Zillow scraper job...")
        from main import _scrape_single

        for state in settings.target_states:
            try:
                await _scrape_single("zillow", state)
            except Exception as e:
                logger.error(f"Error scraping Zillow for {state}: {e}")

    async def _scrape_redfin(self):
        """Redfin scraper job."""
        logger.info("Running Redfin scraper job...")
        from main import _scrape_single

        for state in settings.target_states:
            try:
                await _scrape_single("redfin", state)
            except Exception as e:
                logger.error(f"Error scraping Redfin for {state}: {e}")

    async def _scrape_realtor(self):
        """Realtor scraper job."""
        logger.info("Running Realtor scraper job...")
        from main import _scrape_single

        for state in settings.target_states:
            try:
                await _scrape_single("realtor", state)
            except Exception as e:
                logger.error(f"Error scraping Realtor for {state}: {e}")

    def start(self):
        """Start scheduler."""
        self.register_jobs()
        self.scheduler.start()
        logger.info("Scheduler started")


async def start_scheduler():
    """Initialize and start the scheduler."""
    jobs = ScraperJobs()
    jobs.start()

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Scheduler shutting down...")
        jobs.scheduler.shutdown()
