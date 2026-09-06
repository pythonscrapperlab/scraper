"""Add properties.days_on_market_mls (cumulative MLS days on market).

Pairs a model change that was added without a migration.

WHY IT MATTERS: `days_on_market` now carries Redfin's `timeOnRedfin` — how
long the listing has been visible on Redfin. `days_on_market_mls` is the MLS's
`cumulativeDaysOnMarket`, which survives a relist and therefore measures total
market exposure including earlier failed attempts to sell.

For motivated-seller scoring the cumulative figure is the better signal: a
property relisted three times over 400 days reads as fresh under
`timeOnRedfin` and as thoroughly stale under the MLS count. Measured on the
overlap available today, the MLS figure is higher on 45 of 195 properties and
more than 50% higher on 27 of them.

Coverage is currently thin (~2.7% of properties, 83% of the newest payloads)
because the scraper only recently began capturing it, so scoring treats it as
a preferred-but-optional input and falls back to `days_on_market`.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5b207c9d418'
down_revision = 'd3f8a1c47b92'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade migration."""
    op.add_column("properties", sa.Column("days_on_market_mls", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade migration."""
    op.drop_column("properties", "days_on_market_mls")
