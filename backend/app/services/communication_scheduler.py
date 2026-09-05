"""WhatsApp communication policy validation and scheduling engine."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import logging
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.communication import Communication
from app.models.customer import Customer
from app.models.recovery_case import RecoveryCase
from app.models.promise_to_pay import PromiseToPay

logger = logging.getLogger(__name__)


@dataclass
class PolicyCheckResult:
    """Outcome of WhatsApp outreach policy verification."""

    allowed: bool
    reason: str
    blocking_rule: Optional[str] = None
    next_eligible_at: Optional[datetime] = None
    attempt_count: int = 0
    max_attempts: int = 3
    evaluated_at: datetime = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CommunicationScheduler:
    """Enforces safety boundaries, quiet hours, cooldowns, attempt caps, and consent rules before outreach."""

    @classmethod
    def evaluate_outreach_policy(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        customer: Optional[Customer] = None,
        reference_time: Optional[datetime] = None,
    ) -> PolicyCheckResult:
        """Evaluate all compliance, quiet hours, cooldown, and attempt limits for WhatsApp outreach.

        Returns:
            PolicyCheckResult indicating whether outreach is currently permitted.
        """
        now = reference_time or datetime.now(timezone.utc)
        cust = customer or recovery_case.customer

        # 1. Check Recovery Case Status
        if recovery_case.status in ["RECOVERED", "CLOSED", "RESOLVED"]:
            return PolicyCheckResult(
                allowed=False,
                reason=f"Recovery case is already {recovery_case.status}. Outreach is strictly prohibited.",
                blocking_rule="CASE_ALREADY_RECOVERED_OR_CLOSED",
                attempt_count=0,
                max_attempts=settings.MAX_WHATSAPP_ATTEMPTS,
                evaluated_at=now,
            )

        # 2. Check Customer Phone Number
        if not cust or not cust.phone or len(cust.phone.strip()) < 7:
            return PolicyCheckResult(
                allowed=False,
                reason="Customer has no valid contact phone number available for WhatsApp outreach.",
                blocking_rule="RECIPIENT_PHONE_MISSING",
                attempt_count=0,
                max_attempts=settings.MAX_WHATSAPP_ATTEMPTS,
                evaluated_at=now,
            )

        # 3. Check Customer Consent & Opt-Out
        if hasattr(cust, "whatsapp_allowed") and cust.whatsapp_allowed is False:
            return PolicyCheckResult(
                allowed=False,
                reason="Customer has opted out of WhatsApp communications.",
                blocking_rule="WHATSAPP_OPT_OUT",
                attempt_count=0,
                max_attempts=settings.MAX_WHATSAPP_ATTEMPTS,
                evaluated_at=now,
            )

        if hasattr(cust, "dnd_enabled") and cust.dnd_enabled is True:
            return PolicyCheckResult(
                allowed=False,
                reason="Customer has explicit DND (Do-Not-Disturb) flag enabled.",
                blocking_rule="CUSTOMER_DND_ENABLED",
                attempt_count=0,
                max_attempts=settings.MAX_WHATSAPP_ATTEMPTS,
                evaluated_at=now,
            )

        # 4. Check Active Promise-to-Pay (Pauses all outreach)
        active_ptp = db.scalar(
            select(PromiseToPay).where(
                PromiseToPay.customer_id == cust.id,
                PromiseToPay.status == "ACTIVE",
            )
        )
        if active_ptp:
            return PolicyCheckResult(
                allowed=False,
                reason="Customer has an active Promise-to-Pay agreement. Routine recovery outreach is paused.",
                blocking_rule="PROMISE_TO_PAY_ACTIVE",
                attempt_count=0,
                max_attempts=settings.MAX_WHATSAPP_ATTEMPTS,
                evaluated_at=now,
            )

        # 5. Check Prior Communication Attempts
        prior_comms = db.scalars(
            select(Communication)
            .where(
                Communication.recovery_case_id == recovery_case.id,
                Communication.channel == "WHATSAPP",
                Communication.status.in_(["SENT", "DELIVERED", "READ", "QUEUED"]),
            )
            .order_by(Communication.created_at.desc())
        ).all()

        attempt_count = len(prior_comms)
        max_attempts = settings.MAX_WHATSAPP_ATTEMPTS

        if attempt_count >= max_attempts:
            return PolicyCheckResult(
                allowed=False,
                reason=f"Maximum WhatsApp outreach attempts ({max_attempts}) reached. Total sent: {attempt_count}.",
                blocking_rule="MAX_WHATSAPP_ATTEMPTS_EXCEEDED",
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                evaluated_at=now,
            )

        # 6. Check Cooldown Period between attempts
        if prior_comms:
            latest_comm = prior_comms[0]
            last_time = latest_comm.sent_at or latest_comm.created_at
            if last_time:
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                elapsed_minutes = (now - last_time).total_seconds() / 60.0
                cooldown_min = settings.WHATSAPP_COOLDOWN_MINUTES

                if elapsed_minutes < cooldown_min:
                    remaining_min = cooldown_min - elapsed_minutes
                    next_eligible = last_time + timedelta(minutes=cooldown_min)
                    return PolicyCheckResult(
                        allowed=False,
                        reason=f"Cooldown active. Must wait {remaining_min:.1f} more minutes before sending next message.",
                        blocking_rule="COOLDOWN_PERIOD_ACTIVE",
                        next_eligible_at=next_eligible,
                        attempt_count=attempt_count,
                        max_attempts=max_attempts,
                        evaluated_at=now,
                    )

        # 7. Check DND / Quiet Hours in Customer's Timezone
        tz_name = getattr(cust, "timezone", None) or settings.DEFAULT_TIMEZONE
        try:
            target_tz = ZoneInfo(tz_name)
        except Exception:
            target_tz = ZoneInfo(settings.DEFAULT_TIMEZONE)

        local_now = now.astimezone(target_tz)
        local_hour = local_now.hour

        dnd_start = settings.WHATSAPP_DND_START_HOUR  # e.g. 20 (8 PM)
        dnd_end = settings.WHATSAPP_DND_END_HOUR      # e.g. 8 (8 AM)

        # Quiet hours: 20:00 to 08:00
        if local_hour >= dnd_start or local_hour < dnd_end:
            return PolicyCheckResult(
                allowed=False,
                reason=f"Quiet hours / DND in effect ({dnd_start:02d}:00 - {dnd_end:02d}:00 in {tz_name}). Current local hour is {local_hour:02d}:00.",
                blocking_rule="QUIET_HOURS_DND_PROHIBITED",
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                evaluated_at=now,
            )

        # All Policy Checks Passed
        return PolicyCheckResult(
            allowed=True,
            reason="All recovery communication policy checks passed successfully.",
            blocking_rule=None,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            evaluated_at=now,
        )
