"""Smart Payment Recovery Intervention Service.

Orchestrates prediction -> decision -> policy -> payment-link creation -> notification,
enforcing strict idempotency, concurrency guards, policy authorization, and stopping rules.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.recovery_case import RecoveryCase
from app.models.intervention import Intervention
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.learning import LearningExample
from app.models.audit_log import AuditLog
from app.decision.base import ActionType
from app.decision.service import DecisionService
from app.diagnosis.service import DiagnosisService
from app.ml.prediction_service import PredictionService
from app.integrations.razorpay.payment_link_client import RazorpayPaymentLinkClient
from app.services.notification_service import NotificationService, NotificationDispatchResult

logger = logging.getLogger(__name__)


@dataclass
class PaymentLinkDTO:
    """Payment Link summary transfer object."""

    id: str
    razorpay_payment_link_id: str
    url: str
    amount: float
    currency: str
    status: str


@dataclass
class InterventionResult:
    """Result of an intervention execution."""

    case_id: str
    intervention_id: Optional[str]
    action: str
    status: str  # "SENT", "BLOCKED", "SUCCEEDED", "FAILED", "ALREADY_RECOVERED"
    payment_link: Optional[PaymentLinkDTO] = None
    predicted_probability: Optional[float] = None
    expected_recovered_value: Optional[float] = None
    reason: Optional[str] = None
    notification: Optional[Dict[str, Any]] = None


class InterventionService:
    """Orchestrates end-to-end recovery interventions."""

    _plink_client: Optional[RazorpayPaymentLinkClient] = None

    @classmethod
    def get_payment_link_client(cls) -> RazorpayPaymentLinkClient:
        """Get or initialize Razorpay Payment Link client."""
        if cls._plink_client is None:
            cls._plink_client = RazorpayPaymentLinkClient()
        return cls._plink_client

    @classmethod
    def set_payment_link_client(cls, client: RazorpayPaymentLinkClient) -> None:
        """Override payment link client (useful for unit testing/mocking)."""
        cls._plink_client = client

    @classmethod
    def preview_intervention(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
    ) -> Dict[str, Any]:
        """Generate a dry-run preview of the recommended intervention without side-effects."""
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{recovery_case_id}' not found.")

        # 1. Prediction
        pred_res = PredictionService.predict_for_case(db=db, recovery_case=case, save_predictions=False)
        top_pred = pred_res.top_prediction

        # 2. Latest Diagnosis
        diagnosis = db.scalar(
            select(RecoveryCase).where(RecoveryCase.id == recovery_case_id)
        )
        diag = case.diagnoses[-1] if case.diagnoses else None
        if not diag:
            diag = DiagnosisService.diagnose_case(db=db, recovery_case=case)

        # 3. Policy & Decision Evaluation (simulate without committing)
        action = DecisionService.generate_recommendation(
            db=db,
            recovery_case=case,
            diagnosis=diag,
        )

        policy_reasons = []
        if action.reason:
            policy_reasons.append(action.reason)
        if action.supporting_factors:
            policy_reasons.extend(action.supporting_factors)
        if not policy_reasons:
            policy_reasons = ["Action approved by PolicyEngine."]

        return {
            "case_id": str(case.id),
            "amount_at_risk": float(case.amount_at_risk or 0.0),
            "currency": case.currency,
            "recommended_action": action.action_type,
            "probability": float(top_pred.predicted_probability) if top_pred else float(case.recovery_probability or 0.5),
            "expected_recovered_value": float(top_pred.expected_recovered_value) if top_pred else float(case.amount_at_risk or 0.0) * 0.5,
            "policy_status": action.status,
            "policy_reasons": policy_reasons,
            "case_status": case.status,
        }

    @classmethod
    def execute_intervention(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        action_override: Optional[str] = None,
        dry_run: Optional[bool] = None,
        accept_partial: bool = False,
        first_min_partial_amount: Optional[Decimal] = None,
        reference_time: Optional[datetime] = None,
    ) -> InterventionResult:
        """Execute recovery intervention with idempotency and policy guards.

        Args:
            db: Database session.
            recovery_case_id: Target recovery case ID.
            action_override: Optional action type override (e.g. 'SEND_PAYMENT_LINK').
            dry_run: Explicit dry-run flag override. If None, checks settings.INTERVENTION_MODE.

        Returns:
            InterventionResult describing the execution status and created artifacts.
        """
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{recovery_case_id}' not found.")

        # Every time-based rule (contact window, frequency caps) evaluates against the
        # caller's clock so replay/virtual-time and tests are deterministic; falls back to now.
        now = reference_time or datetime.now(timezone.utc)

        # 1. State Check: Block if case is already recovered or closed
        if case.status in ("RECOVERED", "CLOSED"):
            logger.info(f"[INTERVENTION_BLOCKED] Case {case.id} is already in terminal status '{case.status}'.")
            return InterventionResult(
                case_id=str(case.id),
                intervention_id=None,
                action=action_override or "SEND_PAYMENT_LINK",
                status="ALREADY_RECOVERED",
                reason=f"Recovery case is already {case.status}.",
            )

        # 1b. Measurement: HOLDOUT arm never receives payment links or notifications
        from app.experiments.assigner import ExperimentAssigner
        holdout = ExperimentAssigner.holdout_block(case)
        if holdout:
            cls._audit(
                db=db,
                case_id=case.id,
                action="POLICY_BLOCKED",
                metadata={"action": action_override or "SEND_PAYMENT_LINK", "reason": holdout["reason"], "blocking_rule": holdout["blocking_rule"]},
            )
            db.commit()
            return InterventionResult(
                case_id=str(case.id),
                intervention_id=None,
                action=action_override or "SEND_PAYMENT_LINK",
                status="BLOCKED",
                reason=holdout["reason"],
            )

        # 2. Idempotency Check: Return existing active intervention / payment link
        active_link = db.scalar(
            select(RecoveryPaymentLink)
            .where(
                RecoveryPaymentLink.recovery_case_id == case.id,
                RecoveryPaymentLink.status.in_(["CREATED", "SENT"]),
            )
            .order_by(RecoveryPaymentLink.created_at.desc())
        )
        existing_intervention = db.scalar(
            select(Intervention)
            .where(
                Intervention.recovery_case_id == case.id,
                Intervention.status.in_(["EXECUTING", "SENT"]),
            )
            .order_by(Intervention.created_at.desc())
        )

        if active_link and existing_intervention:
            logger.info(f"[INTERVENTION_IDEMPOTENT] Reusing active intervention {existing_intervention.id} and link {active_link.razorpay_payment_link_id}.")
            return InterventionResult(
                case_id=str(case.id),
                intervention_id=str(existing_intervention.id),
                action=existing_intervention.action_type,
                status=existing_intervention.status,
                payment_link=PaymentLinkDTO(
                    id=str(active_link.id),
                    razorpay_payment_link_id=active_link.razorpay_payment_link_id,
                    url=active_link.payment_url,
                    amount=float(active_link.amount),
                    currency=active_link.currency,
                    status=active_link.status,
                ),
                predicted_probability=existing_intervention.predicted_probability,
                expected_recovered_value=float(existing_intervention.expected_recovered_value or 0.0),
                reason="Active intervention and payment link already exist.",
            )

        # 2b. Cross-channel compliance for the notification that carries the payment link
        from app.compliance.contact_policy import ContactPolicy
        notify_channel = "WHATSAPP" if (case.customer and case.customer.phone) else "EMAIL"
        compliance = ContactPolicy.evaluate(db, case=case, customer=case.customer, channel=notify_channel, now=now)
        if not compliance.allowed:
            cls._audit(
                db=db,
                case_id=case.id,
                action="POLICY_BLOCKED",
                metadata={"action": action_override or "SEND_PAYMENT_LINK", "reason": compliance.reason, "blocking_rule": compliance.rule, "channel": notify_channel},
            )
            db.commit()
            return InterventionResult(
                case_id=str(case.id),
                intervention_id=None,
                action=action_override or "SEND_PAYMENT_LINK",
                status="BLOCKED",
                reason=compliance.reason,
            )

        # 3. Predict Best Action & Value
        pred_res = PredictionService.predict_for_case(db=db, recovery_case=case, save_predictions=True)
        top_pred = pred_res.top_prediction
        selected_action = action_override or (top_pred.action if top_pred else ActionType.SEND_PAYMENT_LINK.value)

        # Audit: Prediction created
        cls._audit(
            db=db,
            case_id=case.id,
            action="PREDICTION_CREATED",
            metadata={
                "strategy": pred_res.strategy,
                "top_action": selected_action,
                "probability": top_pred.predicted_probability if top_pred else None,
                "expected_recovered_value": float(top_pred.expected_recovered_value) if top_pred else None,
            },
        )

        # 4. Fetch / Run Diagnosis
        diag = case.diagnoses[-1] if case.diagnoses else None
        if not diag:
            diag = DiagnosisService.diagnose_case(db=db, recovery_case=case)

        # 5. Policy Engine & Decision Verification
        rec_action = DecisionService.generate_recommendation(
            db=db,
            recovery_case=case,
            diagnosis=diag,
        )

        cls._audit(
            db=db,
            case_id=case.id,
            action="POLICY_CHECKED",
            metadata={"evaluated_action": selected_action, "policy_status": rec_action.status},
        )

        # Check policy approval
        if rec_action.status == "BLOCKED":
            reason = rec_action.reason or (rec_action.policy_result.get("policy_reason") if rec_action.policy_result else "Policy rule violation")
            blocked_intervention = Intervention(
                recovery_case_id=case.id,
                action_type=selected_action,
                status="BLOCKED",
                reason=reason,
                prediction_id=uuid.UUID(top_pred.prediction_id) if (top_pred and top_pred.prediction_id) else None,
                policy_decision_id=rec_action.id,
                predicted_probability=top_pred.predicted_probability if top_pred else None,
                expected_recovered_value=Decimal(str(top_pred.expected_recovered_value)) if top_pred else None,
                failure_reason=reason,
            )
            db.add(blocked_intervention)
            cls._audit(
                db=db,
                case_id=case.id,
                action="POLICY_BLOCKED",
                metadata={"action": selected_action, "reason": reason},
            )
            db.commit()

            return InterventionResult(
                case_id=str(case.id),
                intervention_id=str(blocked_intervention.id),
                action=selected_action,
                status="BLOCKED",
                reason=reason,
                predicted_probability=top_pred.predicted_probability if top_pred else None,
                expected_recovered_value=float(top_pred.expected_recovered_value) if top_pred else None,
            )

        # 6. Only Proceed if Action is SEND_PAYMENT_LINK (or permitted action)
        if selected_action != ActionType.SEND_PAYMENT_LINK.value:
            logger.info(f"[INTERVENTION_SKIPPED] Selected action '{selected_action}' is not SEND_PAYMENT_LINK.")
            return InterventionResult(
                case_id=str(case.id),
                intervention_id=None,
                action=selected_action,
                status="BLOCKED",
                reason=f"Action '{selected_action}' is not handled by Payment Link recovery intervention.",
            )

        # 7. Create Intervention Record (EXECUTING)
        idempotency_key = f"intervention_{case.id}_{selected_action}_{uuid.uuid4().hex[:8]}"
        intervention = Intervention(
            recovery_case_id=case.id,
            action_type=selected_action,
            status="EXECUTING",
            prediction_id=uuid.UUID(top_pred.prediction_id) if (top_pred and top_pred.prediction_id) else None,
            policy_decision_id=rec_action.id,
            predicted_probability=top_pred.predicted_probability if top_pred else None,
            expected_recovered_value=Decimal(str(top_pred.expected_recovered_value)) if top_pred else None,
            idempotency_key=idempotency_key,
        )
        db.add(intervention)
        db.flush()

        # Ensure initial LearningExample exists for future model learning
        existing_learning_ex = db.scalar(
            select(LearningExample).where(LearningExample.recovery_case_id == case.id)
        )
        if not existing_learning_ex:
            from app.learning.service import LearningDataService
            LearningDataService.create_initial_example(
                db=db,
                recovery_case=case,
                action=rec_action,
                diagnosis=diag,
            )

        cls._audit(
            db=db,
            case_id=case.id,
            action="INTERVENTION_CREATED",
            metadata={"intervention_id": str(intervention.id), "action": selected_action},
        )

        # 8. Create Payment Link (Dry Run vs Razorpay Test)
        mode = "dry_run"
        if dry_run is False:
            mode = "razorpay_test"
        elif dry_run is True:
            mode = "dry_run"
        else:
            configured_mode = getattr(settings, "INTERVENTION_MODE", None) or getattr(settings, "EXECUTION_MODE", "dry_run")
            mode = configured_mode.lower()

        amount_val = Decimal(str(case.amount_at_risk or "0.00"))
        currency_val = case.currency or "INR"
        customer = case.customer

        payment_link_id = ""
        payment_url = ""

        try:
            if mode in ("razorpay_test", "live"):
                client = cls.get_payment_link_client()
                amount_paise = int(amount_val * 100)
                desc = f"Payment recovery for Invoice / Case {str(case.id)[:8]}"

                res = client.create_payment_link(
                    amount_paise=amount_paise,
                    currency=currency_val,
                    description=desc,
                    customer_name=customer.name if customer else None,
                    customer_email=customer.email if customer else None,
                    customer_phone=customer.phone if customer else None,
                    reference_id=str(case.id),
                    accept_partial=accept_partial,
                    first_min_partial_amount_paise=int(first_min_partial_amount * 100) if (accept_partial and first_min_partial_amount) else None,
                )
                payment_link_id = res.payment_link_id
                payment_url = res.short_url
            else:
                # Dry run simulation
                payment_link_id = f"plink_sim_{uuid.uuid4().hex[:12]}"
                payment_url = f"https://rzp.io/i/{payment_link_id}"

            # Persist RecoveryPaymentLink
            existing_plink = db.scalar(
                select(RecoveryPaymentLink)
                .where(RecoveryPaymentLink.razorpay_payment_link_id == payment_link_id)
                .limit(1)
            )
            if existing_plink:
                existing_plink.recovery_case_id = case.id
                existing_plink.intervention_id = intervention.id
                existing_plink.payment_url = payment_url
                existing_plink.amount = amount_val
                existing_plink.currency = currency_val
                existing_plink.status = "SENT"
                plink_record = existing_plink
                db.flush()
            else:
                plink_record = RecoveryPaymentLink(
                    recovery_case_id=case.id,
                    intervention_id=intervention.id,
                    razorpay_payment_link_id=payment_link_id,
                    payment_url=payment_url,
                    amount=amount_val,
                    currency=currency_val,
                    status="SENT",
                )
                db.add(plink_record)
                db.flush()

            cls._audit(
                db=db,
                case_id=case.id,
                action="PAYMENT_LINK_CREATED",
                metadata={
                    "payment_link_id": payment_link_id,
                    "payment_url": payment_url,
                    "mode": mode,
                    "accept_partial": bool(accept_partial),
                    "first_min_partial_amount": float(first_min_partial_amount) if first_min_partial_amount else None,
                },
            )

            # 9. Dispatch Customer Notification
            recipient = customer.phone if (customer and customer.phone) else (customer.email if customer else None)
            notif_result: NotificationDispatchResult = NotificationService.send_recovery_notification(
                db=db,
                recovery_case_id=case.id,
                customer_id=customer.id if customer else None,
                recipient=recipient,
                amount=amount_val,
                currency=currency_val,
                payment_url=payment_url,
                channel="WHATSAPP",
            )

            cls._audit(
                db=db,
                case_id=case.id,
                action="NOTIFICATION_GENERATED",
                metadata={
                    "channel": notif_result.channel,
                    "recipient_masked": notif_result.recipient_masked,
                    "status": notif_result.status,
                },
            )

            # 10. Update Intervention & Case Status
            intervention.status = "SENT"
            case.status = "IN_PROGRESS"
            case.retry_count = (case.retry_count or 0) + 1
            from app.ledger.service import LedgerService
            LedgerService.post_cost(db, case, channel="PAYMENT_LINK", provider_reference=str(payment_link_id))
            LedgerService.post_cost(db, case, channel=str(notif_result.channel or "WHATSAPP"), provider_reference=f"notif_{intervention.id}")
            from app.compliance.contact_policy import ContactPolicy
            ContactPolicy.record_sent(db, case=case, channel=str(notif_result.channel or "WHATSAPP"), reference=f"notif_{intervention.id}", now=now)
            db.commit()

            return InterventionResult(
                case_id=str(case.id),
                intervention_id=str(intervention.id),
                action=selected_action,
                status="SENT",
                payment_link=PaymentLinkDTO(
                    id=str(plink_record.id),
                    razorpay_payment_link_id=payment_link_id,
                    url=payment_url,
                    amount=float(amount_val),
                    currency=currency_val,
                    status="SENT",
                ),
                predicted_probability=top_pred.predicted_probability if top_pred else None,
                expected_recovered_value=float(top_pred.expected_recovered_value) if top_pred else None,
                notification={
                    "status": notif_result.status,
                    "channel": notif_result.channel,
                    "recipient": notif_result.recipient_masked,
                },
            )

        except Exception as e:
            logger.exception(f"[INTERVENTION_EXECUTION_ERROR] Error creating payment link: {e}")
            intervention.status = "FAILED"
            intervention.failure_reason = str(e)
            db.commit()
            return InterventionResult(
                case_id=str(case.id),
                intervention_id=str(intervention.id),
                action=selected_action,
                status="FAILED",
                reason=str(e),
            )

    @classmethod
    def stop_intervention_on_recovery(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        captured_amount: Decimal,
        captured_at: Optional[datetime] = None,
        provider_payment_id: Optional[str] = None,
    ) -> None:
        """Stopping Rule: Mark active interventions and payment links resolved when payment succeeds.

        Prevents any further outreach or redundant retries.
        """
        now = captured_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        logger.info(f"[STOPPING_RULE_TRIGGERED] Payment captured for case={recovery_case.id}. Stopping all recovery outreach.")

        # 1. Update active interventions
        active_interventions = db.scalars(
            select(Intervention).where(
                Intervention.recovery_case_id == recovery_case.id,
                Intervention.status.in_(["EXECUTING", "SENT", "PENDING"]),
            )
        ).all()

        for intervention in active_interventions:
            intervention.status = "SUCCEEDED"
            intervention.completed_at = now

        # 2. Update active payment links
        active_links = db.scalars(
            select(RecoveryPaymentLink).where(
                RecoveryPaymentLink.recovery_case_id == recovery_case.id,
                RecoveryPaymentLink.status.in_(["CREATED", "SENT"]),
            )
        ).all()

        for plink in active_links:
            plink.status = "PAID"
            plink.paid_at = now
            if provider_payment_id:
                plink.razorpay_payment_id = provider_payment_id

        # 3. Cancel pending/approved recovery actions & communications
        DecisionService.cancel_pending_actions(
            db=db,
            recovery_case=recovery_case,
            cancellation_reason="Payment captured; case successfully recovered.",
        )
        from app.services.communication_orchestrator import CommunicationOrchestrator
        CommunicationOrchestrator.stop_whatsapp_on_recovery(
            db=db,
            recovery_case=recovery_case,
        )

        # 4. Audit events
        cls._audit(
            db=db,
            case_id=recovery_case.id,
            action="INTERVENTION_COMPLETED",
            metadata={
                "captured_amount": float(captured_amount),
                "interventions_stopped": len(active_interventions),
                "payment_links_resolved": len(active_links),
            },
        )
        cls._audit(
            db=db,
            case_id=recovery_case.id,
            action="CASE_RECOVERED",
            metadata={
                "recovered_amount": float(captured_amount),
                "recovered_at": now.isoformat(),
            },
        )

    @classmethod
    def _audit(
        cls,
        db: Session,
        case_id: uuid.UUID,
        action: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Internal helper to emit standard audit log entries."""
        audit_entry = AuditLog(
            recovery_case_id=case_id,
            actor_type="SYSTEM",
            actor_id="intervention_service_v1",
            action=action,
            entity_type="Intervention",
            entity_id=str(case_id),
            audit_metadata=metadata or {},
        )
        db.add(audit_entry)
        db.flush()
