"""Add local two-tier freshness state and event queue."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9c2e7f4a1b63"
down_revision = "ebe84906b119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create E2 local state without changing scoring inputs or outputs."""
    op.add_column("properties", sa.Column("refreshed_at", sa.DateTime(), nullable=True))
    op.add_column("properties", sa.Column("delisted_at", sa.DateTime(), nullable=True))

    op.create_table(
        "market_freshness",
        sa.Column("slug", sa.String(160), primary_key=True),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("state", sa.String(2), nullable=False),
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("next_check_at", sa.DateTime(), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("check_status", sa.String(20), nullable=False),
        sa.Column("listings_active", sa.Integer(), nullable=False),
        sa.Column("changed_last_check", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "check_status in ('ok','late','failed','warming')",
            name="ck_market_freshness_status",
        ),
    )
    op.create_table(
        "listing_presence",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("market_slug", sa.String(160), nullable=False),
        sa.Column("redfin_id", sa.String(255), nullable=False),
        sa.Column("property_id", sa.UUID(), nullable=True),
        sa.Column("listing_url", sa.String(2048), nullable=False),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("listing_status", sa.String(80), nullable=True),
        sa.Column("dom", sa.Integer(), nullable=True),
        sa.Column("listed_at", sa.DateTime(), nullable=True),
        sa.Column("absence_count", sa.Integer(), nullable=False),
        sa.Column("is_delisted", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["market_slug"], ["market_freshness.slug"]),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"]),
        sa.UniqueConstraint(
            "market_slug", "redfin_id", name="uq_listing_presence_market_redfin"
        ),
    )
    op.create_index(
        "idx_listing_presence_market_absence",
        "listing_presence",
        ["market_slug", "absence_count"],
    )
    op.create_table(
        "pending_refresh",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("market_slug", sa.String(160), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("redfin_id", sa.String(255), nullable=False),
        sa.Column("property_id", sa.UUID(), nullable=True),
        sa.Column("listing_url", sa.String(2048), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_error_class", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["market_slug"], ["market_freshness.slug"]),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"]),
        sa.UniqueConstraint(
            "market_slug", "redfin_id", name="uq_pending_refresh_market_redfin"
        ),
        sa.CheckConstraint(
            "status in ('pending','processing','succeeded','failed')",
            name="ck_pending_refresh_status",
        ),
    )
    op.create_index(
        "idx_pending_refresh_due", "pending_refresh", ["status", "next_attempt_at"]
    )
    op.create_table(
        "change_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("property_id", sa.UUID(), nullable=True),
        sa.Column("market_slug", sa.String(160), nullable=False),
        sa.Column("redfin_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"]),
        sa.ForeignKeyConstraint(["market_slug"], ["market_freshness.slug"]),
        sa.CheckConstraint(
            "kind in ('new','price_cut','price_increase','status','relisted',"
            "'delisted','tier_up','tier_down','score_move')",
            name="ck_change_events_kind",
        ),
    )
    op.create_index(
        "idx_change_events_market_observed",
        "change_events",
        ["market_slug", "observed_at"],
    )
    op.create_index("idx_change_events_property", "change_events", ["property_id"])
    op.create_table(
        "analysis_tiers",
        sa.Column("property_id", sa.UUID(), nullable=False),
        sa.Column("lens", sa.String(30), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("percentile", sa.Float(), nullable=True),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"]),
        sa.PrimaryKeyConstraint("property_id", "lens"),
        sa.CheckConstraint(
            "tier in ('top','strong','rest')", name="ck_analysis_tiers_tier"
        ),
    )


def downgrade() -> None:
    """Remove E2 local state."""
    op.drop_table("analysis_tiers")
    op.drop_index("idx_change_events_property", table_name="change_events")
    op.drop_index("idx_change_events_market_observed", table_name="change_events")
    op.drop_table("change_events")
    op.drop_index("idx_pending_refresh_due", table_name="pending_refresh")
    op.drop_table("pending_refresh")
    op.drop_index("idx_listing_presence_market_absence", table_name="listing_presence")
    op.drop_table("listing_presence")
    op.drop_table("market_freshness")
    op.drop_column("properties", "delisted_at")
    op.drop_column("properties", "refreshed_at")
