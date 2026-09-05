"""Add leak-surface entities: orders, mandate_retries, case metadata, invoice/subscription fields.

Revision ID: 0016_surfaces
Revises: 0015_experiments_and_ledger
Create Date: 2026-09-05 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import GUID, JSON_TYPE

revision: str = '0016_surfaces'
down_revision: Union[str, None] = '0015_experiments_and_ledger'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_if_missing(inspector, table: str, name: str, column: sa.Column) -> None:
    existing = {c['name'] for c in inspector.get_columns(table)}
    if name not in existing:
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. recovery_cases.case_metadata
    _add_if_missing(inspector, 'recovery_cases', 'case_metadata', sa.Column('case_metadata', JSON_TYPE, nullable=True))

    # 2. invoices: receivables fields
    _add_if_missing(inspector, 'invoices', 'amount_paid', sa.Column('amount_paid', sa.Numeric(12, 2), server_default='0.00', nullable=False))
    _add_if_missing(inspector, 'invoices', 'issued_at', sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True))
    _add_if_missing(inspector, 'invoices', 'source', sa.Column('source', sa.String(length=30), server_default='RAZORPAY', nullable=False))
    _add_if_missing(inspector, 'invoices', 'short_url', sa.Column('short_url', sa.String(length=500), nullable=True))
    _add_if_missing(inspector, 'invoices', 'customer_reference', sa.Column('customer_reference', sa.String(length=255), nullable=True))
    _add_if_missing(inspector, 'invoices', 'notes', sa.Column('notes', JSON_TYPE, nullable=True))

    # 3. subscriptions: mandate fields
    _add_if_missing(inspector, 'subscriptions', 'plan_id', sa.Column('plan_id', sa.String(length=255), nullable=True))
    _add_if_missing(inspector, 'subscriptions', 'charge_at', sa.Column('charge_at', sa.DateTime(timezone=True), nullable=True))
    _add_if_missing(inspector, 'subscriptions', 'auth_attempts', sa.Column('auth_attempts', sa.Integer(), server_default='0', nullable=False))
    _add_if_missing(inspector, 'subscriptions', 'paid_count', sa.Column('paid_count', sa.Integer(), server_default='0', nullable=False))
    _add_if_missing(inspector, 'subscriptions', 'remaining_count', sa.Column('remaining_count', sa.Integer(), nullable=True))
    _add_if_missing(inspector, 'subscriptions', 'mandate_type', sa.Column('mandate_type', sa.String(length=30), nullable=True))
    _add_if_missing(inspector, 'subscriptions', 'payment_method', sa.Column('payment_method', sa.String(length=30), nullable=True))
    _add_if_missing(inspector, 'subscriptions', 'short_url', sa.Column('short_url', sa.String(length=500), nullable=True))
    _add_if_missing(inspector, 'subscriptions', 'notes', sa.Column('notes', JSON_TYPE, nullable=True))

    # 4. orders
    if 'orders' not in tables:
        op.create_table(
            'orders',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('external_order_id', sa.String(length=255), nullable=False),
            sa.Column('customer_id', GUID(), nullable=True),
            sa.Column('amount', sa.Numeric(12, 2), nullable=False),
            sa.Column('amount_paid', sa.Numeric(12, 2), server_default='0.00', nullable=False),
            sa.Column('currency', sa.String(length=3), server_default='INR', nullable=False),
            sa.Column('status', sa.String(length=30), server_default='CREATED', nullable=False),
            sa.Column('receipt', sa.String(length=255), nullable=True),
            sa.Column('notes', JSON_TYPE, server_default='{}', nullable=False),
            sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
            sa.Column('razorpay_created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('last_payment_attempt_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('abandonment_case_id', GUID(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['abandonment_case_id'], ['recovery_cases.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('external_order_id', name='uq_orders_external_order_id'),
        )
        op.create_index('ix_orders_external_order_id', 'orders', ['external_order_id'])
        op.create_index('ix_orders_customer_id', 'orders', ['customer_id'])
        op.create_index('ix_orders_status', 'orders', ['status'])
        op.create_index('ix_orders_razorpay_created_at', 'orders', ['razorpay_created_at'])
        op.create_index('ix_orders_abandonment_case_id', 'orders', ['abandonment_case_id'])

    # 5. mandate_retries
    if 'mandate_retries' not in tables:
        op.create_table(
            'mandate_retries',
            sa.Column('id', GUID(), nullable=False),
            sa.Column('recovery_case_id', GUID(), nullable=False),
            sa.Column('subscription_id', GUID(), nullable=True),
            sa.Column('attempt_number', sa.Integer(), nullable=False),
            sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('notify_by', sa.DateTime(timezone=True), nullable=False),
            sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('status', sa.String(length=30), server_default='SCHEDULED', nullable=False),
            sa.Column('reason', sa.Text(), nullable=True),
            sa.Column('metadata', JSON_TYPE, server_default='{}', nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_mandate_retries_recovery_case_id', 'mandate_retries', ['recovery_case_id'])
        op.create_index('ix_mandate_retries_subscription_id', 'mandate_retries', ['subscription_id'])
        op.create_index('ix_mandate_retries_scheduled_at', 'mandate_retries', ['scheduled_at'])
        op.create_index('ix_mandate_retries_status', 'mandate_retries', ['status'])


def downgrade() -> None:
    op.drop_table('mandate_retries')
    op.drop_table('orders')
    for col in ['plan_id', 'charge_at', 'auth_attempts', 'paid_count', 'remaining_count', 'mandate_type', 'payment_method', 'short_url', 'notes']:
        op.drop_column('subscriptions', col)
    for col in ['amount_paid', 'issued_at', 'source', 'short_url', 'customer_reference', 'notes']:
        op.drop_column('invoices', col)
    op.drop_column('recovery_cases', 'case_metadata')
