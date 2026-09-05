"""Add voice_calls table for Retell voice recovery.

Revision ID: 0013_add_voice_calls_table
Revises: 0012_extend_promise_to_pay_fields
Create Date: 2026-08-23 09:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from app.db.base import GUID, JSON_TYPE

# revision identifiers, used by Alembic.
revision: str = '0013_add_voice_calls_table'
down_revision: Union[str, None] = '0012_extend_ptp_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'voice_calls',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_case_id', GUID(), nullable=False),
        sa.Column('customer_id', GUID(), nullable=True),
        sa.Column('provider', sa.String(length=50), server_default='RETELL', nullable=False),
        sa.Column('provider_call_id', sa.String(length=255), nullable=True),
        sa.Column('from_number', sa.String(length=50), nullable=False),
        sa.Column('to_number', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='QUEUED', nullable=False),
        sa.Column('attempt_number', sa.Integer(), server_default='1', nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('outcome', sa.String(length=100), nullable=True),
        sa.Column('dynamic_variables', JSON_TYPE, nullable=False),
        sa.Column('call_metadata', JSON_TYPE, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_voice_calls_customer_id'), 'voice_calls', ['customer_id'], unique=False)
    op.create_index(op.f('ix_voice_calls_provider_call_id'), 'voice_calls', ['provider_call_id'], unique=False)
    op.create_index(op.f('ix_voice_calls_recovery_case_id'), 'voice_calls', ['recovery_case_id'], unique=False)
    op.create_index(op.f('ix_voice_calls_status'), 'voice_calls', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_voice_calls_status'), table_name='voice_calls')
    op.drop_index(op.f('ix_voice_calls_recovery_case_id'), table_name='voice_calls')
    op.drop_index(op.f('ix_voice_calls_provider_call_id'), table_name='voice_calls')
    op.drop_index(op.f('ix_voice_calls_customer_id'), table_name='voice_calls')
    op.drop_table('voice_calls')
