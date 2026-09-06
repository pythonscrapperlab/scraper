"""Data-audit fixes: distress signals, typed economics, rental-event tagging.

Follows the 2026-08-18 ingestion audit. Additive except for two deliberate,
bounded data operations, both called out below.

Schema
------
  properties      +32 nullable columns — seller-distress signals extracted
                  from listing remarks, plus the economics/climate fields
                  promoted out of the untyped `meta` JSON so they can be
                  indexed and filtered.
  tax_history     +land_value, +improvement_value. `assessed_value` is now
                  only populated when both components are present; it used to
                  treat a missing land value as zero and silently understate
                  the assessment.
  price_history   +is_rental_event. Redfin files rentals and sales in one
                  timeline at three orders of magnitude apart.

Data operations (not additive)
------------------------------
  1. `UPDATE properties SET state = upper(state)` — 2 rows ("Fl", "fl").
     These fall out of every state-scoped query and the APN dedup lookup.
  2. Deletes 506 exact-duplicate `price_history` rows (488 groups), keeping
     the oldest of each, so the uniqueness guard that the previous migration
     had to skip can finally be created. A "duplicate" here means identical
     on every column the unique index keys on, so nothing distinguishable is
     lost.

`property_scores` (14,268 rows, a frozen legacy scoring snapshot from
2026-07-26, superseded by property_analysis) is deliberately LEFT IN PLACE.
It is unreferenced by any code, but it is real historical data and dropping
it is not this migration's job.
"""
import logging

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e2d94c5a11'
down_revision = 'a1f6c37b04e9'
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

UNIQUE_INDEX_NAME = "uq_price_history_event"
UNIQUE_INDEX_COLUMNS = (
    "property_id, source, event, event_date, "
    "COALESCE(price, -1), COALESCE(source_event_id, '')"
)

# Keep the oldest row of each identical group (ctid is stable within the
# statement and needs no surrogate ordering column). Every column the unique
# index keys on is equal across the group, so which one survives is
# immaterial — only the count changes.
DEDUPE_SQL = f"""
    DELETE FROM price_history a
    USING price_history b
    WHERE a.ctid > b.ctid
      AND a.property_id = b.property_id
      AND a.source = b.source
      AND a.event = b.event
      AND a.event_date = b.event_date
      AND COALESCE(a.price, -1) = COALESCE(b.price, -1)
      AND COALESCE(a.source_event_id, '') = COALESCE(b.source_event_id, '')
"""

# (name, type) for the plain nullable columns added to `properties`.
PROPERTY_COLUMNS = (
    # --- economics, promoted out of meta ---
    ("avm_value", sa.Float()),
    ("rental_est_low", sa.Integer()),
    ("rental_est_mid", sa.Integer()),
    ("rental_est_high", sa.Integer()),
    ("hoa_monthly", sa.Float()),
    ("price_per_sqft", sa.Float()),
    ("tax_annual", sa.Float()),
    # --- climate risk: flood and wind decide Florida insurability ---
    ("flood_factor", sa.Integer()),
    ("fire_factor", sa.Integer()),
    ("heat_factor", sa.Integer()),
    ("wind_factor", sa.Integer()),
    # --- walkability ---
    ("walk_score", sa.Float()),
    ("transit_score", sa.Float()),
    ("bike_score", sa.Float()),
    # --- condition ---
    ("year_renovated", sa.Integer()),
    ("stories", sa.Float()),
    # --- seller distress / use restrictions (NULL = no listing text) ---
    ("is_foreclosure", sa.Boolean()),
    ("is_auction", sa.Boolean()),
    ("auction_date", sa.DateTime()),
    ("is_short_sale", sa.Boolean()),
    ("is_reo", sa.Boolean()),
    ("is_probate_or_estate", sa.Boolean()),
    ("is_as_is", sa.Boolean()),
    ("is_tenant_occupied", sa.Boolean()),
    ("is_vacant", sa.Boolean()),
    ("is_cash_only", sa.Boolean()),
    ("is_age_restricted", sa.Boolean()),
    ("is_rental_restricted", sa.Boolean()),
    ("allows_short_term_rental", sa.Boolean()),
    ("distress_signals", sa.JSON()),
    # --- ingestion-level data quality ---
    ("price_is_placeholder", sa.Boolean()),
    ("data_quality_flags", sa.JSON()),
)


def upgrade() -> None:
    """Upgrade migration."""
    for name, column_type in PROPERTY_COLUMNS:
        op.add_column("properties", sa.Column(name, column_type, nullable=True))

    op.add_column("tax_history", sa.Column("land_value", sa.Integer(), nullable=True))
    op.add_column("tax_history", sa.Column("improvement_value", sa.Integer(), nullable=True))

    # NOT NULL with a server default: existing rows become False, which is the
    # correct reading for the 94% of history written before rental tagging
    # existed *except* for explicitly rental-typed events. The backfill
    # corrects those; this keeps the column honest for anything it misses,
    # since a NULL here would silently drop rows from `WHERE NOT
    # is_rental_event` filters.
    op.add_column(
        "price_history",
        sa.Column("is_rental_event", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Partial indexes — the distress cohorts are a tiny slice of the table
    # (44 auctions in 6,854 rows) and are only ever queried for the True side.
    op.create_index(
        "idx_properties_foreclosure", "properties", ["state", "is_foreclosure"],
        unique=False, postgresql_where=sa.text("is_foreclosure"),
    )
    op.create_index(
        "idx_properties_auction", "properties", ["state", "auction_date"],
        unique=False, postgresql_where=sa.text("is_auction"),
    )
    op.create_index(
        "idx_properties_placeholder_price", "properties", ["price_is_placeholder"],
        unique=False, postgresql_where=sa.text("price_is_placeholder"),
    )
    op.create_index(
        "idx_price_history_sale_events", "price_history", ["property_id", "event_type"],
        unique=False, postgresql_where=sa.text("NOT is_rental_event"),
    )

    _normalize_state_casing()
    _dedupe_and_guard_price_history()


def _normalize_state_casing() -> None:
    """Uppercase the handful of rows stored as 'Fl' / 'fl'."""
    result = op.get_bind().execute(
        sa.text("UPDATE properties SET state = upper(state) WHERE state <> upper(state)")
    )
    if result.rowcount:
        logger.info("Uppercased `state` on %s row(s).", result.rowcount)


def _dedupe_and_guard_price_history() -> None:
    """
    Remove exact duplicate events, then add the uniqueness guard.

    The previous migration attempted this index and backed off cleanly
    because pre-existing duplicates blocked it, leaving uniqueness enforced
    only in Python. Now that the duplicates are provably identical on every
    keyed column, they can be collapsed and the guard put in place.
    """
    bind = op.get_bind()

    deleted = bind.execute(sa.text(DEDUPE_SQL)).rowcount
    logger.info("Removed %s duplicate price_history row(s).", deleted)

    remaining = bind.execute(sa.text(f"""
        SELECT count(*) FROM (
            SELECT {UNIQUE_INDEX_COLUMNS} FROM price_history
            GROUP BY 1, 2, 3, 4, 5, 6 HAVING count(*) > 1
        ) duplicated
    """)).scalar() or 0
    if remaining:
        # Should be unreachable — the delete above covers exactly the same
        # key. Refuse to guess rather than delete something unexpected.
        logger.warning(
            "Skipping %s: %s duplicate group(s) survived the cleanup. Uniqueness "
            "stays enforced in PipelineRunner._upsert_price_history only.",
            UNIQUE_INDEX_NAME, remaining,
        )
        return

    # Inside a SAVEPOINT: a failed statement aborts the whole Postgres
    # transaction, which would take every column added above with it and make
    # the migration look like it had never run.
    try:
        with bind.begin_nested():
            bind.execute(sa.text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INDEX_NAME} "
                f"ON price_history ({UNIQUE_INDEX_COLUMNS})"
            ))
        logger.info("Created %s.", UNIQUE_INDEX_NAME)
    except Exception as exc:  # noqa: BLE001 - never fail the migration over the guard
        logger.warning(
            "Could not create %s (%s: %s). Uniqueness stays enforced in "
            "PipelineRunner._upsert_price_history.",
            UNIQUE_INDEX_NAME, type(exc).__name__, exc,
        )


def downgrade() -> None:
    """Downgrade migration.

    Deleted duplicate rows and uppercased state values are NOT restored —
    both are cleanups of provably wrong data, and re-introducing them would
    be the actual data loss. Restore from a dump if that's genuinely wanted.
    """
    op.execute(sa.text(f"DROP INDEX IF EXISTS {UNIQUE_INDEX_NAME}"))

    op.drop_index("idx_price_history_sale_events", table_name="price_history")
    op.drop_index("idx_properties_placeholder_price", table_name="properties")
    op.drop_index("idx_properties_auction", table_name="properties")
    op.drop_index("idx_properties_foreclosure", table_name="properties")

    op.drop_column("price_history", "is_rental_event")
    op.drop_column("tax_history", "improvement_value")
    op.drop_column("tax_history", "land_value")

    for name, _ in reversed(PROPERTY_COLUMNS):
        op.drop_column("properties", name)
