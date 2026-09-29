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

        if settings.scoring_enabled:
            self.scheduler.add_job(
                self._run_analysis,
                "interval",
                hours=settings.scheduler_scoring_interval_hours,
                id="property_scoring",
            )
            logger.info(f"Registered analysis job (every {settings.scheduler_scoring_interval_hours}h)")

    async def _scrape_zillow(self):
        """Zillow scraper job."""
        logger.info("Running Zillow scraper job...")
        from main import _scrape_single

        for state in settings.target_states:
            try:
                await _scrape_single("zillow", state)
            except Exception as exc:
                logger.error(
                    "Scheduled Zillow scrape failed: state=%s error_class=%s",
                    state,
                    type(exc).__name__,
                )

    async def _scrape_redfin(self):
        """Redfin scraper job."""
        logger.info("Running Redfin scraper job...")
        from main import _scrape_single

        for state in settings.target_states:
            try:
                await _scrape_single("redfin", state)
            except Exception as exc:
                logger.error(
                    "Scheduled Redfin scrape failed: state=%s error_class=%s",
                    state,
                    type(exc).__name__,
                )

    async def _scrape_realtor(self):
        """Realtor scraper job."""
        logger.info("Running Realtor scraper job...")
        from main import _scrape_single

        for state in settings.target_states:
            try:
                await _scrape_single("realtor", state)
            except Exception as exc:
                logger.error(
                    "Scheduled Realtor scrape failed: state=%s error_class=%s",
                    state,
                    type(exc).__name__,
                )

    async def _run_analysis(self):
        """
        Analysis job — rebuilds market baselines, values, then scores.

        This used to call scoring alone, which was wrong for any newly
        scraped market: four of the five scorers read `property_valuation`,
        so scoring without a preceding valuation pass writes analysis rows
        where only motivated_seller is populated. The scrape job and this job
        are the only things running on a live box, so if the chain is not
        here it is nowhere.
        """
        logger.info("Running analysis job (market-stats -> value -> score)...")
        from main import _analyze_all

        try:
            await _analyze_all()
        except Exception as exc:
            logger.error("Scheduled analysis failed: error_class=%s", type(exc).__name__)

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
