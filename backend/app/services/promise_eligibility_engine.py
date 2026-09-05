"""Promise Eligibility Engine evaluating case value and customer context for commitment requests."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
from app.core.config import settings
from app.models.customer import Customer
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


class PromiseEligibilityEngine:
    """Decision-support engine determining whether an open case qualifies for Promise-to-Pay engagement."""

    @classmethod
    def evaluate_eligibility(
        cls,
        case: RecoveryCase,
        customer: Optional[Customer] = None,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Evaluate whether case meets criteria for structured Promise-to-Pay outreach."""
        now = reference_time or datetime.now(timezone.utc)
        amount = float(case.amount_at_risk or 0.0)

        # 1. Terminal / Closed Check
        if case.status in ["RECOVERED", "CLOSED"]:
            return {
                "eligible": False,
                "score": 0.0,
                "reason": f"Case is already in terminal status '{case.status}'.",
            }

        # 2. Overdue Duration Check
        created_dt = case.created_at
        if created_dt and created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        hours_overdue = (now - created_dt).total_seconds() / 3600.0 if created_dt else 0.0

        # 3. Score Calculation
        score = 0.50
        reasons = []

        if amount >= settings.PROMISE_MIN_AMOUNT:
            score += 0.25
            reasons.append(f"High transaction value (Rs.{amount:,.2f} >= Rs.{settings.PROMISE_MIN_AMOUNT:,.2f})")
        else:
            score -= 0.15
            reasons.append(f"Sub-threshold transaction value (Rs.{amount:,.2f} < Rs.{settings.PROMISE_MIN_AMOUNT:,.2f})")

        if hours_overdue >= settings.PROMISE_MIN_OVERDUE_HOURS:
            score += 0.20
            reasons.append(f"Overdue duration ({hours_overdue:.1f}h >= {settings.PROMISE_MIN_OVERDUE_HOURS}h)")
        else:
            reasons.append(f"Recent failure ({hours_overdue:.1f}h < {settings.PROMISE_MIN_OVERDUE_HOURS}h)")

        cust = customer or case.customer
        if cust and cust.transactional_allowed:
            score += 0.05

        final_score = round(min(max(score, 0.0), 1.0), 2)
        eligible = final_score >= 0.65

        reason_str = "; ".join(reasons)
        if eligible:
            summary = f"Eligible for Promise-to-Pay (Score: {final_score}): {reason_str}."
        else:
            summary = f"Not recommended for Promise-to-Pay (Score: {final_score}): {reason_str}."

        return {
            "eligible": eligible,
            "score": final_score,
            "reason": summary,
        }
