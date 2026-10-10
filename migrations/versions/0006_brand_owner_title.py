"""Brand owners' actual titles (e.g. Product Manager Lead - NZ), shown instead of a generic "GPM"

Revision: 0006
Previous: 0005
Created: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('brand', sa.Column('gpm_title', sa.String(length=100), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('brand', 'gpm_title')
