"""Add evidence, risk_score, recovery_probability, and engine_version to diagnoses table

Revision ID: 0002_add_diagnosis_fields
Revises: 0001_initial_core_schema
Create Date: 2026-08-22 00:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_add_diagnosis_fields"
down_revision: Union[str, None] = "0001_initial_core_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "diagnoses",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "diagnoses",
        sa.Column("risk_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "diagnoses",
        sa.Column("recovery_probability", sa.Float(), nullable=True),
    )
    op.add_column(
        "diagnoses",
        sa.Column(
            "engine_version",
            sa.String(length=50),
            server_default="diagnosis_engine_v1",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("diagnoses", "engine_version")
    op.drop_column("diagnoses", "recovery_probability")
    op.drop_column("diagnoses", "risk_score")
    op.drop_column("diagnoses", "evidence")
