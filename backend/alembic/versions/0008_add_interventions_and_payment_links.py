"""Add interventions and recovery_payment_links tables.

Revision ID: 0008_add_interventions_and_payment_links
Revises: 0007_add_ml_registry_and_predictions
Create Date: 2026-08-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.db.base import GUID

# revision identifiers, used by Alembic.
revision: str = '0008_interventions_pay_links'
down_revision: Union[str, None] = '0007_ml_registry_predictions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create interventions table
    op.create_table(
        'interventions',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_case_id', GUID(), nullable=False),
        sa.Column('action_type', sa.String(length=50), nullable=False, server_default='SEND_PAYMENT_LINK'),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='PENDING'),
        sa.Column('reason', sa.String(length=255), nullable=True),
        sa.Column('prediction_id', GUID(), nullable=True),
        sa.Column('policy_decision_id', GUID(), nullable=True),
        sa.Column('predicted_probability', sa.Float(), nullable=True),
        sa.Column('expected_recovered_value', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('idempotency_key', sa.String(length=255), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_reason', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['prediction_id'], ['predictions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['policy_decision_id'], ['recovery_actions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_interventions_recovery_case_id'), 'interventions', ['recovery_case_id'], unique=False)
    op.create_index(op.f('ix_interventions_action_type'), 'interventions', ['action_type'], unique=False)
    op.create_index(op.f('ix_interventions_status'), 'interventions', ['status'], unique=False)
    op.create_index(op.f('ix_interventions_idempotency_key'), 'interventions', ['idempotency_key'], unique=True)
    op.create_index(op.f('ix_interventions_prediction_id'), 'interventions', ['prediction_id'], unique=False)
    op.create_index(op.f('ix_interventions_policy_decision_id'), 'interventions', ['policy_decision_id'], unique=False)

    # 2. Create recovery_payment_links table
    op.create_table(
        'recovery_payment_links',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_case_id', GUID(), nullable=False),
        sa.Column('intervention_id', GUID(), nullable=True),
        sa.Column('razorpay_payment_link_id', sa.String(length=255), nullable=False),
        sa.Column('payment_url', sa.String(length=500), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='INR'),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='CREATED'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('razorpay_payment_id', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['intervention_id'], ['interventions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_recovery_payment_links_recovery_case_id'), 'recovery_payment_links', ['recovery_case_id'], unique=False)
    op.create_index(op.f('ix_recovery_payment_links_intervention_id'), 'recovery_payment_links', ['intervention_id'], unique=False)
    op.create_index(op.f('ix_recovery_payment_links_razorpay_payment_link_id'), 'recovery_payment_links', ['razorpay_payment_link_id'], unique=True)
    op.create_index(op.f('ix_recovery_payment_links_status'), 'recovery_payment_links', ['status'], unique=False)
    op.create_index(op.f('ix_recovery_payment_links_razorpay_payment_id'), 'recovery_payment_links', ['razorpay_payment_id'], unique=False)


def downgrade() -> None:
    op.drop_table('recovery_payment_links')
    op.drop_table('interventions')
