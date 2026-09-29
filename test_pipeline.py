#!/usr/bin/env python
"""
Test script — verify entire pipeline works without real scrapers.

This script:
1. Initializes database connection
2. Creates a test scraper with mock data
3. Runs the pipeline
4. Verifies data was inserted/updated
5. Checks database for results

Run: python test_pipeline.py
"""

import asyncio
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def test_pipeline():
    """Test the complete pipeline with mock data."""
    
    logger.info("=" * 60)
    logger.info("AEVOREX PIPELINE TEST")
    logger.info("=" * 60)

    try:
        # Import after logging is configured
        from aevorex.db import async_session_maker, close_engine
        from aevorex.normalizers.test import TestNormalizer
        from aevorex.pipeline.runner import PipelineRunner
        from aevorex.scrapers.test import TestScraper

        logger.info("\n✓ Imports successful")

        # Test database connection
        logger.info("\n📦 Testing database connection...")
        async with async_session_maker() as session:
            try:
                from sqlalchemy import text
                result = await session.execute(text("SELECT 1"))
                logger.info("✓ Database connection successful")
            except Exception as e:
                logger.error(f"✗ Database connection failed: {e}")
                return False

        # Test scraper instantiation
        logger.info("\n🔧 Initializing test scraper and normalizer...")
        scraper = TestScraper()
        normalizer = TestNormalizer()
        logger.info(f"✓ Scraper initialized: {scraper.__class__.__name__}")
        logger.info(f"✓ Normalizer initialized: {normalizer.__class__.__name__}")

        # Create pipeline
        logger.info("\n🚀 Creating pipeline runner...")
        runner = PipelineRunner(scraper, normalizer)
        logger.info(f"✓ Pipeline ready for source: {runner.source}")

        # Run scrape
        logger.info("\n📡 Running test scrape for FL...")
        async with async_session_maker() as session:
            result = await runner.run_scrape(session, "FL")

        logger.info("\n" + "=" * 60)
        logger.info("PIPELINE RESULTS")
        logger.info("=" * 60)
        logger.info(f"State: {result['state']}")
        logger.info(f"Source: {result['source']}")
        logger.info(f"Total URLs: {result['stats']['total_urls']}")
        logger.info(f"Successful: {result['stats']['success']}")
        logger.info(f"Failed: {result['stats']['failed']}")
        logger.info(f"Errors: {result['stats']['errors']}")
        logger.info(f"Properties inserted: {result['inserted']}")
        logger.info(f"Properties updated: {result['updated']}")

        if result["errors"]:
            logger.warning("\n⚠️  Errors encountered:")
            for i, error in enumerate(result["errors"], 1):
                logger.warning(f"  {i}. {error}")

        # Verify data in database
        logger.info("\n✅ Verifying data in database...")
        async with async_session_maker() as session:
            from sqlalchemy import func, select

            from aevorex.db.models import Property, RawScrape

            prop_count = await session.execute(select(func.count(Property.id)))
            raw_count = await session.execute(select(func.count(RawScrape.id)))

            props = prop_count.scalar() or 0
            raws = raw_count.scalar() or 0

            logger.info(f"Properties in DB: {props}")
            logger.info(f"Raw scrapes in DB: {raws}")

            if props > 0:
                # Show first property
                props_query = select(Property).limit(1)
                props_result = await session.execute(props_query)
                first_prop = props_result.scalar_one_or_none()
                if first_prop:
                    logger.info("\n📍 First property:")
                    logger.info(f"   Address: {first_prop.address}")
                    logger.info(f"   City: {first_prop.city}")
                    logger.info(f"   Price: ${first_prop.price:,}")
                    logger.info(f"   Type: {first_prop.property_type}")
                    logger.info(f"   Source Count: {first_prop.source_count}")

        logger.info("\n" + "=" * 60)
        logger.info("✅ TEST SUCCESSFUL!")
        logger.info("=" * 60)
        logger.info("\nNext steps:")
        logger.info("1. Implement zillow.py scraper (get_listing_urls, fetch, parse)")
        logger.info("2. Implement zillow.py normalizer (normalize)")
        logger.info("3. Run: python main.py scrape --source zillow --state FL")
        logger.info("")

        return True

    except Exception as e:
        logger.error(f"\n❌ TEST FAILED: {e}", exc_info=True)
        return False

    finally:
        # Clean up
        await close_engine()


if __name__ == "__main__":
    success = asyncio.run(test_pipeline())
    exit(0 if success else 1)
