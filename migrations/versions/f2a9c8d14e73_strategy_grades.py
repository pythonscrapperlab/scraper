"""Add property_analysis.{strategy}_grade letter-grade columns (scoring v3).

WHY: investors filter on "A and B deals", not on a 0-100 float. The grade is
derived from the final confidence-adjusted, gated score by
aevorex/scoring/config.py::letter_grade and written by ScoringRunner alongside
the score, so it is queryable and indexable rather than buried in the factors
JSON.

Pairs with scoring CONFIG_VERSION v3 and VALUATION_VERSION v2. Rows scored
under v2 carry NULL grades until `python main.py analyze --all` is run.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f2a9c8d14e73"
down_revision = "34c81b9e61c7"
branch_labels = None
depends_on = None

STRATEGIES = ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")


def upgrade() -> None:
    """Upgrade migration."""
    for key in STRATEGIES:
        op.add_column(
            "property_analysis",
            sa.Column(f"{key}_grade", sa.String(length=1), nullable=True),
        )
        op.create_index(
            f"ix_property_analysis_{key}_grade",
            "property_analysis",
            [f"{key}_grade"],
        )


def downgrade() -> None:
    """Downgrade migration."""
    for key in STRATEGIES:
        op.drop_index(f"ix_property_analysis_{key}_grade", table_name="property_analysis")
        op.drop_column("property_analysis", f"{key}_grade")
