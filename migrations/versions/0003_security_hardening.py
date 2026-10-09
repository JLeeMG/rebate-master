"""Security hardening: revocable sessions, Microsoft object ids, audit addresses, sign-in throttling,
feed token expiry, and no emptying of the protected tables

Revision: 0003
Previous: 0002
Created: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

# Tables whose rows the database already protects row by row; TRUNCATE would bypass that.
PROTECTED_TABLES = ('audit_event', 'evidence_file', 'rebate_rate', 'rebate_change_request')
FEED_TOKEN_LIFETIME = "interval '1 year'"


def upgrade() -> None:
    op.add_column('app_user', sa.Column('session_version', sa.Integer(), server_default='0', nullable=False))
    op.add_column('app_user', sa.Column('entra_object_id', sa.String(length=64), nullable=True))
    op.create_unique_constraint(op.f('uq_app_user_entra_object_id'), 'app_user', ['entra_object_id'])
    op.add_column('audit_event', sa.Column('address', sa.String(length=45), nullable=True))
    op.add_column('api_token', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.execute(f"UPDATE api_token SET expires_at = now() + {FEED_TOKEN_LIFETIME} WHERE revoked_at IS NULL")
    op.create_table('failed_sign_in',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('address', sa.String(length=45), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_failed_sign_in'))
    )
    op.create_index(op.f('ix_failed_sign_in_email'), 'failed_sign_in', ['email'], unique=False)
    op.create_index(op.f('ix_failed_sign_in_address'), 'failed_sign_in', ['address'], unique=False)

    op.execute(FORBID_TRUNCATE)
    for table in PROTECTED_TABLES:
        op.execute(f"CREATE TRIGGER {table}_not_emptied BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION mgrm_forbid_truncate()")


FORBID_TRUNCATE = """
CREATE FUNCTION mgrm_forbid_truncate() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
    RAISE EXCEPTION '% is part of the permanent record and cannot be emptied.', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$body$
"""


def downgrade() -> None:
    for table in PROTECTED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_not_emptied ON {table}")
    op.execute("DROP FUNCTION IF EXISTS mgrm_forbid_truncate()")
    op.drop_index(op.f('ix_failed_sign_in_address'), table_name='failed_sign_in')
    op.drop_index(op.f('ix_failed_sign_in_email'), table_name='failed_sign_in')
    op.drop_table('failed_sign_in')
    op.drop_column('api_token', 'expires_at')
    op.drop_column('audit_event', 'address')
    op.drop_constraint(op.f('uq_app_user_entra_object_id'), 'app_user', type_='unique')
    op.drop_column('app_user', 'entra_object_id')
    op.drop_column('app_user', 'session_version')
