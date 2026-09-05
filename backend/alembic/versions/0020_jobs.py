"""Transactional outbox jobs table.

Revision ID: 0020_jobs
Revises: 0019_agent
Create Date: 2026-09-05 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import GUID, JSON_TYPE

revision: str = '0020_jobs'
down_revision: Union[str, None] = '0019_agent'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'jobs' in set(inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        'jobs',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('kind', sa.String(length=60), nullable=False),
        sa.Column('payload', JSON_TYPE, server_default='{}', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='QUEUED', nullable=False),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('max_attempts', sa.Integer(), server_default='5', nullable=False),
        sa.Column('idempotency_key', sa.String(length=200), nullable=True),
        sa.Column('locked_by', sa.String(length=100), nullable=True),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('result', JSON_TYPE, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key', name='uq_jobs_idempotency_key'),
    )
    for col in ('kind', 'status', 'run_at', 'created_at'):
        op.create_index(f'ix_jobs_{col}', 'jobs', [col])


def downgrade() -> None:
    op.drop_table('jobs')
