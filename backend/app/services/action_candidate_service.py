"""Candidate Action Generation Service for Next-Best-Action Engine."""
import logging
from typing import List, Optional
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.customer import Customer
from app.models.promise_to_pay import PromiseToPay

logger = logging.getLogger(__name__)


class ActionCandidateService:
    """Generates feasible recovery action candidates based on case state, customer channels, and stopping rules."""

    ALLOWED_SYSTEM_ACTIONS = [
        "PAYMENT_RETRY",
        "EMAIL",
        "VOICE",
        "WHATSAPP",
        "NO_ACTION",
    ]

    @classmethod
    def get_candidate_actions(
        cls,
        case: RecoveryCase,
        db: Optional[Session] = None,
    ) -> List[str]:
        """Generate eligible candidate recovery interventions for the case.

        Args:
            case: RecoveryCase database instance.
            db: Optional active database session for checking active PTP agreements.

        Returns:
            List of candidate action strings (e.g. ['PAYMENT_RETRY', 'EMAIL', 'VOICE', 'WHATSAPP', 'NO_ACTION']).
        """
        # 1. Stopping Rule 1: Case is already recovered, resolved, or closed
        status = str(getattr(case, "status", "") or "").upper()
        if status in ["RECOVERED", "CLOSED", "RESOLVED"]:
            logger.info(f"[ACTION_CANDIDATE] Case {case.id} is {status}. Only NO_ACTION is eligible.")
            return ["NO_ACTION"]

        # 2. Stopping Rule 2: Active Promise-to-Pay pauses routine outreach
        if db is not None:
            active_ptp = (
                db.query(PromiseToPay)
                .filter(
                    PromiseToPay.recovery_case_id == case.id,
                    PromiseToPay.status.in_(["ACTIVE", "PENDING"]),
                )
                .first()
            )
            if active_ptp:
                logger.info(f"[ACTION_CANDIDATE] Case {case.id} has active Promise-to-Pay. Only NO_ACTION is eligible.")
                return ["NO_ACTION"]

        candidates = []

        customer: Optional[Customer] = getattr(case, "customer", None)
        phone = getattr(customer, "phone", None) if customer else None
        email = getattr(customer, "email", None) if customer else None

        # Candidate 1: PAYMENT_RETRY
        # Eligible by default for payment failures where gateway allows retries
        candidates.append("PAYMENT_RETRY")

        # Candidate 2: EMAIL
        # Eligible if customer has a valid email address
        if email and "@" in str(email):
            candidates.append("EMAIL")

        # Candidate 3: VOICE
        # Eligible if customer has a phone number
        if phone and len(str(phone).strip()) >= 8:
            candidates.append("VOICE")

        # Candidate 4: WHATSAPP
        # Eligible if customer has a phone number
        if phone and len(str(phone).strip()) >= 8:
            candidates.append("WHATSAPP")

        # Candidate 5: NO_ACTION
        # Always an available option
        if "NO_ACTION" not in candidates:
            candidates.append("NO_ACTION")

        return candidates
