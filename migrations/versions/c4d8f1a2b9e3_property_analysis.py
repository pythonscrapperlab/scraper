"""Alembic script template for migrations."""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c4d8f1a2b9e3'
down_revision = '308a36e51aca'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade migration."""
    op.add_column(
        'properties',
        sa.Column('needs_analysis', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index(
        'idx_properties_needs_analysis',
        'properties',
        ['needs_analysis'],
        unique=False,
        postgresql_where=sa.text('needs_analysis'),
    )

    op.create_table(
        'property_analysis',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('property_id', sa.UUID(), nullable=False),
        sa.Column('motivated_seller_score', sa.Float(), nullable=True),
        sa.Column('motivated_seller_rationale', sa.Text(), nullable=True),
        sa.Column('motivated_seller_factors', sa.JSON(), nullable=True),
        sa.Column('motivated_seller_flags', sa.JSON(), nullable=True),
        sa.Column('fix_flip_score', sa.Float(), nullable=True),
        sa.Column('fix_flip_rationale', sa.Text(), nullable=True),
        sa.Column('fix_flip_factors', sa.JSON(), nullable=True),
        sa.Column('fix_flip_flags', sa.JSON(), nullable=True),
        sa.Column('buy_hold_score', sa.Float(), nullable=True),
        sa.Column('buy_hold_rationale', sa.Text(), nullable=True),
        sa.Column('buy_hold_factors', sa.JSON(), nullable=True),
        sa.Column('buy_hold_flags', sa.JSON(), nullable=True),
        sa.Column('str_score', sa.Float(), nullable=True),
        sa.Column('str_rationale', sa.Text(), nullable=True),
        sa.Column('str_factors', sa.JSON(), nullable=True),
        sa.Column('str_flags', sa.JSON(), nullable=True),
        sa.Column('scoring_config_version', sa.String(length=20), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_property_analysis_property_id'), 'property_analysis', ['property_id'], unique=True
    )
    op.create_index(
        op.f('ix_property_analysis_motivated_seller_score'), 'property_analysis', ['motivated_seller_score'], unique=False
    )
    op.create_index(
        op.f('ix_property_analysis_fix_flip_score'), 'property_analysis', ['fix_flip_score'], unique=False
    )
    op.create_index(
        op.f('ix_property_analysis_buy_hold_score'), 'property_analysis', ['buy_hold_score'], unique=False
    )
    op.create_index(
        op.f('ix_property_analysis_str_score'), 'property_analysis', ['str_score'], unique=False
    )


def downgrade() -> None:
    """Downgrade migration."""
    op.drop_index(op.f('ix_property_analysis_str_score'), table_name='property_analysis')
    op.drop_index(op.f('ix_property_analysis_buy_hold_score'), table_name='property_analysis')
    op.drop_index(op.f('ix_property_analysis_fix_flip_score'), table_name='property_analysis')
    op.drop_index(op.f('ix_property_analysis_motivated_seller_score'), table_name='property_analysis')
    op.drop_index(op.f('ix_property_analysis_property_id'), table_name='property_analysis')
    op.drop_table('property_analysis')

    op.drop_index('idx_properties_needs_analysis', table_name='properties')
    op.drop_column('properties', 'needs_analysis')
