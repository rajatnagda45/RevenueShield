"""Add recovery_executions table

Revision ID: 0004_add_executions
Revises: 0003_decision_fields
Create Date: 2026-08-22 01:10:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_add_executions"
down_revision: Union[str, None] = "0003_decision_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recovery_executions",
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
            sa.ForeignKey("recovery_actions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("action_type", sa.String(length=50), nullable=False, index=True),
        sa.Column("provider", sa.String(length=50), nullable=False, index=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="PENDING", index=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False, unique=True, index=True),
        sa.Column("provider_reference", sa.String(length=255), nullable=True),
        sa.Column("provider_url", sa.String(length=1024), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "execution_metadata",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("recovery_executions")
