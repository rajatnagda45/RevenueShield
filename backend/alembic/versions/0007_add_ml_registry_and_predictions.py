"""Add ML registry fields and predictions table.

Revision ID: 0007_add_ml_registry_and_predictions
Revises: 0006_add_sync_checkpoints
Create Date: 2026-08-22 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.db.base import GUID, JSON_TYPE

# revision identifiers, used by Alembic.
revision: str = '0007_ml_registry_predictions'
down_revision: Union[str, None] = '0006_add_sync_checkpoints'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Expand model_versions table
    op.add_column('model_versions', sa.Column('model_type', sa.String(length=50), nullable=False, server_default='LOGISTIC_REGRESSION'))
    op.add_column('model_versions', sa.Column('dataset_type', sa.String(length=50), nullable=False, server_default='REAL'))
    op.add_column('model_versions', sa.Column('dataset_version', sa.String(length=100), nullable=True))
    op.add_column('model_versions', sa.Column('feature_schema_version', sa.String(length=50), nullable=False, server_default='v1'))
    op.add_column('model_versions', sa.Column('artifact_path', sa.String(length=500), nullable=True))
    op.add_column('model_versions', sa.Column('training_started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('model_versions', sa.Column('training_completed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_model_versions_version'), 'model_versions', ['version'], unique=False)

    # 2. Create predictions table
    op.create_table(
        'predictions',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('recovery_case_id', GUID(), nullable=False),
        sa.Column('model_version_id', GUID(), nullable=True),
        sa.Column('model_version', sa.String(length=50), nullable=False, server_default='heuristic_v1'),
        sa.Column('feature_schema_version', sa.String(length=50), nullable=False, server_default='v1'),
        sa.Column('action_type', sa.String(length=100), nullable=False),
        sa.Column('predicted_probability', sa.Float(), nullable=False),
        sa.Column('expected_recovered_value', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('strategy', sa.String(length=50), nullable=False, server_default='ML'),
        sa.Column('contributing_factors', JSON_TYPE, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['recovery_case_id'], ['recovery_cases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_predictions_recovery_case_id'), 'predictions', ['recovery_case_id'], unique=False)
    op.create_index(op.f('ix_predictions_model_version_id'), 'predictions', ['model_version_id'], unique=False)
    op.create_index(op.f('ix_predictions_action_type'), 'predictions', ['action_type'], unique=False)
    op.create_index(op.f('ix_predictions_strategy'), 'predictions', ['strategy'], unique=False)
    op.create_index(op.f('ix_predictions_created_at'), 'predictions', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_predictions_created_at'), table_name='predictions')
    op.drop_index(op.f('ix_predictions_strategy'), table_name='predictions')
    op.drop_index(op.f('ix_predictions_action_type'), table_name='predictions')
    op.drop_index(op.f('ix_predictions_model_version_id'), table_name='predictions')
    op.drop_index(op.f('ix_predictions_recovery_case_id'), table_name='predictions')
    op.drop_table('predictions')

    op.drop_index(op.f('ix_model_versions_version'), table_name='model_versions')
    op.drop_column('model_versions', 'training_completed_at')
    op.drop_column('model_versions', 'training_started_at')
    op.drop_column('model_versions', 'artifact_path')
    op.drop_column('model_versions', 'feature_schema_version')
    op.drop_column('model_versions', 'dataset_version')
    op.drop_column('model_versions', 'dataset_type')
    op.drop_column('model_versions', 'model_type')
