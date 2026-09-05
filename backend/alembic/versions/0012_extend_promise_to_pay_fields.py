"""Extend promise_to_pays table fields.

Revision ID: 0012_extend_promise_to_pay_fields
Revises: 0011_add_self_learning_feedback_tables
Create Date: 2026-08-23 01:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from app.db.base import GUID, JSON_TYPE

# revision identifiers, used by Alembic.
revision: str = '0012_extend_ptp_fields'
down_revision: Union[str, None] = '0011_self_learning_feedback'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('promise_to_pays', sa.Column('amount_due', sa.Numeric(precision=12, scale=2), server_default='0.00', nullable=False))
    op.add_column('promise_to_pays', sa.Column('promised_time', sa.String(length=10), server_default='17:00', nullable=True))
    op.add_column('promise_to_pays', sa.Column('currency', sa.String(length=10), server_default='INR', nullable=False))
    op.add_column('promise_to_pays', sa.Column('source', sa.String(length=50), server_default='CUSTOMER', nullable=False))
    op.add_column('promise_to_pays', sa.Column('confidence', sa.Float(), server_default='1.0', nullable=False))
    op.add_column('promise_to_pays', sa.Column('notes', sa.String(length=255), nullable=True))
    op.add_column('promise_to_pays', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))
    op.add_column('promise_to_pays', sa.Column('expired_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('promise_to_pays', sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('promise_to_pays', 'cancelled_at')
    op.drop_column('promise_to_pays', 'expired_at')
    op.drop_column('promise_to_pays', 'updated_at')
    op.drop_column('promise_to_pays', 'notes')
    op.drop_column('promise_to_pays', 'confidence')
    op.drop_column('promise_to_pays', 'source')
    op.drop_column('promise_to_pays', 'currency')
    op.drop_column('promise_to_pays', 'promised_time')
    op.drop_column('promise_to_pays', 'amount_due')
