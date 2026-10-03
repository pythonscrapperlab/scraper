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
- scheduler run | preview | run-now | clock-check | soak-start | soak-report | soak-daily
"""

import asyncio
import json
import logging
import os
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


@cli.group("scheduler")
def scheduler_group() -> None:
    """Windows scheduler service and its operator tools."""


@scheduler_group.command("run")
def scheduler_run() -> None:
    """Run the scheduler service in the foreground (NSSM runs exactly this)."""
    logger.info("Starting scheduler service...")
    _run_command("scheduler", {"pid": os.getpid()}, _start_scheduler)


async def _start_scheduler() -> dict[str, int]:
    from aevorex.scheduler.service import run_service

    return await run_service()


@scheduler_group.command("preview")
@click.option("--hours", type=float, default=48.0, show_default=True, help="Look-ahead window")
@click.option("--from", "start_text", default=None, help="UTC start, ISO-8601 (default: now)")
@click.option("--offline", is_flag=True, help="Skip Supabase; demo markets only")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
def scheduler_preview(hours: float, start_text: str | None, offline: bool, as_json: bool) -> None:
    """Dry run: print every check/refresh the service would fire. Executes nothing."""
    _run_untracked(lambda: _scheduler_preview(hours, start_text, offline, as_json))


async def _scheduler_preview(
    hours: float, start_text: str | None, offline: bool, as_json: bool
) -> None:
    from datetime import UTC, datetime

    from aevorex.publisher.remote import publisher_engine
    from aevorex.scheduler.cities import discover, remote_org_market_reader
    from aevorex.scheduler.config import load_config
    from aevorex.scheduler.preview import (
        NightlyInput,
        build_preview,
        refresh_durations,
        render_preview,
        simulate_nightly,
    )
    from aevorex.scheduler.preview import as_json as preview_json

    config = load_config(settings.scheduler_config_path)
    engine = None
    reader = None
    if not offline and settings.supabase_direct_connection_url is not None:
        engine = publisher_engine()
        reader = remote_org_market_reader(engine)
    try:
        discovery, _ = await discover(config, reader)
    finally:
        if engine is not None:
            await engine.dispose()
    start = (
        datetime.fromisoformat(start_text).astimezone(UTC) if start_text else datetime.now(UTC)
    )
    runs = build_preview(discovery.active, config, start, hours)
    durations = await refresh_durations(discovery.active, config)
    capacity = simulate_nightly(
        [
            NightlyInput(
                slug, city.timezone, config.for_market(slug), city.stagger_minutes,
                *durations[slug],
            )
            for slug, city in sorted(discovery.active.items())
        ],
        start,
    )
    if as_json:
        click.echo(json.dumps(preview_json(runs, capacity), indent=2))
    else:
        click.echo(render_preview(
            runs, capacity, unsupported=discovery.unsupported,
            remote_ok=discovery.remote_ok and (reader is not None or offline),
        ))


@scheduler_group.command("run-now")
@click.option("--market", "market_slug", default=None,
              help="Market slug (not needed for analyze/market-stats)")
@click.option(
    "--job",
    type=click.Choice(["check", "refresh", "nightly", "analyze", "market-stats", "publish"]),
    required=True,
    help="check = search check (+refresh/analyze/publish if changed); nightly = full refresh chain",
)
@click.option("--wait/--no-wait", default=False, help="Wait for a busy lane instead of failing")
def scheduler_run_now(market_slug: str | None, job: str, wait: bool) -> None:
    """Run one scheduled chain immediately, under the same locks as the service."""
    if job not in {"analyze", "market-stats"} and not market_slug:
        raise click.UsageError("--market is required for this job")
    _run_untracked(lambda: _scheduler_run_now(market_slug or "*", job.replace("-", "_"), wait))


async def _scheduler_run_now(slug: str, job: str, wait: bool) -> None:
    from aevorex.publisher.markets import market_definition
    from aevorex.scheduler.cities import ActiveCity, city_zips, discover
    from aevorex.scheduler.config import load_config
    from aevorex.scheduler.jobs import JobRunner
    from aevorex.scheduler.ops import EngineOperations
    from aevorex.scheduler.slots import stagger_offset_minutes

    config = load_config(settings.scheduler_config_path)
    discovery, _ = await discover(config, None)
    if slug != "*" and slug not in discovery.active:
        definition = market_definition(slug)  # supported slug that is merely not demo/org-active
        zips = city_zips(definition.city, definition.state)
        discovery.active[definition.slug] = ActiveCity(
            definition.slug, definition.city, definition.state, definition.timezone, zips,
            ("manual",),
            stagger_offset_minutes(definition.slug, zips, config.for_market(slug).stagger_minutes),
        )
    ops = EngineOperations()
    runner = JobRunner(config, ops, lambda: discovery.active, wait_for_locks=wait)
    try:
        result = await runner.run_now(slug, job)
    finally:
        await ops.close()
    click.echo(json.dumps(
        {"market": result.slug, "job": result.kind, "status": result.status,
         "reason": result.reason, "stages": result.stages},
        indent=2, sort_keys=True, default=str,
    ))
    if result.status in {"failed", "skipped"}:
        raise click.exceptions.Exit(1)


@scheduler_group.command("clock-check")
def scheduler_clock_check() -> None:
    """Compare the laptop clock with NTP (falls back to an HTTPS Date header)."""
    _run_untracked(_scheduler_clock_check)


async def _scheduler_clock_check() -> None:
    from aevorex.scheduler.config import load_config
    from aevorex.scheduler.drift import check_clock

    service = load_config(settings.scheduler_config_path).service
    reading = await check_clock(service.clock_warn_seconds, service.clock_fail_seconds)
    click.echo(json.dumps(
        {"status": reading.status, "source": reading.source,
         "offset_seconds": reading.offset_seconds}, indent=2))
    if reading.status == "fail":
        raise click.exceptions.Exit(2)


@scheduler_group.command("soak-start")
@click.option("--label", default=None, help="Soak label (default: soak-YYYYMMDD-HHMM)")
def scheduler_soak_start(label: str | None) -> None:
    """Record the 'before' publisher status for a soak run."""
    _run_untracked(lambda: _soak_start(label))


async def _soak_start(label: str | None) -> None:
    from datetime import UTC, datetime

    from aevorex.scheduler import report

    now = datetime.now(UTC)
    name = label or f"soak-{now:%Y%m%d-%H%M}"
    status = await _with_publisher("status")
    path = report.write_start(name, status, now)
    click.echo(f"Soak '{name}' started; before-status saved to {path}")


@scheduler_group.command("soak-report")
@click.option("--label", required=True)
@click.option("--append/--print-only", default=True, help="Append to docs/progress.md")
def scheduler_soak_report(label: str, append: bool) -> None:
    """Capture 'after' status, summarise per-run metrics and append to docs/progress.md."""
    _run_untracked(lambda: _soak_report(label, append))


async def _soak_report(label: str, append: bool) -> None:
    from datetime import UTC, datetime

    from aevorex.scheduler import report

    saved = report.read_start(label)
    started = datetime.fromisoformat(saved["started_at"])
    now = datetime.now(UTC)
    after = await _with_publisher("status")
    runs = await report.load_runs(started)
    markdown = report.render_report(
        label, started, now, saved["before"], json.loads(json.dumps(after, default=str)),
        runs, "Services: aevoraex-scheduler under NSSM on the Windows laptop.",
    )
    if append:
        report.append_progress(markdown)
        click.echo("Appended to docs/progress.md")
    else:
        click.echo(markdown)


@scheduler_group.command("soak-daily")
@click.option("--label", default="e5-7d", show_default=True, help="Soak label")
@click.option("--hours", default=24, show_default=True, help="Trailing window length")
@click.option("--append/--print-only", default=True, help="Upsert today's row in docs/progress.md")
def scheduler_soak_daily(label: str, hours: int, append: bool) -> None:
    """Record one day of soak metrics (checks, refreshes, blocks, DB size, late events)."""
    _run_untracked(lambda: _soak_daily(label, hours, append))


async def _soak_daily(label: str, hours: int, append: bool) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from sqlalchemy import text

    from aevorex.scheduler import report, soak

    now = datetime.now(UTC)
    start_file = report.start_path(label)
    if start_file.exists():
        started = datetime.fromisoformat(report.read_start(label)["started_at"])
    else:
        started = now
        report.write_start(label, {}, started)  # day 0: the soak starts when first recorded
    window_start, window_end = soak.window_for(now, hours)
    runs = await report.load_runs(window_start)
    async with async_session_maker() as session:
        local_bytes = int(await session.scalar(text("select pg_database_size(current_database())")) or 0)
    cloud: float | None = None
    try:
        cloud = float((await _with_publisher("status"))["database_size_mb"])
    except Exception as exc:  # the soak row must still be written, with the gap stated
        logger.warning("Cloud size unavailable for soak row: error_class=%s", type(exc).__name__)
    metrics = soak.summarize_day(
        runs, soak.read_log_lines(soak.log_files(), window_start),
        day=now.astimezone().date(), start=window_start, end=window_end,
        local_db_bytes=local_bytes, cloud_db_mib=cloud,
    )
    row = soak.render_row(metrics)
    if not append:
        click.echo(row)
        return
    path = Path("docs/progress.md")
    document = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(
        soak.upsert_row(document, row, metrics.day, label=label, started=started), encoding="utf-8"
    )
    click.echo(f"Soak row for {metrics.day} written to docs/progress.md")


def _run_untracked(operation: Callable[[], Awaitable[None]]) -> None:
    """Operator commands record their own stage runs; they must not double-count."""

    async def execute() -> None:
        try:
            await operation()
        finally:
            await close_engine()

    try:
        asyncio.run(execute())
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        error_class = type(exc).__name__
        logger.error("Command failed: error_class=%s", error_class)
        raise click.ClickException(f"Command failed ({error_class})") from None


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
