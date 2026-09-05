"""Add decision, policy, and alternatives fields to recovery_actions table

Revision ID: 0003_add_recovery_action_decision_fields
Revises: 0002_add_diagnosis_fields
Create Date: 2026-08-22 00:50:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_decision_fields"
down_revision: Union[str, None] = "0002_add_diagnosis_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recovery_actions",
        sa.Column("decision_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "recovery_actions",
        sa.Column("decision_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "recovery_actions",
        sa.Column(
            "decision_engine_version",
            sa.String(length=50),
            server_default="decision_engine_v1",
            nullable=False,
        ),
    )
    op.add_column(
        "recovery_actions",
        sa.Column(
            "policy_engine_version",
            sa.String(length=50),
            server_default="policy_engine_v1",
            nullable=False,
        ),
    )
    op.add_column(
        "recovery_actions",
        sa.Column(
            "policy_result",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "recovery_actions",
        sa.Column(
            "alternatives",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "recovery_actions",
        sa.Column(
            "supporting_factors",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default="[]",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("recovery_actions", "supporting_factors")
    op.drop_column("recovery_actions", "alternatives")
    op.drop_column("recovery_actions", "policy_result")
    op.drop_column("recovery_actions", "policy_engine_version")
    op.drop_column("recovery_actions", "decision_engine_version")
    op.drop_column("recovery_actions", "decision_confidence")
    op.drop_column("recovery_actions", "decision_score")
