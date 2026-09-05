"""Unified cross-channel contact policy.

v1 had per-channel rules that did not see each other: three WhatsApp messages, three voice calls
and three emails were all "within limits" while the customer received nine touches in a week. This
policy runs before any channel-specific check and enforces the rules a regulator would ask about:

* consent (ledger + legacy flags) and TRAI DND registry
* human handoff freeze (dispute / human request / wrong number)
* contact windows in the customer's timezone: voice 08:00-19:00 (RBI Fair Practices Code for
  recovery agents), WhatsApp/SMS 08:00-21:00, email any time; no voice on national holidays
* cross-channel frequency: max touches per rolling 7 days, max voice calls per rolling 3 days,
  minimum gap between any two touches

Every decision - allowed or blocked - is recorded in `contact_attempts` so the scorecard can show
blocks by rule and the auditor can see the rule that stopped a message.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance.consent import ConsentService
from app.compliance.dnd import get_dnd_registry
from app.compliance.handoff import HANDOFF_FREEZE_RULE, HandoffService
from app.core.config import settings
from app.models.contact_attempt import ContactAttempt
from app.models.customer import Customer
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)

OUTREACH_CHANNELS = ("WHATSAPP", "SMS", "EMAIL", "VOICE")


@dataclass
class ComplianceDecision:
    allowed: bool
    channel: str
    rule: Optional[str] = None
    reason: str = ""
    next_eligible_at: Optional[datetime] = None
    checks: List[str] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["next_eligible_at"] = self.next_eligible_at.isoformat() if self.next_eligible_at else None
        return d


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ContactPolicy:
    """Evaluate and record customer-contact decisions."""

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _tz(customer: Optional[Customer]) -> ZoneInfo:
        name = (getattr(customer, "timezone", None) or settings.DEFAULT_TIMEZONE or "Asia/Kolkata")
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("Asia/Kolkata")

    @staticmethod
    def _window_for(channel: str) -> Optional[tuple[int, int]]:
        ch = channel.upper()
        if ch == "VOICE":
            return settings.CONTACT_VOICE_START_HOUR, settings.CONTACT_VOICE_END_HOUR
        if ch in ("WHATSAPP", "SMS"):
            return settings.CONTACT_MESSAGE_START_HOUR, settings.CONTACT_MESSAGE_END_HOUR
        return None  # email: no window

    @staticmethod
    def _holidays() -> set[date]:
        out: set[date] = set()
        for token in (settings.CONTACT_HOLIDAYS or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                out.add(date.fromisoformat(token))
            except ValueError:
                continue
        return out

    @classmethod
    def _next_window_open(cls, local_now: datetime, start_hour: int, end_hour: int, skip_dates: set[date]) -> datetime:
        candidate = local_now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if local_now.hour >= start_hour:
            candidate += timedelta(days=1)
        for _ in range(14):
            if candidate.date() not in skip_dates:
                return candidate
            candidate += timedelta(days=1)
        return candidate

    # ------------------------------------------------------------------ evaluation

    @classmethod
    def evaluate(
        cls,
        db: Session,
        *,
        case: Optional[RecoveryCase],
        customer: Optional[Customer],
        channel: str,
        now: Optional[datetime] = None,
        record: bool = True,
    ) -> ComplianceDecision:
        now = _aware(now) or datetime.now(timezone.utc)
        channel = channel.upper()
        checks: List[str] = []
        customer = customer or (case.customer if case else None)

        def blocked(rule: str, reason: str, next_at: Optional[datetime] = None) -> ComplianceDecision:
            dec = ComplianceDecision(False, channel, rule, reason, next_at, checks)
            if record and customer is not None:
                cls._record(db, customer=customer, case=case, channel=channel, outcome="BLOCKED", rule=rule, reason=reason, now=now)
                try:
                    from app.observability.metrics import metrics
                    metrics.policy_block(rule)
                except Exception:
                    pass
            return dec

        if customer is None:
            return ComplianceDecision(False, channel, "CUSTOMER_UNKNOWN", "No customer identity to evaluate consent for.", None, checks)

        # 1. Human handoff freeze
        if HandoffService.is_frozen(case):
            return blocked(HANDOFF_FREEZE_RULE, "Case is frozen for human handling (dispute / human request / wrong number).")
        checks.append("handoff:ok")

        # 2. Consent ledger + legacy flags
        opt_out = ConsentService.is_opted_out(db, customer.id, channel)
        if opt_out:
            return blocked("CONSENT_OPT_OUT", f"Customer withdrew consent for {opt_out.channel} on {opt_out.recorded_at.date() if opt_out.recorded_at else 'record'} ({opt_out.source}).")
        if getattr(customer, "dnd_enabled", False):
            return blocked("CUSTOMER_DND_ENABLED", "Customer has an explicit do-not-disturb flag.")
        if channel == "WHATSAPP" and getattr(customer, "whatsapp_allowed", True) is False:
            return blocked("WHATSAPP_OPT_OUT", "Customer has opted out of WhatsApp communications.")
        if channel in ("WHATSAPP", "SMS", "VOICE") and getattr(customer, "transactional_allowed", True) is False:
            return blocked("TRANSACTIONAL_NOT_ALLOWED", "Customer has not permitted transactional messages or calls.")
        checks.append("consent:ok")

        # 3. TRAI DND registry (voice and SMS; WhatsApp transactional is permitted)
        if channel in ("VOICE", "SMS") and get_dnd_registry().is_registered(getattr(customer, "phone", None)):
            return blocked("TRAI_DND_REGISTERED", f"Number is on the TRAI DND registry; {channel.lower()} contact is not permitted.")
        checks.append("dnd:ok")

        # 4. Contact window + holidays in the customer's timezone
        window = cls._window_for(channel)
        if window:
            tz = cls._tz(customer)
            local = now.astimezone(tz)
            start, end = window
            holidays = cls._holidays() if channel == "VOICE" else set()
            if local.date() in holidays:
                return blocked("HOLIDAY_NO_CONTACT", f"No recovery calls on {local.date().isoformat()} (national holiday).", cls._next_window_open(local, start, end, holidays).astimezone(timezone.utc))
            if not (start <= local.hour < end):
                nxt = cls._next_window_open(local, start, end, holidays)
                if local.hour < start:
                    nxt = local.replace(hour=start, minute=0, second=0, microsecond=0)
                return blocked(
                    "CONTACT_WINDOW_CLOSED",
                    f"{channel} contact is permitted {start:02d}:00-{end:02d}:00 {tz.key}; local time is {local.strftime('%H:%M')}.",
                    nxt.astimezone(timezone.utc),
                )
        checks.append("window:ok")

        # 5. Cross-channel frequency caps (SENT attempts only)
        since_7d = now - timedelta(days=7)
        touches_7d = int(db.scalar(select(func.count(ContactAttempt.id)).where(ContactAttempt.customer_id == customer.id, ContactAttempt.outcome == "SENT", ContactAttempt.occurred_at >= since_7d)) or 0)
        if touches_7d >= settings.CONTACT_MAX_TOUCHES_7D:
            oldest = db.scalar(select(func.min(ContactAttempt.occurred_at)).where(ContactAttempt.customer_id == customer.id, ContactAttempt.outcome == "SENT", ContactAttempt.occurred_at >= since_7d))
            nxt = (_aware(oldest) + timedelta(days=7)) if oldest else None
            return blocked("CONTACT_FREQUENCY_CAP", f"Customer already received {touches_7d} touches in the last 7 days (cap {settings.CONTACT_MAX_TOUCHES_7D}).", nxt)
        if channel == "VOICE":
            since_3d = now - timedelta(days=3)
            calls_3d = int(db.scalar(select(func.count(ContactAttempt.id)).where(ContactAttempt.customer_id == customer.id, ContactAttempt.channel == "VOICE", ContactAttempt.outcome == "SENT", ContactAttempt.occurred_at >= since_3d)) or 0)
            if calls_3d >= settings.CONTACT_MAX_VOICE_3D:
                last_call = db.scalar(select(func.max(ContactAttempt.occurred_at)).where(ContactAttempt.customer_id == customer.id, ContactAttempt.channel == "VOICE", ContactAttempt.outcome == "SENT"))
                return blocked("VOICE_FREQUENCY_CAP", f"Customer already received {calls_3d} recovery call(s) in the last 3 days (cap {settings.CONTACT_MAX_VOICE_3D}).", (_aware(last_call) + timedelta(days=3)) if last_call else None)
        last_touch = db.scalar(select(func.max(ContactAttempt.occurred_at)).where(ContactAttempt.customer_id == customer.id, ContactAttempt.outcome == "SENT"))
        if last_touch:
            gap = now - _aware(last_touch)
            min_gap = timedelta(hours=settings.CONTACT_MIN_GAP_HOURS)
            if gap < min_gap:
                return blocked("CONTACT_MIN_GAP", f"Last touch was {gap.total_seconds() / 3600:.1f}h ago; minimum gap between touches is {settings.CONTACT_MIN_GAP_HOURS}h.", _aware(last_touch) + min_gap)
        checks.append("frequency:ok")

        return ComplianceDecision(True, channel, None, "Contact is compliant with consent, DND, window and frequency rules.", None, checks)

    # ------------------------------------------------------------------ registry

    @classmethod
    def _record(cls, db: Session, *, customer: Customer, case: Optional[RecoveryCase], channel: str, outcome: str, rule: Optional[str], reason: str, now: datetime, reference: Optional[str] = None) -> ContactAttempt:
        row = ContactAttempt(
            customer_id=customer.id, recovery_case_id=case.id if case else None, channel=channel.upper(), outcome=outcome,
            blocking_rule=rule, reference=reference, occurred_at=now, attempt_metadata={"reason": reason},
        )
        db.add(row)
        db.flush()
        return row

    @classmethod
    def record_sent(cls, db: Session, *, case: RecoveryCase, channel: str, reference: str, now: Optional[datetime] = None) -> Optional[ContactAttempt]:
        """Register a successful customer touch (call the same place the cost is posted)."""
        customer = case.customer
        if customer is None:
            return None
        existing = db.scalar(select(ContactAttempt).where(ContactAttempt.reference == reference, ContactAttempt.channel == channel.upper(), ContactAttempt.outcome == "SENT"))
        if existing:
            return existing
        return cls._record(db, customer=customer, case=case, channel=channel, outcome="SENT", rule=None, reason="sent", now=_aware(now) or datetime.now(timezone.utc), reference=reference)

    @classmethod
    def touches(cls, db: Session, customer_id, days: int = 30) -> List[Dict[str, Any]]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = db.scalars(select(ContactAttempt).where(ContactAttempt.customer_id == customer_id, ContactAttempt.occurred_at >= since).order_by(ContactAttempt.occurred_at.desc())).all()
        return [{"channel": r.channel, "outcome": r.outcome, "rule": r.blocking_rule, "reference": r.reference, "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None, "case_id": str(r.recovery_case_id) if r.recovery_case_id else None} for r in rows]

    @classmethod
    def blocks_by_rule(cls, db: Session, case_ids: Optional[List] = None) -> Dict[str, int]:
        stmt = select(ContactAttempt.blocking_rule, func.count(ContactAttempt.id)).where(ContactAttempt.outcome == "BLOCKED")
        if case_ids is not None:
            stmt = stmt.where(ContactAttempt.recovery_case_id.in_(case_ids))
        return {str(rule): int(n) for rule, n in db.execute(stmt.group_by(ContactAttempt.blocking_rule)).all()}
