"""Alembic script template for migrations."""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade migration."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade migration."""
    ${downgrades if downgrades else "pass"}
