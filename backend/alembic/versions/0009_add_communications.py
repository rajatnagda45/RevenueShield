"""Add communications table and customer communication preference columns.

Revision ID: 0009_add_communications
Revises: 0008_add_interventions_and_payment_links
Create Date: 2026-08-22 10:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.db.base import GUID, JSON_TYPE

# revision identifiers, used by Alembic.
revision: str = '0009_add_communications'
down_revision: Union[str, None] = '0008_interventions_pay_links'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add preference columns to customers table
    op.add_column('customers', sa.Column('whatsapp_allowed', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    op.add_column('customers', sa.Column('marketing_opt_out', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('customers', sa.Column('transactional_allowed', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    op.add_column('customers', sa.Column('timezone', sa.String(length=50), nullable=False, server_default='Asia/Kolkata'))
    op.add_column('customers', sa.Column('preferred_language', sa.String(length=20), nullable=False, server_default='ENGLISH'))

    # 2. Create communications table
    op.create_table(
        'communications',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_case_id', GUID(), nullable=False),
        sa.Column('customer_id', GUID(), nullable=True),
        sa.Column('channel', sa.String(length=50), nullable=False, server_default='WHATSAPP'),
        sa.Column('provider', sa.String(length=50), nullable=False, server_default='DEVELOPMENT'),
        sa.Column('template_name', sa.String(length=100), nullable=False, server_default='PAYMENT_RECOVERY_EN_V1'),
        sa.Column('template_version', sa.String(length=20), nullable=False, server_default='v1.0'),
        sa.Column('language', sa.String(length=20), nullable=False, server_default='ENGLISH'),
        sa.Column('recipient_reference', sa.String(length=255), nullable=False),
        sa.Column('recipient_masked', sa.String(length=255), nullable=False),
        sa.Column('message_body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='GENERATED'),
        sa.Column('provider_message_id', sa.String(length=255), nullable=True),
        sa.Column('idempotency_key', sa.String(length=255), nullable=False),
        sa.Column('attempt_number', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('is_simulated', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata', JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key', name='uq_communications_idempotency_key')
    )
    op.create_index(op.f('ix_communications_recovery_case_id'), 'communications', ['recovery_case_id'], unique=False)
    op.create_index(op.f('ix_communications_customer_id'), 'communications', ['customer_id'], unique=False)
    op.create_index(op.f('ix_communications_channel'), 'communications', ['channel'], unique=False)
    op.create_index(op.f('ix_communications_status'), 'communications', ['status'], unique=False)
    op.create_index(op.f('ix_communications_provider_message_id'), 'communications', ['provider_message_id'], unique=False)
    op.create_index(op.f('ix_communications_created_at'), 'communications', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_communications_created_at'), table_name='communications')
    op.drop_index(op.f('ix_communications_provider_message_id'), table_name='communications')
    op.drop_index(op.f('ix_communications_status'), table_name='communications')
    op.drop_index(op.f('ix_communications_channel'), table_name='communications')
    op.drop_index(op.f('ix_communications_customer_id'), table_name='communications')
    op.drop_index(op.f('ix_communications_recovery_case_id'), table_name='communications')
    op.drop_table('communications')

    op.drop_column('customers', 'preferred_language')
    op.drop_column('customers', 'timezone')
    op.drop_column('customers', 'transactional_allowed')
    op.drop_column('customers', 'marketing_opt_out')
    op.drop_column('customers', 'whatsapp_allowed')
