"""Event Processing Service.

Decoupled core processing service for incoming payment & billing events.
Handles idempotency, entity reconciliation, RecoveryCase state transitions,
and compliance audit logging.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.payment import Payment
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.audit_log import AuditLog
from app.schemas.event import NormalizedEvent, WebhookProcessingResult
from app.services.customer_service import CustomerService
from app.outcomes.engine import OutcomeEngine
from app.integrations.razorpay.payment_normalizer import PaymentNormalizer, NormalizedPaymentData
from app.repositories.payment_repository import PaymentRepository
from app.ledger.service import LedgerService


class EventProcessor:
    """Core domain service for ingesting and processing normalized events."""

    @classmethod
    def process_normalized_event(
        cls, db: Session, event: NormalizedEvent
    ) -> WebhookProcessingResult:
        """Process a normalized event idempotently and transition recovery workflows.

        Args:
            db: Database session.
            event: Normalized incoming event.

        Returns:
            WebhookProcessingResult with execution status.
        """
        # Lazy imports: detectors import services, and the services package imports this module.
        from app.detectors.case_factory import CaseFactory
        from app.detectors.checkout_abandonment import CheckoutAbandonmentDetector
        from app.detectors.surface_router import SurfaceEventRouter
        from app.domain.surfaces import LeakSurface, is_checkout_abandonment
        from app.degradation.monitor import DegradationMonitor

        # 1. Idempotency Check: Prevent duplicate event processing
        existing_event = db.scalar(
            select(Event).where(Event.external_event_id == event.event_id)
        )
        if existing_event:
            # Find associated recovery case if any
            existing_case = db.scalar(
                select(RecoveryCase).where(RecoveryCase.event_id == existing_event.id)
            )
            return WebhookProcessingResult(
                status="duplicate",
                event_id=event.event_id,
                internal_event_id=str(existing_event.id),
                recovery_case_id=str(existing_case.id) if existing_case else None,
                message="Duplicate event detected; skipped processing.",
            )

        try:
            # 2. Resolve or provision customer
            customer: Customer = CustomerService.resolve_or_create_customer(db, event)

            # 3. Resolve or create Payment record via unified PaymentRepository
            payment: Optional[Payment] = None
            if event.external_payment_id:
                if event.raw_payload:
                    try:
                        norm_payment = PaymentNormalizer.normalize_webhook(event.raw_payload)
                    except Exception:
                        norm_payment = None
                else:
                    norm_payment = None

                if not norm_payment:
                    # Construct fallback normalized payment from event fields
                    norm_payment = NormalizedPaymentData(
                        external_payment_id=event.external_payment_id,
                        razorpay_order_id=None,
                        amount=event.amount,
                        currency=event.currency,
                        status="CAPTURED" if event.event_type == "payment.captured" else ("FAILED" if event.event_type == "payment.failed" else "UNKNOWN"),
                        payment_method=event.payment_method or "CARD",
                        bank=None,
                        wallet=None,
                        vpa=None,
                        international=False,
                        captured=(event.event_type == "payment.captured"),
                        amount_refunded=event.amount if event.event_type == "refund.processed" else Decimal("0.00"),
                        refund_status=None,
                        description=None,
                        error_code=event.failure_code,
                        error_description=event.failure_description,
                        error_source=None,
                        error_step=None,
                        error_reason=event.failure_reason,
                        paid_at=event.occurred_at if event.event_type == "payment.captured" else None,
                        razorpay_created_at=event.occurred_at or datetime.now(timezone.utc),
                        customer_id_ext=event.external_customer_id,
                        customer_email=event.customer_email,
                        customer_phone=event.customer_phone,
                        customer_name=event.customer_name,
                        raw_payload=event.raw_payload or {},
                    )

                payment, _ = PaymentRepository.upsert_payment(db=db, data=norm_payment, customer_id=customer.id)

            # 4. Store Event record with raw payload
            db_event = Event(
                external_event_id=event.event_id,
                event_type=event.event_type,
                source=event.source,
                customer_id=customer.id,
                payment_id=payment.id if payment else None,
                payload=event.raw_payload,
                occurred_at=event.occurred_at,
                processing_status="PROCESSED",
                processed_at=datetime.now(timezone.utc),
            )
            db.add(db_event)
            db.flush()

            # 5. RecoveryCase State Machine & Lifecycle Transitions
            recovery_case: Optional[RecoveryCase] = None

            if event.event_type == "payment.failed":
                # Track the checkout (order) this attempt belonged to, if any.
                order = None
                if event.external_order_id:
                    order = CheckoutAbandonmentDetector.upsert_order(
                        db, external_order_id=event.external_order_id, amount=event.amount, currency=event.currency,
                        customer=customer, created_at=event.occurred_at, notes=(event.metadata or {}).get("notes") or {},
                    )
                    CheckoutAbandonmentDetector.mark_attempt(db, order, at=event.occurred_at)

                if order is not None and is_checkout_abandonment(
                    event.failure_reason, event.failure_code, event.failure_description, has_order=True
                ):
                    # The customer walked away from checkout: a different (lighter) surface than a bank failure.
                    recovery_case = CheckoutAbandonmentDetector.open_case_for_order(
                        db, order=order, customer=customer, db_event=db_event,
                        reason="payment cancelled by customer at checkout", batch_id=event.batch_id, source_event=event,
                    )
                else:
                    # Portfolio check: is this bank/method currently degraded? Then the customer is not at fault.
                    case_meta: Dict[str, Any] = {"order_id": event.external_order_id} if event.external_order_id else {}
                    diagnosis_event = event
                    systemic = DegradationMonitor.systemic_context_for_failure(
                        db, (event.metadata or {}).get("bank"), event.payment_method
                    )
                    if systemic:
                        case_meta.update(systemic)
                        diagnosis_event = event.model_copy(update={
                            "failure_description": f"{event.failure_description or ''} [systemic_issuer_degradation]".strip(),
                        })

                    # Bank/instrument failure: open a PAYMENT_FAILURE case with the full bootstrap sequence.
                    recovery_case = CaseFactory.open_case(
                        db,
                        customer=customer,
                        db_event=db_event,
                        surface=LeakSurface.PAYMENT_FAILURE,
                        amount=event.amount,
                        currency=event.currency,
                        diagnosis_event=diagnosis_event,
                        case_type="PAYMENT_FAILURE",
                        payment=payment,
                        batch_id=event.batch_id,
                        metadata=case_meta or None,
                        actor_id="razorpay_webhook_processor",
                        ledger_reference=event.external_payment_id or event.event_id,
                    )

            elif SurfaceEventRouter.handles(event.event_type):
                recovery_case = SurfaceEventRouter.handle(
                    db, event=event, customer=customer, db_event=db_event, payment=payment
                )

            elif event.event_type == "payment.captured":
                # Locate open recovery case associated with this payment or customer
                if payment:
                    recovery_case = db.scalar(
                        select(RecoveryCase).where(
                            RecoveryCase.payment_id == payment.id,
                            RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PTP"]),
                        )
                    )

                if not recovery_case:
                    # Fallback match by customer and amount if payment reference was decoupled
                    recovery_case = db.scalar(
                        select(RecoveryCase).where(
                            RecoveryCase.customer_id == customer.id,
                            RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PTP"]),
                            RecoveryCase.amount_at_risk == event.amount,
                        )
                    )

                if recovery_case:
                    OutcomeEngine.process_payment_capture(
                        db=db,
                        recovery_case=recovery_case,
                        captured_amount=event.amount,
                        captured_at=event.occurred_at or datetime.now(timezone.utc),
                        provider_event_id=event.event_id,
                        provider_payment_id=event.external_payment_id,
                    )

            elif event.event_type == "refund.processed":
                # Money going back to the customer reduces net recovery on the ledger.
                refund_entity = ((event.raw_payload or {}).get("payload", {}).get("refund", {}) or {}).get("entity", {}) or {}
                refund_id = refund_entity.get("id") or event.event_id
                try:
                    refund_amount = (Decimal(str(refund_entity.get("amount", 0))) / Decimal("100")).quantize(Decimal("0.01"))
                except Exception:
                    refund_amount = Decimal("0.00")
                if payment:
                    recovery_case = db.scalar(
                        select(RecoveryCase)
                        .where(RecoveryCase.payment_id == payment.id)
                        .order_by(RecoveryCase.created_at.desc())
                    )
                if recovery_case and refund_amount > 0:
                    LedgerService.post_refund(
                        db, recovery_case, refund_amount, provider_reference=str(refund_id),
                        occurred_at=event.occurred_at, metadata={"payment_id": event.external_payment_id},
                    )
                    db.add(
                        AuditLog(
                            recovery_case_id=recovery_case.id,
                            actor_type="SYSTEM",
                            actor_id="razorpay_webhook_processor",
                            action="REFUND_RECORDED",
                            entity_type="RecoveryCase",
                            entity_id=str(recovery_case.id),
                            audit_metadata={"refund_id": str(refund_id), "amount": float(refund_amount)},
                        )
                    )

            elif event.event_type == "settlement.processed":
                # Settlement entity carries no payment ids; recon is fetched separately (ledger reconcile API/job).
                settlement_entity = ((event.raw_payload or {}).get("payload", {}).get("settlement", {}) or {}).get("entity", {}) or {}
                db.add(
                    AuditLog(
                        recovery_case_id=None,
                        actor_type="SYSTEM",
                        actor_id="razorpay_webhook_processor",
                        action="SETTLEMENT_RECEIVED",
                        entity_type="Settlement",
                        entity_id=str(settlement_entity.get("id") or event.event_id),
                        audit_metadata={
                            "amount": settlement_entity.get("amount"),
                            "status": settlement_entity.get("status"),
                            "utr": settlement_entity.get("utr"),
                        },
                    )
                )

            db.commit()

            from app.observability.metrics import metrics
            metrics.webhook(event.event_type, "processed")

            return WebhookProcessingResult(
                status="processed",
                event_id=event.event_id,
                internal_event_id=str(db_event.id),
                recovery_case_id=str(recovery_case.id) if recovery_case else None,
                message=f"Event {event.event_type} successfully processed.",
            )

        except IntegrityError:
            db.rollback()
            # Handle concurrent race conditions on external_event_id database uniqueness
            existing_event = db.scalar(
                select(Event).where(Event.external_event_id == event.event_id)
            )
            return WebhookProcessingResult(
                status="duplicate",
                event_id=event.event_id,
                internal_event_id=str(existing_event.id) if existing_event else None,
                message="Duplicate event caught by database constraint; safely skipped.",
            )
        except Exception:
            db.rollback()
            raise
