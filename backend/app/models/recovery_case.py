import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from sqlalchemy import String, Numeric, Integer, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.event import Event
    from app.models.payment import Payment
    from app.models.subscription import Subscription
    from app.models.invoice import Invoice
    from app.models.diagnosis import Diagnosis
    from app.models.recovery_action import RecoveryAction
    from app.models.action_outcome import ActionOutcome
    from app.models.promise_to_pay import PromiseToPay
    from app.models.communication_log import CommunicationLog
    from app.models.audit_log import AuditLog
    from app.models.execution import RecoveryExecution
    from app.models.outcome import RecoveryOutcome
    from app.models.learning import LearningExample
    from app.models.recovery_payment_link import RecoveryPaymentLink
    from app.models.communication import Communication
    from app.models.recovery_plan import RecoveryPlan
    from app.models.recovery_attribution import RecoveryAttribution
    from app.models.voice_call import VoiceCall
    from app.models.intervention import Intervention
    from app.models.experiment_assignment import ExperimentAssignment
    from app.models.ledger_entry import LedgerEntry


class RecoveryCase(Base):
    """Central business object orchestrating revenue recovery lifecycles."""

    __tablename__ = "recovery_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("events.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    payment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subscription_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    invoice_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    amount_at_risk: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD"
    )
    case_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="OPEN"
    )
    risk_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    recovery_probability: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    recommended_channel: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    recommended_action: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovered_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    # v2: measurement + multi-surface fields
    experiment_arm: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, index=True
    )  # TREATMENT | HOLDOUT
    batch_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )
    leak_surface: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True
    )  # PAYMENT_FAILURE | SUBSCRIPTION_MANDATE_FAILURE | CHECKOUT_ABANDONMENT | RECEIVABLE_OVERDUE
    case_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )  # surface-specific facts: order id, ageing bucket, mandate state, systemic incident id

    # Relationships
    experiment_assignment: Mapped[Optional["ExperimentAssignment"]] = relationship(
        "ExperimentAssignment", back_populates="recovery_case", uselist=False, cascade="all, delete-orphan"
    )
    ledger_entries: Mapped[List["LedgerEntry"]] = relationship(
        "LedgerEntry", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    customer: Mapped["Customer"] = relationship(
        "Customer", back_populates="recovery_cases"
    )
    originating_event: Mapped["Event"] = relationship(
        "Event", back_populates="recovery_cases"
    )
    payment: Mapped[Optional["Payment"]] = relationship(
        "Payment", back_populates="recovery_cases"
    )
    subscription: Mapped[Optional["Subscription"]] = relationship(
        "Subscription", back_populates="recovery_cases"
    )
    invoice: Mapped[Optional["Invoice"]] = relationship(
        "Invoice", back_populates="recovery_cases"
    )

    diagnoses: Mapped[List["Diagnosis"]] = relationship(
        "Diagnosis", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    recovery_actions: Mapped[List["RecoveryAction"]] = relationship(
        "RecoveryAction", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    action_outcomes: Mapped[List["ActionOutcome"]] = relationship(
        "ActionOutcome", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    promise_to_pays: Mapped[List["PromiseToPay"]] = relationship(
        "PromiseToPay", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    communication_logs: Mapped[List["CommunicationLog"]] = relationship(
        "CommunicationLog", back_populates="recovery_case"
    )
    audit_logs: Mapped[List["AuditLog"]] = relationship(
        "AuditLog", back_populates="recovery_case"
    )
    executions: Mapped[List["RecoveryExecution"]] = relationship(
        "RecoveryExecution", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    recovery_outcomes: Mapped[List["RecoveryOutcome"]] = relationship(
        "RecoveryOutcome", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    learning_examples: Mapped[List["LearningExample"]] = relationship(
        "LearningExample", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    interventions: Mapped[List["Intervention"]] = relationship(
        "Intervention", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    payment_links: Mapped[List["RecoveryPaymentLink"]] = relationship(
        "RecoveryPaymentLink", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    communications: Mapped[List["Communication"]] = relationship(
        "Communication", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    recovery_plan: Mapped[Optional["RecoveryPlan"]] = relationship(
        "RecoveryPlan", back_populates="recovery_case", uselist=False, cascade="all, delete-orphan"
    )
    recovery_attributions: Mapped[List["RecoveryAttribution"]] = relationship(
        "RecoveryAttribution", back_populates="recovery_case", cascade="all, delete-orphan"
    )
    voice_calls: Mapped[List["VoiceCall"]] = relationship(
        "VoiceCall", back_populates="recovery_case", cascade="all, delete-orphan"
    )
