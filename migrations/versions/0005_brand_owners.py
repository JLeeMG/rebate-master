"""Brand owners (GPM and PM) for the brand review packs

Revision: 0005
Previous: 0004
Created: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

LOAD_KINDS = ('customers', 'classes', 'trading_detail', 'brand_owners', 'review_return', 'rebate_workbook',
              'forecasting_import')
PREVIOUS_LOAD_KINDS = ('customers', 'classes', 'trading_detail', 'rebate_workbook', 'forecasting_import')
OWNER_COLUMNS = (('gpm_name', 100), ('gpm_email', 254), ('pm_name', 100), ('pm_email', 254))


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    for name, length in OWNER_COLUMNS:
        op.add_column('brand', sa.Column(name, sa.String(length=length), server_default='', nullable=False))
    op.add_column('brand', sa.Column('owner_notes', sa.Text(), server_default='', nullable=False))
    op.drop_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', type_='check')
    op.create_check_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', _in('kind', LOAD_KINDS))


def downgrade() -> None:
    op.drop_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', type_='check')
    op.create_check_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', _in('kind', PREVIOUS_LOAD_KINDS))
    op.drop_column('brand', 'owner_notes')
    for name, _ in reversed(OWNER_COLUMNS):
        op.drop_column('brand', name)
