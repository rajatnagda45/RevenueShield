"""Leak surfaces: where revenue slips away, and the recovery profile for each.

Track 3 names three surfaces explicitly (payment failures, checkout abandonment, overdue
receivables) and lists mandate retry sequencing as a direction. Each surface carries a bounded
recovery profile that the policy engine, scheduler and agent read from one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class LeakSurface(str, Enum):
    PAYMENT_FAILURE = "PAYMENT_FAILURE"
    SUBSCRIPTION_MANDATE_FAILURE = "SUBSCRIPTION_MANDATE_FAILURE"
    CHECKOUT_ABANDONMENT = "CHECKOUT_ABANDONMENT"
    RECEIVABLE_OVERDUE = "RECEIVABLE_OVERDUE"


@dataclass(frozen=True)
class SurfaceProfile:
    """Bounded recovery profile for a leak surface."""

    surface: LeakSurface
    max_touches: int                 # total customer contacts across all channels
    voice_allowed: bool              # may the system place recovery calls
    retry_allowed: bool              # may the system re-attempt the payment instrument
    escalation_allowed: bool         # may the system escalate to merchant staff / B2B collections
    reevaluation_hours: int          # wait between plan steps
    max_duration_hours: int          # plan expiry
    pre_debit_notice_hours: int = 0  # mandates: notify customer at least this long before a debit
    description: str = ""


SURFACE_PROFILES: Dict[LeakSurface, SurfaceProfile] = {
    LeakSurface.PAYMENT_FAILURE: SurfaceProfile(
        surface=LeakSurface.PAYMENT_FAILURE, max_touches=3, voice_allowed=True, retry_allowed=True,
        escalation_allowed=True, reevaluation_hours=24, max_duration_hours=72,
        description="One-off or invoice payment failed at the gateway.",
    ),
    LeakSurface.SUBSCRIPTION_MANDATE_FAILURE: SurfaceProfile(
        surface=LeakSurface.SUBSCRIPTION_MANDATE_FAILURE, max_touches=3, voice_allowed=True, retry_allowed=True,
        escalation_allowed=True, reevaluation_hours=24, max_duration_hours=7 * 24, pre_debit_notice_hours=24,
        description="Recurring charge on a UPI Autopay / e-mandate / card mandate failed; subscription pending or halted.",
    ),
    LeakSurface.CHECKOUT_ABANDONMENT: SurfaceProfile(
        surface=LeakSurface.CHECKOUT_ABANDONMENT, max_touches=2, voice_allowed=False, retry_allowed=False,
        escalation_allowed=False, reevaluation_hours=12, max_duration_hours=48,
        description="Order created (or payment cancelled by the user) with no successful payment.",
    ),
    LeakSurface.RECEIVABLE_OVERDUE: SurfaceProfile(
        surface=LeakSurface.RECEIVABLE_OVERDUE, max_touches=6, voice_allowed=True, retry_allowed=False,
        escalation_allowed=True, reevaluation_hours=72, max_duration_hours=60 * 24,
        description="B2B invoice past its due date (Razorpay Invoices or imported AR ledger).",
    ),
}


def profile_for(surface: Optional[str]) -> SurfaceProfile:
    """Resolve a profile from a surface string, defaulting to PAYMENT_FAILURE."""
    try:
        return SURFACE_PROFILES[LeakSurface(surface or LeakSurface.PAYMENT_FAILURE.value)]
    except ValueError:
        return SURFACE_PROFILES[LeakSurface.PAYMENT_FAILURE]


def ageing_bucket(days_overdue: float) -> str:
    """Standard AR ageing buckets."""
    if days_overdue <= 7:
        return "1-7"
    if days_overdue <= 30:
        return "8-30"
    if days_overdue <= 60:
        return "31-60"
    return "60+"


# Razorpay `payment.failed` reasons that indicate the customer walked away from checkout rather than
# a bank/instrument problem. These open a CHECKOUT_ABANDONMENT case when an order id is present.
CHECKOUT_ABANDONMENT_REASONS = {
    "payment_cancelled", "user_cancelled", "cancelled by user", "payment_cancelled_by_user",
    "user_dropped", "session_expired", "timeout_by_user", "checkout_abandoned",
}


def is_checkout_abandonment(failure_reason: Optional[str], failure_code: Optional[str], description: Optional[str], has_order: bool) -> bool:
    if not has_order:
        return False
    text = " ".join(filter(None, [failure_reason, failure_code, description])).lower()
    return any(marker in text for marker in CHECKOUT_ABANDONMENT_REASONS)
