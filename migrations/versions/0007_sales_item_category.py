"""Item category on sales lines, so a rate can apply to some of a brand's categories (e.g. EUFY Security)

Revision: 0007
Previous: 0006
Created: 2026-10-12
"""

from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('sales_line', sa.Column('item_category', sa.String(length=100), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('sales_line', 'item_category')
