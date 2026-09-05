"""Recovery agent: agent_runs and approvals.

Revision ID: 0019_agent
Revises: 0018_compliance
Create Date: 2026-09-05 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import GUID, JSON_TYPE

revision: str = '0019_agent'
down_revision: Union[str, None] = '0018_compliance'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if 'agent_runs' not in tables:
        op.create_table(
            'agent_runs',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('recovery_case_id', GUID(), nullable=False),
            sa.Column('provider', sa.String(length=30), nullable=False),
            sa.Column('model', sa.String(length=100), nullable=False),
            sa.Column('prompt_version', sa.String(length=60), server_default='n/a', nullable=False),
            sa.Column('status', sa.String(length=30), server_default='RUNNING', nullable=False),
            sa.Column('dry_run', sa.Boolean(), server_default=sa.text('true'), nullable=False),
            sa.Column('degraded_to_rules', sa.Boolean(), server_default=sa.text('false'), nullable=False),
            sa.Column('dossier_hash', sa.String(length=32), nullable=True),
            sa.Column('dossier', JSON_TYPE, nullable=True),
            sa.Column('turns', sa.Integer(), server_default='0', nullable=False),
            sa.Column('trace', JSON_TYPE, server_default='[]', nullable=False),
            sa.Column('final_plan', JSON_TYPE, nullable=True),
            sa.Column('validation', JSON_TYPE, nullable=True),
            sa.Column('execution_result', JSON_TYPE, nullable=True),
            sa.Column('approval_id', GUID(), nullable=True),
            sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
            sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
            sa.Column('cost_usd', sa.Float(), server_default='0', nullable=False),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        for col in ('recovery_case_id', 'status', 'started_at'):
            op.create_index(f'ix_agent_runs_{col}', 'agent_runs', [col])
    if 'approvals' not in tables:
        op.create_table(
            'approvals',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('recovery_case_id', GUID(), nullable=False),
            sa.Column('agent_run_id', GUID(), nullable=True),
            sa.Column('action', sa.String(length=40), nullable=False),
            sa.Column('payload', JSON_TYPE, server_default='{}', nullable=False),
            sa.Column('reason', sa.Text(), nullable=False),
            sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
            sa.Column('requested_by', sa.String(length=60), server_default='recovery_agent_v1', nullable=False),
            sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('decided_by', sa.String(length=120), nullable=True),
            sa.Column('decision_note', sa.Text(), nullable=True),
            sa.Column('execution_result', JSON_TYPE, nullable=True),
            sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        for col in ('recovery_case_id', 'agent_run_id', 'action', 'status', 'requested_at'):
            op.create_index(f'ix_approvals_{col}', 'approvals', [col])


def downgrade() -> None:
    op.drop_table('approvals')
    op.drop_table('agent_runs')
