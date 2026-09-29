"""Add the local CLI run ledger and additive score breakdown outputs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "ebe84906b119"
down_revision = "f2a9c8d14e73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade migration."""
    op.create_table(
        "runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_class", sa.String(length=255), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_kind", "runs", ["kind"], unique=False)
    op.create_index("ix_runs_status", "runs", ["status"], unique=False)
    op.create_index(
        "idx_runs_kind_started", "runs", ["kind", "started_at"], unique=False
    )

    for lens in ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb"):
        op.add_column(
            "property_analysis",
            sa.Column(
                f"{lens}_breakdown",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    """Downgrade migration."""
    for lens in reversed(
        ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")
    ):
        op.drop_column("property_analysis", f"{lens}_breakdown")

    op.drop_index("idx_runs_kind_started", table_name="runs")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_kind", table_name="runs")
    op.drop_table("runs")
