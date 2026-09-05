"""Add payment ingestion fields and sync_checkpoints table.

Revision ID: 0006_add_sync_checkpoints
Revises: 0005_add_outcomes_learning
Create Date: 2026-08-22 09:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0006_add_sync_checkpoints'
down_revision: Union[str, None] = '0005_add_outcomes_learning'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Alter payments table to add new gateway ingestion fields
    op.add_column('payments', sa.Column('razorpay_order_id', sa.String(length=255), nullable=True))
    op.add_column('payments', sa.Column('bank', sa.String(length=100), nullable=True))
    op.add_column('payments', sa.Column('wallet', sa.String(length=100), nullable=True))
    op.add_column('payments', sa.Column('vpa', sa.String(length=255), nullable=True))
    op.add_column('payments', sa.Column('international', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('payments', sa.Column('captured', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('payments', sa.Column('amount_refunded', sa.Numeric(precision=12, scale=2), server_default='0.00', nullable=False))
    op.add_column('payments', sa.Column('refund_status', sa.String(length=50), nullable=True))
    op.add_column('payments', sa.Column('description', sa.String(length=500), nullable=True))
    op.add_column('payments', sa.Column('error_source', sa.String(length=100), nullable=True))
    op.add_column('payments', sa.Column('error_step', sa.String(length=100), nullable=True))
    op.add_column('payments', sa.Column('error_reason', sa.String(length=100), nullable=True))
    op.add_column('payments', sa.Column('razorpay_created_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('payments', sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True))
    
    op.create_index(op.f('ix_payments_razorpay_order_id'), 'payments', ['razorpay_order_id'], unique=False)

    # 2. Create sync_checkpoints table
    op.create_table(
        'sync_checkpoints',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('source', sa.String(length=50), server_default='RAZORPAY_API', nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('from_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('to_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('records_fetched', sa.Integer(), server_default='0', nullable=False),
        sa.Column('records_created', sa.Integer(), server_default='0', nullable=False),
        sa.Column('records_updated', sa.Integer(), server_default='0', nullable=False),
        sa.Column('status', sa.String(length=50), server_default='RUNNING', nullable=False),
        sa.Column('error_message', sa.String(length=1000), nullable=True),
        sa.Column('sync_metadata', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sync_checkpoints_status'), 'sync_checkpoints', ['status'], unique=False)
    op.create_index(op.f('ix_sync_checkpoints_created_at'), 'sync_checkpoints', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sync_checkpoints_created_at'), table_name='sync_checkpoints')
    op.drop_index(op.f('ix_sync_checkpoints_status'), table_name='sync_checkpoints')
    op.drop_table('sync_checkpoints')

    op.drop_index(op.f('ix_payments_razorpay_order_id'), table_name='payments')
    op.drop_column('payments', 'raw_payload')
    op.drop_column('payments', 'razorpay_created_at')
    op.drop_column('payments', 'error_reason')
    op.drop_column('payments', 'error_step')
    op.drop_column('payments', 'error_source')
    op.drop_column('payments', 'description')
    op.drop_column('payments', 'refund_status')
    op.drop_column('payments', 'amount_refunded')
    op.drop_column('payments', 'captured')
    op.drop_column('payments', 'international')
    op.drop_column('payments', 'vpa')
    op.drop_column('payments', 'wallet')
    op.drop_column('payments', 'bank')
    op.drop_column('payments', 'razorpay_order_id')
