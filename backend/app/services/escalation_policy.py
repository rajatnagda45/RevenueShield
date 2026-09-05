"""Escalation Policy framework defining escalation tiers and governance rules."""
from enum import Enum
import logging
from typing import Any, Dict
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


class EscalationLevel(str, Enum):
    """Hierarchical recovery escalation levels."""

    LEVEL_0 = "LEVEL_0"  # Passive / Wait (Low amount, fresh failure)
    LEVEL_1 = "LEVEL_1"  # Initial Primary Outreach (Transactional email)
    LEVEL_2 = "LEVEL_2"  # Follow-up Outreach (Scheduled reminder / payment link)
    LEVEL_3 = "LEVEL_3"  # High-Value / Promise-to-Pay / Operator Escalation


class EscalationPolicy:
    """Evaluates case value and elapsed progression to determine escalation tiers."""

    @classmethod
    def evaluate_level(
        cls,
        case: RecoveryCase,
        hours_overdue: float = 0.0,
        previous_attempts: int = 0,
    ) -> Dict[str, Any]:
        """Determine the escalation level for an open recovery case."""
        amount = float(case.amount_at_risk or 0.0)

        if case.status in ["RECOVERED", "CLOSED"]:
            return {
                "level": EscalationLevel.LEVEL_0.value,
                "reason": "Case is already closed/recovered.",
            }

        # Level 3: High Value (> Rs. 10,000) or 2+ previous outreach attempts
        if amount >= 10000.0 and (hours_overdue >= 24.0 or previous_attempts >= 2):
            return {
                "level": EscalationLevel.LEVEL_3.value,
                "reason": f"High-value case (Rs.{amount:,.2f}) with multiple attempts/overdue window -> Eligible for Promise-to-Pay & Operator Escalation.",
            }

        # Level 2: Previous attempt made or overdue > 24h
        if previous_attempts >= 1 or hours_overdue >= 24.0:
            return {
                "level": EscalationLevel.LEVEL_2.value,
                "reason": f"Follow-up tier (Attempt count: {previous_attempts}, Overdue: {hours_overdue:.1f}h).",
            }

        # Level 1: Initial failure
        if amount >= 1000.0:
            return {
                "level": EscalationLevel.LEVEL_1.value,
                "reason": f"Standard initial recovery tier (Rs.{amount:,.2f}).",
            }

        # Level 0: Micro-failure
        return {
            "level": EscalationLevel.LEVEL_0.value,
            "reason": f"Micro-transaction (< Rs.1,000) -> Passive background retry.",
        }
