"""The monthly accrual journal: agreement scope, NetSuite sales, and the journal with four-eyes

Revision: 0004
Previous: 0003
Created: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

LOAD_KINDS = ('customers', 'classes', 'trading_detail', 'rebate_workbook', 'forecasting_import')
PREVIOUS_LOAD_KINDS = ('customers', 'classes', 'rebate_workbook', 'forecasting_import')
STATUSES = ('proposed', 'approved', 'rejected', 'withdrawn')


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column('rebate_agreement', sa.Column('scope', sa.Text(), nullable=True))
    op.drop_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', type_='check')
    op.create_check_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', _in('kind', LOAD_KINDS))

    op.create_table('sales_line',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=False),
    sa.Column('entity_code', sa.String(length=8), nullable=False),
    sa.Column('period', sa.Date(), nullable=False),
    sa.Column('account_code', sa.String(length=16), nullable=False),
    sa.Column('netsuite_class_id', sa.Integer(), nullable=True),
    sa.Column('class_name', sa.String(length=200), server_default='', nullable=False),
    sa.Column('netsuite_customer_id', sa.Integer(), nullable=True),
    sa.Column('customer_name', sa.String(length=300), server_default='', nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=2), nullable=False),
    sa.CheckConstraint('extract(day from period) = 1', name=op.f('ck_sales_line_period_is_first_of_month')),
    sa.ForeignKeyConstraint(['batch_id'], ['load_batch.id'], name=op.f('fk_sales_line_batch_id_load_batch')),
    sa.ForeignKeyConstraint(['entity_code'], ['entity.code'], name=op.f('fk_sales_line_entity_code_entity')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sales_line'))
    )
    op.create_index(op.f('ix_sales_line_period'), 'sales_line', ['period'], unique=False)

    op.create_table('rebate_journal',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_code', sa.String(length=8), nullable=False),
    sa.Column('period', sa.Date(), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum(*STATUSES, name='review_status', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('total', sa.Numeric(precision=20, scale=2), nullable=False),
    sa.Column('lines', sa.JSON(), nullable=False),
    sa.Column('schedule', sa.JSON(), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.Column('sales_batch_ids', sa.JSON(), nullable=False),
    sa.Column('file_sha256', sa.String(length=64), nullable=False),
    sa.Column('entered_by_id', sa.Integer(), nullable=False),
    sa.Column('entered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reviewed_by_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('review_note', sa.Text(), server_default='', nullable=False),
    sa.CheckConstraint(_in('status', STATUSES), name=op.f('ck_rebate_journal_review_status')),
    sa.CheckConstraint('extract(day from period) = 1', name=op.f('ck_rebate_journal_period_is_first_of_month')),
    sa.CheckConstraint('reviewed_by_id IS NULL OR reviewed_by_id <> entered_by_id', name=op.f('ck_rebate_journal_four_eyes')),
    sa.CheckConstraint("status IN ('proposed', 'withdrawn') OR (reviewed_by_id IS NOT NULL AND reviewed_at IS NOT NULL)",
                       name=op.f('ck_rebate_journal_decision_records_reviewer')),
    sa.ForeignKeyConstraint(['entity_code'], ['entity.code'], name=op.f('fk_rebate_journal_entity_code_entity')),
    sa.ForeignKeyConstraint(['entered_by_id'], ['app_user.id'], name=op.f('fk_rebate_journal_entered_by_id_app_user')),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['app_user.id'], name=op.f('fk_rebate_journal_reviewed_by_id_app_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rebate_journal'))
    )
    # One live journal per entity and month: a new one only after the last was rejected or withdrawn.
    op.create_index('uq_rebate_journal_live', 'rebate_journal', ['entity_code', 'period'], unique=True,
                    postgresql_where=sa.text("status IN ('proposed', 'approved')"))
    op.execute(JOURNAL_IS_HISTORY)
    op.execute("CREATE TRIGGER rebate_journal_history BEFORE UPDATE OR DELETE ON rebate_journal "
               "FOR EACH ROW EXECUTE FUNCTION mgrm_guard_decided_journal()")
    op.execute("CREATE TRIGGER rebate_journal_not_emptied BEFORE TRUNCATE ON rebate_journal "
               "FOR EACH STATEMENT EXECUTE FUNCTION mgrm_forbid_truncate()")


JOURNAL_IS_HISTORY = """
CREATE FUNCTION mgrm_guard_decided_journal() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status <> 'proposed' THEN
        RAISE EXCEPTION 'A journal that has been decided or withdrawn is part of the history and cannot be changed or deleted.'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.entity_code, NEW.period, NEW.external_id, NEW.total, NEW.lines::text, NEW.schedule::text, NEW.file_sha256,
        NEW.entered_by_id, NEW.entered_at)
       IS DISTINCT FROM (OLD.entity_code, OLD.period, OLD.external_id, OLD.total, OLD.lines::text, OLD.schedule::text,
                         OLD.file_sha256, OLD.entered_by_id, OLD.entered_at) THEN
        RAISE EXCEPTION 'A prepared journal cannot be edited. Withdraw it and prepare it again.'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$body$
"""


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS rebate_journal_not_emptied ON rebate_journal")
    op.execute("DROP TRIGGER IF EXISTS rebate_journal_history ON rebate_journal")
    op.execute("DROP FUNCTION IF EXISTS mgrm_guard_decided_journal()")
    op.drop_index('uq_rebate_journal_live', table_name='rebate_journal')
    op.drop_table('rebate_journal')
    op.drop_index(op.f('ix_sales_line_period'), table_name='sales_line')
    op.drop_table('sales_line')
    op.drop_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', type_='check')
    op.create_check_constraint(op.f('ck_load_batch_load_kind'), 'load_batch', _in('kind', PREVIOUS_LOAD_KINDS))
    op.drop_column('rebate_agreement', 'scope')
