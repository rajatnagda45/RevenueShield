"""Add self-learning feedback tables and columns.

Revision ID: 0011_add_self_learning_feedback_tables
Revises: 0010_add_recovery_plans_and_steps
Create Date: 2026-08-23 00:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from app.db.base import GUID, JSON_TYPE

# revision identifiers, used by Alembic.
revision: str = '0011_self_learning_feedback'
down_revision: Union[str, None] = '0010_recovery_plans_steps'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create recovery_attributions table
    op.create_table(
        'recovery_attributions',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_case_id', GUID(), nullable=False),
        sa.Column('recovery_step_id', GUID(), nullable=True),
        sa.Column('learning_example_id', GUID(), nullable=True),
        sa.Column('amount_recovered', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('attribution_type', sa.String(length=50), server_default='PRIMARY', nullable=False),
        sa.Column('attribution_weight', sa.Float(), server_default='1.0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recovery_step_id'], ['recovery_plan_steps.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['learning_example_id'], ['learning_examples.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_recovery_attributions_recovery_case_id'), 'recovery_attributions', ['recovery_case_id'], unique=False)
    op.create_index(op.f('ix_recovery_attributions_recovery_step_id'), 'recovery_attributions', ['recovery_step_id'], unique=False)
    op.create_index(op.f('ix_recovery_attributions_attribution_type'), 'recovery_attributions', ['attribution_type'], unique=False)

    # 2. Create model_evaluations table
    op.create_table(
        'model_evaluations',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('model_version', sa.String(length=50), nullable=False),
        sa.Column('evaluation_period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('evaluation_period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sample_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('roc_auc', sa.Float(), nullable=True),
        sa.Column('log_loss', sa.Float(), nullable=True),
        sa.Column('brier_score', sa.Float(), nullable=True),
        sa.Column('precision', sa.Float(), nullable=True),
        sa.Column('recall', sa.Float(), nullable=True),
        sa.Column('f1', sa.Float(), nullable=True),
        sa.Column('recovery_rate', sa.Float(), nullable=True),
        sa.Column('amount_recovered', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('amount_at_risk', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_model_evaluations_model_version'), 'model_evaluations', ['model_version'], unique=False)

    # 3. Add columns to learning_examples
    op.add_column('learning_examples', sa.Column('recovery_plan_id', GUID(), nullable=True))
    op.add_column('learning_examples', sa.Column('recovery_step_id', GUID(), nullable=True))
    op.add_column('learning_examples', sa.Column('channel', sa.String(length=50), server_default='EMAIL', nullable=False))
    op.add_column('learning_examples', sa.Column('model_version', sa.String(length=50), nullable=True))
    op.add_column('learning_examples', sa.Column('feature_version', sa.String(length=50), server_default='v1', nullable=False))
    op.add_column('learning_examples', sa.Column('expected_recovery_value', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('learning_examples', sa.Column('prediction_error', sa.Float(), nullable=True))
    op.add_column('learning_examples', sa.Column('training_eligible', sa.Boolean(), server_default='true', nullable=False))
    op.add_column('learning_examples', sa.Column('training_exclusion_reason', sa.String(length=255), nullable=True))
    op.add_column('learning_examples', sa.Column('environment_type', sa.String(length=50), server_default='TEST', nullable=False))

    op.create_foreign_key('fk_learning_examples_recovery_plan_id', 'learning_examples', 'recovery_plans', ['recovery_plan_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_learning_examples_recovery_step_id', 'learning_examples', 'recovery_plan_steps', ['recovery_step_id'], ['id'], ondelete='SET NULL')
    op.create_index(op.f('ix_learning_examples_recovery_plan_id'), 'learning_examples', ['recovery_plan_id'], unique=False)
    op.create_index(op.f('ix_learning_examples_recovery_step_id'), 'learning_examples', ['recovery_step_id'], unique=False)
    op.create_index(op.f('ix_learning_examples_training_eligible'), 'learning_examples', ['training_eligible'], unique=False)
    op.create_index(op.f('ix_learning_examples_environment_type'), 'learning_examples', ['environment_type'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_learning_examples_environment_type'), table_name='learning_examples')
    op.drop_index(op.f('ix_learning_examples_training_eligible'), table_name='learning_examples')
    op.drop_index(op.f('ix_learning_examples_recovery_step_id'), table_name='learning_examples')
    op.drop_index(op.f('ix_learning_examples_recovery_plan_id'), table_name='learning_examples')
    op.drop_constraint('fk_learning_examples_recovery_step_id', 'learning_examples', type_='foreignkey')
    op.drop_constraint('fk_learning_examples_recovery_plan_id', 'learning_examples', type_='foreignkey')

    op.drop_column('learning_examples', 'environment_type')
    op.drop_column('learning_examples', 'training_exclusion_reason')
    op.drop_column('learning_examples', 'training_eligible')
    op.drop_column('learning_examples', 'prediction_error')
    op.drop_column('learning_examples', 'expected_recovery_value')
    op.drop_column('learning_examples', 'feature_version')
    op.drop_column('learning_examples', 'model_version')
    op.drop_column('learning_examples', 'channel')
    op.drop_column('learning_examples', 'recovery_step_id')
    op.drop_column('learning_examples', 'recovery_plan_id')

    op.drop_table('model_evaluations')
    op.drop_table('recovery_attributions')
