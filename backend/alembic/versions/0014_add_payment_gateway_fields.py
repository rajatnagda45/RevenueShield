"""Add razorpay and gateway metadata fields to payments table with idempotent inspection.

Revision ID: 0014_payment_gateway_fields
Revises: 0013_add_voice_calls_table
Create Date: 2026-08-27 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from app.db.base import JSON_TYPE

# revision identifiers, used by Alembic.
revision: str = '0014_payment_gateway_fields'
down_revision: Union[str, None] = '0013_add_voice_calls_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_columns = {c['name'] for c in inspector.get_columns('payments')}
    existing_indexes = {i['name'] for i in inspector.get_indexes('payments')}

    def add_column_if_missing(name: str, column_def: sa.Column) -> None:
        if name not in existing_columns:
            op.add_column('payments', column_def)

    # Safely add each column only if it does not already exist
    add_column_if_missing('razorpay_payment_id', sa.Column('razorpay_payment_id', sa.String(length=255), nullable=True))
    add_column_if_missing('razorpay_order_id', sa.Column('razorpay_order_id', sa.String(length=255), nullable=True))
    add_column_if_missing('bank', sa.Column('bank', sa.String(length=100), nullable=True))
    add_column_if_missing('wallet', sa.Column('wallet', sa.String(length=100), nullable=True))
    add_column_if_missing('vpa', sa.Column('vpa', sa.String(length=255), nullable=True))
    add_column_if_missing('international', sa.Column('international', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    add_column_if_missing('captured', sa.Column('captured', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    add_column_if_missing('amount_refunded', sa.Column('amount_refunded', sa.Numeric(precision=12, scale=2), server_default=sa.text('0.00'), nullable=False))
    add_column_if_missing('refund_status', sa.Column('refund_status', sa.String(length=50), nullable=True))
    add_column_if_missing('description', sa.Column('description', sa.String(length=500), nullable=True))
    add_column_if_missing('error_source', sa.Column('error_source', sa.String(length=100), nullable=True))
    add_column_if_missing('error_step', sa.Column('error_step', sa.String(length=100), nullable=True))
    add_column_if_missing('error_reason', sa.Column('error_reason', sa.String(length=100), nullable=True))
    add_column_if_missing('razorpay_created_at', sa.Column('razorpay_created_at', sa.DateTime(timezone=True), nullable=True))
    add_column_if_missing('raw_payload', sa.Column('raw_payload', JSON_TYPE, nullable=True))

    if 'ix_payments_razorpay_payment_id' not in existing_indexes:
        op.create_index('ix_payments_razorpay_payment_id', 'payments', ['razorpay_payment_id'])
    if 'ix_payments_razorpay_order_id' not in existing_indexes:
        op.create_index('ix_payments_razorpay_order_id', 'payments', ['razorpay_order_id'])


def downgrade() -> None:
    pass
