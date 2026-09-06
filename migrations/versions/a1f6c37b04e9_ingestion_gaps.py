"""Close the Redfin ingestion gaps: unpriced events, offer insights, real status, ai summary.

Strictly additive:
  - `price_history.price` NOT NULL -> nullable (the only relaxation; without
    it, every unpriced event type is discarded on insert)
  - new nullable columns on `price_history` and `properties`
  - one unique index, attempted concurrently and skipped cleanly if existing
    duplicates block it

No existing row is read, rewritten, or deleted. Rows written before this
migration keep their stale values (listing_status = 'InStock', NULL in every
new column) by design — the backfill from `raw_scrapes.raw_json` is a
separate exercise.
"""
import logging

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1f6c37b04e9'
down_revision = 'c4d8f1a2b9e3'
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# One row per (property, platform, event wording, timestamp, price, listing
# run). COALESCE rather than a plain column list because Postgres treats
# NULLs as distinct in a unique index: with `price` now nullable and every
# unpriced "Listing Removed" carrying NULL, a bare unique index would happily
# admit the same removal event a hundred times over. `-1` is safe as the
# price sentinel (no real price is negative) and `''` as the id sentinel (an
# empty MLS id is never emitted).
UNIQUE_INDEX_NAME = "uq_price_history_event"
UNIQUE_INDEX_COLUMNS = (
    "property_id, source, event, event_date, "
    "COALESCE(price, -1), COALESCE(source_event_id, '')"
)

DUPLICATE_CHECK_SQL = f"""
    SELECT count(*) FROM (
        SELECT {UNIQUE_INDEX_COLUMNS}
        FROM price_history
        GROUP BY 1, 2, 3, 4, 5, 6
        HAVING count(*) > 1
    ) duplicated
"""


def upgrade() -> None:
    """Upgrade migration."""
    # ---- Fix 1: stop dropping unpriced events ----
    op.alter_column("price_history", "price", existing_type=sa.Integer(), nullable=True)
    op.add_column("price_history", sa.Column("event_type", sa.String(length=30), nullable=True))
    op.add_column("price_history", sa.Column("event_source", sa.String(length=150), nullable=True))
    op.add_column("price_history", sa.Column("source_event_id", sa.String(length=64), nullable=True))
    op.create_index(
        "idx_price_history_event_type", "price_history", ["property_id", "event_type"], unique=False
    )
    op.create_index(
        op.f("ix_price_history_event_type"), "price_history", ["event_type"], unique=False
    )

    # ---- Fix 2: offer_insights ----
    op.add_column("properties", sa.Column("price_drop_count", sa.Integer(), nullable=True))
    op.add_column("properties", sa.Column("sale_to_list_pct", sa.Float(), nullable=True))
    op.add_column("properties", sa.Column("area_price_drop_pct", sa.Float(), nullable=True))
    op.add_column("properties", sa.Column("area_avg_days_to_pending", sa.Integer(), nullable=True))
    op.add_column("properties", sa.Column("area_median_list_price", sa.Integer(), nullable=True))

    # ---- Fix 3: real MLS status alongside the schema.org one ----
    op.add_column("properties", sa.Column("listing_status_normalized", sa.String(length=30), nullable=True))
    op.add_column("properties", sa.Column("availability_status", sa.String(length=50), nullable=True))
    op.create_index(
        op.f("ix_properties_listing_status_normalized"),
        "properties",
        ["listing_status_normalized"],
        unique=False,
    )

    # ---- Fix 4: ai_summary ----
    op.add_column("properties", sa.Column("ai_summary", sa.Text(), nullable=True))

    # ---- smaller issue 2: listing freshness ----
    op.add_column("properties", sa.Column("source_updated_at", sa.DateTime(), nullable=True))

    _try_create_unique_event_index()


def _try_create_unique_event_index() -> None:
    """
    Add the price_history uniqueness guard, or explain why it couldn't be.

    This table already holds duplicate groups from before the pipeline
    deduped on write, so the index genuinely may not apply. That is an
    expected outcome, not a failure: uniqueness is also enforced in
    PipelineRunner._upsert_price_history, which is what actually protects
    new scrapes. Deleting the existing duplicates to force the index through
    is explicitly out of scope, so this backs off and leaves the migration
    additive instead.

    CONCURRENTLY keeps the build from taking an ACCESS EXCLUSIVE lock on a
    64k-row table that the scraper may be writing to, and requires running
    outside the migration's transaction — hence autocommit_block.
    """
    bind = op.get_bind()

    duplicate_groups = bind.execute(sa.text(DUPLICATE_CHECK_SQL)).scalar() or 0
    if duplicate_groups:
        logger.warning(
            "Skipping %s: price_history already holds %s duplicate group(s) on "
            "(property_id, source, event, event_date, price, source_event_id). "
            "Existing rows are deliberately left untouched; uniqueness is enforced "
            "in PipelineRunner._upsert_price_history for new scrapes. Re-run this "
            "index creation by hand after the duplicates are reconciled.",
            UNIQUE_INDEX_NAME,
            duplicate_groups,
        )
        return

    try:
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {UNIQUE_INDEX_NAME} "
                    f"ON price_history ({UNIQUE_INDEX_COLUMNS})"
                )
            )
        logger.info("Created %s.", UNIQUE_INDEX_NAME)
        return
    except Exception as exc:  # noqa: BLE001 - the whole point is to not fail the migration
        logger.warning(
            "Concurrent build of %s failed (%s: %s); falling back to a plain index build.",
            UNIQUE_INDEX_NAME,
            type(exc).__name__,
            exc,
        )
        # A failed CONCURRENTLY build leaves an INVALID index behind that is
        # ignored by queries but still enforces uniqueness on INSERT — worse
        # than having no index at all, since it's invisible in every plan.
        _drop_index_quietly()

    # The concurrent path can fail for reasons that have nothing to do with
    # the data — most commonly the driver running DDL through an extended
    # query protocol that Postgres treats as a transaction block. The
    # duplicate check above already passed, so a plain build is safe here:
    # duplicate-free table, lock held only for the build itself.
    #
    # Inside a SAVEPOINT, because a failed statement aborts the whole
    # Postgres transaction — without it, a late failure here would take every
    # column added above down with it and the migration would look like it
    # had simply not run.
    try:
        with bind.begin_nested():
            bind.execute(
                sa.text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INDEX_NAME} "
                    f"ON price_history ({UNIQUE_INDEX_COLUMNS})"
                )
            )
        logger.info("Created %s (non-concurrent build).", UNIQUE_INDEX_NAME)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not create %s (%s: %s). The migration stays additive and uniqueness "
            "is enforced in PipelineRunner._upsert_price_history instead.",
            UNIQUE_INDEX_NAME,
            type(exc).__name__,
            exc,
        )


def _drop_index_quietly() -> None:
    """Best-effort cleanup of a half-built index; never fails the migration."""
    for statement in (
        f"DROP INDEX CONCURRENTLY IF EXISTS {UNIQUE_INDEX_NAME}",
        f"DROP INDEX IF EXISTS {UNIQUE_INDEX_NAME}",
    ):
        try:
            with op.get_context().autocommit_block():
                op.execute(sa.text(statement))
            return
        except Exception:  # noqa: BLE001
            continue
    logger.warning(
        "Left a possibly-invalid index named %s behind; drop it manually with "
        "DROP INDEX CONCURRENTLY IF EXISTS %s.",
        UNIQUE_INDEX_NAME,
        UNIQUE_INDEX_NAME,
    )


def downgrade() -> None:
    """Downgrade migration."""
    bind = op.get_bind()

    try:
        with op.get_context().autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {UNIQUE_INDEX_NAME}"))
    except Exception:  # noqa: BLE001
        op.execute(sa.text(f"DROP INDEX IF EXISTS {UNIQUE_INDEX_NAME}"))

    op.drop_column("properties", "source_updated_at")
    op.drop_column("properties", "ai_summary")
    op.drop_index(op.f("ix_properties_listing_status_normalized"), table_name="properties")
    op.drop_column("properties", "availability_status")
    op.drop_column("properties", "listing_status_normalized")
    op.drop_column("properties", "area_median_list_price")
    op.drop_column("properties", "area_avg_days_to_pending")
    op.drop_column("properties", "area_price_drop_pct")
    op.drop_column("properties", "sale_to_list_pct")
    op.drop_column("properties", "price_drop_count")

    op.drop_index(op.f("ix_price_history_event_type"), table_name="price_history")
    op.drop_index("idx_price_history_event_type", table_name="price_history")
    op.drop_column("price_history", "source_event_id")
    op.drop_column("price_history", "event_source")
    op.drop_column("price_history", "event_type")

    # Only restore NOT NULL if it can be restored without destroying data.
    # Once a scrape has run against this schema there are legitimately
    # unpriced events in here, and forcing the constraint back would mean
    # deleting them — never do that silently in a downgrade.
    unpriced = bind.execute(
        sa.text("SELECT count(*) FROM price_history WHERE price IS NULL")
    ).scalar() or 0
    if unpriced:
        logger.warning(
            "Leaving price_history.price nullable: %s row(s) have a NULL price and "
            "restoring NOT NULL would require deleting them.",
            unpriced,
        )
    else:
        op.alter_column("price_history", "price", existing_type=sa.Integer(), nullable=False)
