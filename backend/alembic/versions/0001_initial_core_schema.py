"""Initial core revenue recovery schema

Revision ID: 0001_initial_core_schema
Revises: 
Create Date: 2026-08-21 21:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_core_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Customers
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_customer_id", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("segment", sa.String(length=50), nullable=False, server_default="STANDARD"),
        sa.Column("preferred_channel", sa.String(length=50), nullable=True),
        sa.Column("dnd_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_customers_email", "customers", ["email"])
    op.create_index("ix_customers_external_customer_id", "customers", ["external_customer_id"], unique=True)

    # 2. Payments
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_payment_id", sa.String(length=255), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("payment_method", sa.String(length=50), nullable=False, server_default="CARD"),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_description", sa.String(length=500), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payments_customer_id", "payments", ["customer_id"])
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index("ix_payments_created_at", "payments", ["created_at"])
    op.create_index("ix_payments_external_payment_id", "payments", ["external_payment_id"], unique=True)

    # 3. Subscriptions
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_subscription_id", sa.String(length=255), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="ACTIVE"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscriptions_customer_id", "subscriptions", ["customer_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_subscriptions_external_subscription_id", "subscriptions", ["external_subscription_id"], unique=True)

    # 4. Invoices
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="ISSUED"),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_invoices_customer_id", "invoices", ["customer_id"])
    op.create_index("ix_invoices_status", "invoices", ["status"])
    op.create_index("ix_invoices_due_date", "invoices", ["due_date"])
    op.create_index("ix_invoices_external_invoice_id", "invoices", ["external_invoice_id"], unique=True)

    # 5. Events
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_status", sa.String(length=50), nullable=False, server_default="RECEIVED"),
    )
    op.create_index("ix_events_external_event_id", "events", ["external_event_id"], unique=True)
    op.create_index("ix_events_event_type", "events", ["event_type"])
    op.create_index("ix_events_customer_id", "events", ["customer_id"])
    op.create_index("ix_events_payment_id", "events", ["payment_id"])
    op.create_index("ix_events_subscription_id", "events", ["subscription_id"])
    op.create_index("ix_events_invoice_id", "events", ["invoice_id"])

    # 6. Recovery Cases
    op.create_table(
        "recovery_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("events.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("amount_at_risk", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("case_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="OPEN"),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("recovery_probability", sa.Float(), nullable=True),
        sa.Column("recommended_channel", sa.String(length=50), nullable=True),
        sa.Column("recommended_action", sa.String(length=50), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovered_amount", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.create_index("ix_recovery_cases_customer_id", "recovery_cases", ["customer_id"])
    op.create_index("ix_recovery_cases_event_id", "recovery_cases", ["event_id"])
    op.create_index("ix_recovery_cases_payment_id", "recovery_cases", ["payment_id"])
    op.create_index("ix_recovery_cases_subscription_id", "recovery_cases", ["subscription_id"])
    op.create_index("ix_recovery_cases_invoice_id", "recovery_cases", ["invoice_id"])
    op.create_index("ix_recovery_cases_status", "recovery_cases", ["status"])
    op.create_index("ix_recovery_cases_case_type", "recovery_cases", ["case_type"])
    op.create_index("ix_recovery_cases_created_at", "recovery_cases", ["created_at"])

    # 7. Diagnoses
    op.create_table(
        "diagnoses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recovery_case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_diagnoses_recovery_case_id", "diagnoses", ["recovery_case_id"])
    op.create_index("ix_diagnoses_category", "diagnoses", ["category"])

    # 8. Recovery Actions
    op.create_table(
        "recovery_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recovery_case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="PLANNED"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_recovery_actions_recovery_case_id", "recovery_actions", ["recovery_case_id"])
    op.create_index("ix_recovery_actions_action_type", "recovery_actions", ["action_type"])
    op.create_index("ix_recovery_actions_status", "recovery_actions", ["status"])
    op.create_index("ix_recovery_actions_created_at", "recovery_actions", ["created_at"])

    # 9. Action Outcomes
    op.create_table(
        "action_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("recovery_actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recovery_case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome_type", sa.String(length=50), nullable=False),
        sa.Column("recovered_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("response_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_action_outcomes_action_id", "action_outcomes", ["action_id"])
    op.create_index("ix_action_outcomes_recovery_case_id", "action_outcomes", ["recovery_case_id"])
    op.create_index("ix_action_outcomes_outcome_type", "action_outcomes", ["outcome_type"])

    # 10. Promise to Pay
    op.create_table(
        "promise_to_pays",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recovery_case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("promised_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("promised_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_promise_to_pays_recovery_case_id", "promise_to_pays", ["recovery_case_id"])
    op.create_index("ix_promise_to_pays_customer_id", "promise_to_pays", ["customer_id"])
    op.create_index("ix_promise_to_pays_promised_date", "promise_to_pays", ["promised_date"])
    op.create_index("ix_promise_to_pays_status", "promise_to_pays", ["status"])

    # 11. Communication Logs
    op.create_table(
        "communication_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("recovery_case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("recovery_cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False, server_default="OUTBOUND"),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="SENT"),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_communication_logs_customer_id", "communication_logs", ["customer_id"])
    op.create_index("ix_communication_logs_recovery_case_id", "communication_logs", ["recovery_case_id"])
    op.create_index("ix_communication_logs_provider_message_id", "communication_logs", ["provider_message_id"])

    # 12. Audit Logs
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recovery_case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("recovery_cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_type", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_logs_recovery_case_id", "audit_logs", ["recovery_case_id"])
    op.create_index("ix_audit_logs_actor_type", "audit_logs", ["actor_type"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])

    # 13. Model Versions
    op.create_table(
        "model_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("algorithm", sa.String(length=100), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("training_dataset_version", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="TRAINED"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_model_versions_model_name", "model_versions", ["model_name"])
    op.create_index("ix_model_versions_status", "model_versions", ["status"])


def downgrade() -> None:
    op.drop_table("model_versions")
    op.drop_table("audit_logs")
    op.drop_table("communication_logs")
    op.drop_table("promise_to_pays")
    op.drop_table("action_outcomes")
    op.drop_table("recovery_actions")
    op.drop_table("diagnoses")
    op.drop_table("recovery_cases")
    op.drop_table("events")
    op.drop_table("invoices")
    op.drop_table("subscriptions")
    op.drop_table("payments")
    op.drop_table("customers")
