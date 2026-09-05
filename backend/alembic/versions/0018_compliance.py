"""Compliance v2: consent ledger, contact registry, hash-chained audit log columns.

Revision ID: 0018_compliance
Revises: 0017_degradation
Create Date: 2026-09-05 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import GUID, JSON_TYPE

revision: str = '0018_compliance'
down_revision: Union[str, None] = '0017_degradation'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. audit_logs: hash chain columns (legacy rows keep NULLs and are treated as pre-chain)
    audit_cols = {c['name'] for c in inspector.get_columns('audit_logs')}
    if 'sequence' not in audit_cols:
        op.add_column('audit_logs', sa.Column('sequence', sa.BigInteger(), nullable=True))
    if 'prev_hash' not in audit_cols:
        op.add_column('audit_logs', sa.Column('prev_hash', sa.String(length=64), nullable=True))
    if 'row_hash' not in audit_cols:
        op.add_column('audit_logs', sa.Column('row_hash', sa.String(length=64), nullable=True))
    audit_idx = {i['name'] for i in inspector.get_indexes('audit_logs')}
    if 'uq_audit_logs_sequence' not in audit_idx:
        op.create_index('uq_audit_logs_sequence', 'audit_logs', ['sequence'], unique=True)

    # 2. consent_records
    if 'consent_records' not in tables:
        op.create_table(
            'consent_records',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('customer_id', GUID(), nullable=False),
            sa.Column('channel', sa.String(length=20), nullable=False),
            sa.Column('status', sa.String(length=10), nullable=False),
            sa.Column('source', sa.String(length=40), nullable=False),
            sa.Column('reason', sa.Text(), nullable=True),
            sa.Column('recovery_case_id', GUID(), nullable=True),
            sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('metadata', JSON_TYPE, server_default='{}', nullable=False),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        for col in ('customer_id', 'channel', 'status', 'recovery_case_id', 'recorded_at'):
            op.create_index(f'ix_consent_records_{col}', 'consent_records', [col])

    # 3. contact_attempts
    if 'contact_attempts' not in tables:
        op.create_table(
            'contact_attempts',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('customer_id', GUID(), nullable=False),
            sa.Column('recovery_case_id', GUID(), nullable=True),
            sa.Column('channel', sa.String(length=20), nullable=False),
            sa.Column('outcome', sa.String(length=20), nullable=False),
            sa.Column('blocking_rule', sa.String(length=60), nullable=True),
            sa.Column('reference', sa.String(length=255), nullable=True),
            sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('metadata', JSON_TYPE, server_default='{}', nullable=False),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        for col in ('customer_id', 'recovery_case_id', 'channel', 'outcome', 'blocking_rule', 'occurred_at'):
            op.create_index(f'ix_contact_attempts_{col}', 'contact_attempts', [col])


def downgrade() -> None:
    op.drop_table('contact_attempts')
    op.drop_table('consent_records')
    op.drop_index('uq_audit_logs_sequence', table_name='audit_logs')
    op.drop_column('audit_logs', 'row_hash')
    op.drop_column('audit_logs', 'prev_hash')
    op.drop_column('audit_logs', 'sequence')
