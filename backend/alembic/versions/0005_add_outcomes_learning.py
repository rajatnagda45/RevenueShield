"""Add recovery_outcomes and learning_examples tables

Revision ID: 0005_add_outcomes_learning
Revises: 0004_add_executions
Create Date: 2026-08-22 02:40:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_add_outcomes_learning"
down_revision: Union[str, None] = "0004_add_executions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create recovery_outcomes table
    op.create_table(
        "recovery_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"), primary_key=True),
        sa.Column(
            "recovery_case_id",
            postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"),
            sa.ForeignKey("recovery_cases.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "recovery_action_id",
            postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"),
            sa.ForeignKey("recovery_actions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"),
            sa.ForeignKey("recovery_executions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("outcome_type", sa.String(length=50), nullable=False, index=True),
        sa.Column("attribution", sa.String(length=50), nullable=False, server_default="UNKNOWN", index=True),
        sa.Column("amount_at_risk", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("amount_recovered", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
        sa.Column("recovery_percentage", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("time_to_recovery_seconds", sa.Float(), nullable=True),
        sa.Column("customer_response", sa.String(length=255), nullable=True),
        sa.Column("provider_event_id", sa.String(length=255), nullable=True, index=True),
        sa.Column(
            "outcome_metadata",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )

    # 2. Create learning_examples table
    op.create_table(
        "learning_examples",
        sa.Column("id", postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"), primary_key=True),
        sa.Column(
            "recovery_case_id",
            postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"),
            sa.ForeignKey("recovery_cases.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "recovery_action_id",
            postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"),
            sa.ForeignKey("recovery_actions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True).with_variant(sa.String(36), "sqlite"),
            sa.ForeignKey("recovery_executions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("diagnosis_category", sa.String(length=50), nullable=False, index=True),
        sa.Column("diagnosis_confidence", sa.Float(), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("recovery_probability", sa.Float(), nullable=False),
        sa.Column("amount_at_risk", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("case_age_at_decision_hours", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("customer_success_rate_at_decision", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("customer_failure_count_at_decision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("previous_recovery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payment_method", sa.String(length=50), nullable=True),
        sa.Column("bank", sa.String(length=50), nullable=True),
        sa.Column("action_type", sa.String(length=50), nullable=False, index=True),
        sa.Column("decision_score", sa.Float(), nullable=False),
        sa.Column("decision_confidence", sa.Float(), nullable=False),
        sa.Column("policy_allowed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "feature_snapshot",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("outcome_type", sa.String(length=50), nullable=True, index=True),
        sa.Column("amount_recovered", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("recovery_percentage", sa.Float(), nullable=True),
        sa.Column("time_to_recovery_seconds", sa.Float(), nullable=True),
        sa.Column("attribution", sa.String(length=50), nullable=True, index=True),
        sa.Column("label", sa.Integer(), nullable=True, index=True),
        sa.Column("is_finalized", sa.Boolean(), nullable=False, server_default="false", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("learning_examples")
    op.drop_table("recovery_outcomes")
