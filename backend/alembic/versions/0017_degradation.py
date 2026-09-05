"""Add degradation_incidents table.

Revision ID: 0017_degradation
Revises: 0016_surfaces
Create Date: 2026-09-05 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import GUID, JSON_TYPE

revision: str = '0017_degradation'
down_revision: Union[str, None] = '0016_surfaces'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if 'degradation_incidents' in set(inspect(bind).get_table_names()):
        return
    op.create_table(
        'degradation_incidents',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('bank', sa.String(length=100), nullable=False),
        sa.Column('payment_method', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='SUSPECTED', nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recovering_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_evaluated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('window_minutes', sa.Integer(), server_default='15', nullable=False),
        sa.Column('observed_total', sa.Integer(), server_default='0', nullable=False),
        sa.Column('observed_failures', sa.Integer(), server_default='0', nullable=False),
        sa.Column('observed_failure_rate', sa.Float(), server_default='0', nullable=False),
        sa.Column('baseline_failure_rate', sa.Float(), server_default='0', nullable=False),
        sa.Column('z_score', sa.Float(), server_default='0', nullable=False),
        sa.Column('consecutive_detections', sa.Integer(), server_default='1', nullable=False),
        sa.Column('consecutive_clean', sa.Integer(), server_default='0', nullable=False),
        sa.Column('affected_case_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('metadata', JSON_TYPE, server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_degradation_incidents_bank', 'degradation_incidents', ['bank'])
    op.create_index('ix_degradation_incidents_payment_method', 'degradation_incidents', ['payment_method'])
    op.create_index('ix_degradation_incidents_status', 'degradation_incidents', ['status'])
    op.create_index('ix_degradation_incidents_opened_at', 'degradation_incidents', ['opened_at'])


def downgrade() -> None:
    op.drop_table('degradation_incidents')
