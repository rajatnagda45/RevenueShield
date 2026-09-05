"""Add recovery_plans and recovery_plan_steps tables.

Revision ID: 0010_add_recovery_plans_and_steps
Revises: 0009_add_communications
Create Date: 2026-08-22 17:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.db.base import GUID, JSON_TYPE

# revision identifiers, used by Alembic.
revision: str = '0010_recovery_plans_steps'
down_revision: Union[str, None] = '0009_add_communications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create recovery_plans table
    op.create_table(
        'recovery_plans',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_case_id', GUID(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='ACTIVE', nullable=False),
        sa.Column('current_step', sa.Integer(), server_default='0', nullable=False),
        sa.Column('max_steps', sa.Integer(), server_default='3', nullable=False),
        sa.Column('next_evaluation_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completion_reason', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('recovery_case_id', name='uq_recovery_plans_recovery_case_id'),
    )
    op.create_index(op.f('ix_recovery_plans_recovery_case_id'), 'recovery_plans', ['recovery_case_id'], unique=False)
    op.create_index(op.f('ix_recovery_plans_status'), 'recovery_plans', ['status'], unique=False)
    op.create_index(op.f('ix_recovery_plans_next_evaluation_at'), 'recovery_plans', ['next_evaluation_at'], unique=False)

    # 2. Create recovery_plan_steps table
    op.create_table(
        'recovery_plan_steps',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_plan_id', GUID(), nullable=False),
        sa.Column('step_number', sa.Integer(), nullable=False),
        sa.Column('action_type', sa.String(length=100), nullable=False),
        sa.Column('channel', sa.String(length=50), server_default='EMAIL', nullable=False),
        sa.Column('status', sa.String(length=50), server_default='PENDING', nullable=False),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('prediction_score', sa.Float(), nullable=True),
        sa.Column('expected_recovery_value', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('metadata', JSON_TYPE, server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['recovery_plan_id'], ['recovery_plans.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('recovery_plan_id', 'step_number', name='uq_recovery_plan_steps_number'),
    )
    op.create_index(op.f('ix_recovery_plan_steps_recovery_plan_id'), 'recovery_plan_steps', ['recovery_plan_id'], unique=False)
    op.create_index(op.f('ix_recovery_plan_steps_status'), 'recovery_plan_steps', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_recovery_plan_steps_status'), table_name='recovery_plan_steps')
    op.drop_index(op.f('ix_recovery_plan_steps_recovery_plan_id'), table_name='recovery_plan_steps')
    op.drop_table('recovery_plan_steps')

    op.drop_index(op.f('ix_recovery_plans_next_evaluation_at'), table_name='recovery_plans')
    op.drop_index(op.f('ix_recovery_plans_status'), table_name='recovery_plans')
    op.drop_index(op.f('ix_recovery_plans_recovery_case_id'), table_name='recovery_plans')
    op.drop_table('recovery_plans')
