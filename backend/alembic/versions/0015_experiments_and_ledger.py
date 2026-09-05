"""Add experiment assignments, recovery ledger, and case surface/arm/batch columns.

Revision ID: 0015_experiments_and_ledger
Revises: 0014_payment_gateway_fields
Create Date: 2026-09-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import GUID, JSON_TYPE

revision: str = '0015_experiments_and_ledger'
down_revision: Union[str, None] = '0014_payment_gateway_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. recovery_cases: experiment arm, batch id, leak surface (idempotent)
    case_columns = {c['name'] for c in inspector.get_columns('recovery_cases')}
    case_indexes = {i['name'] for i in inspector.get_indexes('recovery_cases')}
    if 'experiment_arm' not in case_columns:
        op.add_column('recovery_cases', sa.Column('experiment_arm', sa.String(length=20), nullable=True))
    if 'batch_id' not in case_columns:
        op.add_column('recovery_cases', sa.Column('batch_id', sa.String(length=100), nullable=True))
    if 'leak_surface' not in case_columns:
        op.add_column('recovery_cases', sa.Column('leak_surface', sa.String(length=50), nullable=True))
    if 'ix_recovery_cases_experiment_arm' not in case_indexes:
        op.create_index('ix_recovery_cases_experiment_arm', 'recovery_cases', ['experiment_arm'])
    if 'ix_recovery_cases_batch_id' not in case_indexes:
        op.create_index('ix_recovery_cases_batch_id', 'recovery_cases', ['batch_id'])
    if 'ix_recovery_cases_leak_surface' not in case_indexes:
        op.create_index('ix_recovery_cases_leak_surface', 'recovery_cases', ['leak_surface'])

    # 2. experiment_assignments
    if 'experiment_assignments' not in tables:
        op.create_table(
            'experiment_assignments',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('recovery_case_id', GUID(), nullable=False),
            sa.Column('experiment_key', sa.String(length=100), nullable=False),
            sa.Column('arm', sa.String(length=20), nullable=False),
            sa.Column('bucket', sa.Integer(), nullable=False),
            sa.Column('holdout_bps', sa.Integer(), nullable=False),
            sa.Column('salt_version', sa.String(length=100), nullable=False),
            sa.Column('leak_surface', sa.String(length=50), server_default='PAYMENT_FAILURE', nullable=False),
            sa.Column('metadata', JSON_TYPE, server_default='{}', nullable=False),
            sa.Column('assigned_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('recovery_case_id', 'experiment_key', name='uq_experiment_assignment_case_key'),
        )
        op.create_index('ix_experiment_assignments_recovery_case_id', 'experiment_assignments', ['recovery_case_id'])
        op.create_index('ix_experiment_assignments_experiment_key', 'experiment_assignments', ['experiment_key'])
        op.create_index('ix_experiment_assignments_arm', 'experiment_assignments', ['arm'])
        op.create_index('ix_experiment_assignments_assigned_at', 'experiment_assignments', ['assigned_at'])

    # 3. ledger_entries
    if 'ledger_entries' not in tables:
        op.create_table(
            'ledger_entries',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('recovery_case_id', GUID(), nullable=False),
            sa.Column('entry_type', sa.String(length=40), nullable=False),
            sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
            sa.Column('currency', sa.String(length=3), server_default='INR', nullable=False),
            sa.Column('channel', sa.String(length=30), nullable=True),
            sa.Column('provider_reference', sa.String(length=255), nullable=False),
            sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('metadata', JSON_TYPE, server_default='{}', nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('recovery_case_id', 'entry_type', 'provider_reference', name='uq_ledger_case_type_reference'),
        )
        op.create_index('ix_ledger_entries_recovery_case_id', 'ledger_entries', ['recovery_case_id'])
        op.create_index('ix_ledger_entries_entry_type', 'ledger_entries', ['entry_type'])
        op.create_index('ix_ledger_entries_channel', 'ledger_entries', ['channel'])
        op.create_index('ix_ledger_entries_provider_reference', 'ledger_entries', ['provider_reference'])
        op.create_index('ix_ledger_entries_occurred_at', 'ledger_entries', ['occurred_at'])


def downgrade() -> None:
    op.drop_table('ledger_entries')
    op.drop_table('experiment_assignments')
    op.drop_index('ix_recovery_cases_leak_surface', table_name='recovery_cases')
    op.drop_index('ix_recovery_cases_batch_id', table_name='recovery_cases')
    op.drop_index('ix_recovery_cases_experiment_arm', table_name='recovery_cases')
    op.drop_column('recovery_cases', 'leak_surface')
    op.drop_column('recovery_cases', 'batch_id')
    op.drop_column('recovery_cases', 'experiment_arm')
