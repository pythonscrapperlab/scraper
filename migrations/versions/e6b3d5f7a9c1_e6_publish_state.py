"""Add publisher change-tracking state (E6 step 4)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e6b3d5f7a9c1"
down_revision = "9c2e7f4a1b63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publish_state",
        sa.Column("market_slug", sa.String(160), nullable=False),
        sa.Column("property_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("scores_hash", sa.String(64), nullable=False),
        sa.Column("children_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("market_slug", "property_id"),
    )
    op.create_table(
        "publish_market_state",
        sa.Column("market_slug", sa.String(160), primary_key=True),
        sa.Column("events_watermark", sa.DateTime(), nullable=True),
        sa.Column("last_push_at", sa.DateTime(), nullable=True),
        sa.Column("market_hash", sa.String(64), nullable=True),
        sa.Column("daily_hash", sa.String(64), nullable=True),
        sa.Column(
            "snapshot_hashes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_table("publish_market_state")
    op.drop_table("publish_state")
