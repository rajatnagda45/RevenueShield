"""Subscription mandate failures and compliant retry sequencing.

Razorpay moves a subscription to `pending` on a failed charge and retries daily; after the retries
are exhausted it becomes `halted`. RBI's recurring-payment framework requires a pre-debit
notification at least 24 hours before each debit. Our sequencer therefore does not "hammer" the
mandate: it schedules the next attempt at a salary-friendly, quiet-hour-safe time, records the
notification deadline, holds retries while the issuer is degraded, and caps attempts.

`subscription.charged` / `activated` / `resumed` close the case; `halted` switches the playbook to
re-authorisation (a fresh authorisation link), because a halted mandate cannot be retried.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.detectors.case_factory import CaseFactory
from app.domain.surfaces import LeakSurface, profile_for
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.event import Event
from app.models.mandate_retry import MandateRetry
from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.models.subscription import Subscription
from app.outcomes.engine import OutcomeEngine
from app.schemas.event import NormalizedEvent

logger = logging.getLogger(__name__)

MAX_MANDATE_RETRIES = 3
SALARY_DAYS = {1, 2, 3, 4, 5, 28, 29, 30, 31}
RETRY_LOCAL_HOUR = 10  # 10:00 local: inside RBI contact window, after most salary credits land


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _epoch(ts: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
    except Exception:
        return None


class MandateRetrySequencer:
    """Decides *when* the next debit attempt (and its pre-debit notice) should happen."""

    @classmethod
    def next_retry_at(
        cls,
        now: datetime,
        *,
        timezone_name: str = "Asia/Kolkata",
        pre_debit_notice_hours: int = 24,
        prefer_salary_days: bool = True,
        max_wait_days: int = 7,
        held_until: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return the scheduled debit time, the notification deadline and the reasoning."""
        try:
            tz = ZoneInfo(timezone_name or "Asia/Kolkata")
        except Exception:
            tz = ZoneInfo("Asia/Kolkata")
        reasons: List[str] = []

        earliest = now + timedelta(hours=pre_debit_notice_hours)
        reasons.append(f"earliest debit = now + {pre_debit_notice_hours}h pre-debit notice (RBI recurring-payment framework)")
        if held_until and _aware(held_until) > earliest:
            earliest = _aware(held_until)
            reasons.append(f"issuer degradation hold until {earliest.isoformat()}")

        local_earliest = earliest.astimezone(tz)
        candidate = local_earliest.replace(hour=RETRY_LOCAL_HOUR, minute=0, second=0, microsecond=0)
        if candidate < local_earliest:
            candidate += timedelta(days=1)
        reasons.append(f"aligned to {RETRY_LOCAL_HOUR:02d}:00 {timezone_name} (inside contact window, after salary credits)")

        if prefer_salary_days and candidate.day not in SALARY_DAYS:
            probe = candidate
            for _ in range(max_wait_days):
                probe += timedelta(days=1)
                if probe.day in SALARY_DAYS:
                    candidate = probe
                    reasons.append(f"moved to salary-day window (day {probe.day}) within {max_wait_days} days")
                    break
            else:
                reasons.append("no salary-day within the wait window; keeping earliest compliant slot")

        scheduled_at = candidate.astimezone(timezone.utc)
        notify_by = scheduled_at - timedelta(hours=pre_debit_notice_hours)
        return {"scheduled_at": scheduled_at, "notify_by": notify_by, "reasons": reasons}

    @classmethod
    def schedule_next(
        cls,
        db: Session,
        *,
        case: RecoveryCase,
        subscription: Optional[Subscription],
        now: Optional[datetime] = None,
        held_until: Optional[datetime] = None,
    ) -> Optional[MandateRetry]:
        now = now or datetime.now(timezone.utc)
        existing = db.scalars(select(MandateRetry).where(MandateRetry.recovery_case_id == case.id).order_by(MandateRetry.attempt_number.asc())).all()
        if any(r.status in ("SCHEDULED", "NOTIFIED", "HELD") for r in existing):
            return next(r for r in existing if r.status in ("SCHEDULED", "NOTIFIED", "HELD"))
        attempt_number = len(existing) + 1
        if attempt_number > MAX_MANDATE_RETRIES:
            db.add(AuditLog(
                recovery_case_id=case.id, actor_type="SYSTEM", actor_id="mandate_retry_sequencer_v1",
                action="MANDATE_RETRY_CAP_REACHED", entity_type="RecoveryCase", entity_id=str(case.id),
                audit_metadata={"max_retries": MAX_MANDATE_RETRIES},
            ))
            db.flush()
            return None

        customer_tz = (case.customer.timezone if case.customer and case.customer.timezone else settings.DEFAULT_TIMEZONE)
        profile = profile_for(LeakSurface.SUBSCRIPTION_MANDATE_FAILURE.value)
        plan = cls.next_retry_at(now, timezone_name=customer_tz, pre_debit_notice_hours=profile.pre_debit_notice_hours, held_until=held_until)

        retry = MandateRetry(
            recovery_case_id=case.id,
            subscription_id=subscription.id if subscription else None,
            attempt_number=attempt_number,
            scheduled_at=plan["scheduled_at"],
            notify_by=plan["notify_by"],
            status="HELD" if held_until else "SCHEDULED",
            reason="; ".join(plan["reasons"]),
            retry_metadata={"timezone": customer_tz, "held_until": _aware(held_until).isoformat() if held_until else None},
        )
        db.add(retry)
        db.flush()
        db.add(AuditLog(
            recovery_case_id=case.id, actor_type="SYSTEM", actor_id="mandate_retry_sequencer_v1",
            action="MANDATE_RETRY_SCHEDULED", entity_type="MandateRetry", entity_id=str(retry.id),
            audit_metadata={"attempt_number": attempt_number, "scheduled_at": retry.scheduled_at.isoformat(), "notify_by": retry.notify_by.isoformat(), "reasons": plan["reasons"]},
        ))
        db.flush()
        return retry

    @classmethod
    def due_notifications(cls, db: Session, now: Optional[datetime] = None) -> List[MandateRetry]:
        now = now or datetime.now(timezone.utc)
        return list(db.scalars(select(MandateRetry).where(MandateRetry.status == "SCHEDULED", MandateRetry.notify_by <= now)).all())

    @classmethod
    def due_executions(cls, db: Session, now: Optional[datetime] = None) -> List[MandateRetry]:
        now = now or datetime.now(timezone.utc)
        return list(db.scalars(select(MandateRetry).where(MandateRetry.status.in_(["SCHEDULED", "NOTIFIED"]), MandateRetry.scheduled_at <= now)).all())

    @classmethod
    def sweep(cls, db: Session, now: Optional[datetime] = None, dry_run: bool = True) -> Dict[str, Any]:
        """Send due pre-debit notices and mark due retries executed (the gateway reports the outcome)."""
        now = now or datetime.now(timezone.utc)
        notified, executed, cancelled = [], [], []
        for r in cls.due_notifications(db, now):
            case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == r.recovery_case_id))
            if case is None or case.status in ("RECOVERED", "CLOSED") or case.experiment_arm == "HOLDOUT":
                r.status = "CANCELLED"
                r.reason = (r.reason or "") + " | cancelled: case closed or holdout"
                cancelled.append(str(r.id))
                continue
            r.status = "NOTIFIED"
            r.notified_at = now
            notified.append(str(r.id))
            db.add(AuditLog(
                recovery_case_id=case.id, actor_type="SYSTEM", actor_id="mandate_retry_sequencer_v1",
                action="PRE_DEBIT_NOTIFICATION_SENT", entity_type="MandateRetry", entity_id=str(r.id),
                audit_metadata={"scheduled_at": _aware(r.scheduled_at).isoformat(), "dry_run": dry_run},
            ))
        for r in cls.due_executions(db, now):
            case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == r.recovery_case_id))
            if case is None or case.status in ("RECOVERED", "CLOSED") or case.experiment_arm == "HOLDOUT":
                r.status = "CANCELLED"
                cancelled.append(str(r.id))
                continue
            if r.status == "SCHEDULED":
                # Notification deadline was missed (worker downtime): never debit without notice; reschedule.
                r.status = "CANCELLED"
                r.reason = (r.reason or "") + " | cancelled: pre-debit notice not sent in time; rescheduled"
                cancelled.append(str(r.id))
                cls.schedule_next(db, case=case, subscription=r.subscription, now=now)
                continue
            r.status = "EXECUTED"
            r.executed_at = now
            executed.append(str(r.id))
            db.add(AuditLog(
                recovery_case_id=case.id, actor_type="SYSTEM", actor_id="mandate_retry_sequencer_v1",
                action="MANDATE_RETRY_EXECUTED", entity_type="MandateRetry", entity_id=str(r.id),
                audit_metadata={"attempt_number": r.attempt_number, "dry_run": dry_run},
            ))
        db.flush()
        return {"reference_time": now.isoformat(), "notified": notified, "executed": executed, "cancelled": cancelled}


class MandateDetector:
    """Opens/closes SUBSCRIPTION_MANDATE_FAILURE cases from subscription webhooks."""

    @classmethod
    def upsert_subscription(cls, db: Session, *, event: NormalizedEvent, customer: Customer) -> Subscription:
        meta = (event.metadata or {}).get("subscription", {}) or {}
        sub = db.scalar(select(Subscription).where(Subscription.external_subscription_id == event.external_subscription_id))
        now = datetime.now(timezone.utc)
        current_start = _epoch(meta.get("current_start")) or now
        current_end = _epoch(meta.get("current_end")) or (current_start + timedelta(days=30))
        if sub is None:
            sub = Subscription(
                external_subscription_id=event.external_subscription_id, customer_id=customer.id,
                amount=event.amount or Decimal("0.00"), currency=event.currency or "INR",
                status=str(meta.get("status") or "ACTIVE").upper(), current_period_start=current_start, current_period_end=current_end,
            )
            db.add(sub)
        else:
            if event.amount and event.amount > 0:
                sub.amount = event.amount
            sub.status = str(meta.get("status") or sub.status).upper()
        sub.plan_id = meta.get("plan_id") or sub.plan_id
        sub.charge_at = _epoch(meta.get("charge_at")) or sub.charge_at
        sub.auth_attempts = int(meta.get("auth_attempts") or sub.auth_attempts or 0)
        sub.paid_count = int(meta.get("paid_count") or sub.paid_count or 0)
        sub.remaining_count = meta.get("remaining_count", sub.remaining_count)
        sub.payment_method = (meta.get("payment_method") or event.payment_method or sub.payment_method or "").upper() or None
        sub.mandate_type = sub.mandate_type or cls._mandate_type(sub.payment_method)
        sub.short_url = meta.get("short_url") or sub.short_url
        if sub.status == "HALTED" and not sub.halted_at:
            sub.halted_at = event.occurred_at or now
        db.flush()
        return sub

    @staticmethod
    def _mandate_type(method: Optional[str]) -> Optional[str]:
        m = (method or "").upper()
        if m == "UPI":
            return "UPI_AUTOPAY"
        if m in ("EMANDATE", "NETBANKING", "NACH"):
            return "EMANDATE"
        if m == "CARD":
            return "CARD"
        return None

    @classmethod
    def _open_case(cls, db: Session, *, event: NormalizedEvent, customer: Customer, subscription: Subscription, db_event: Event, payment: Optional[Payment]) -> RecoveryCase:
        existing = db.scalar(select(RecoveryCase).where(RecoveryCase.subscription_id == subscription.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"])))
        if existing:
            return existing
        amount = event.amount if event.amount and event.amount > 0 else subscription.amount
        case = CaseFactory.open_case(
            db, customer=customer, db_event=db_event, surface=LeakSurface.SUBSCRIPTION_MANDATE_FAILURE, amount=amount,
            currency=event.currency or subscription.currency, diagnosis_event=event, subscription=subscription, payment=payment,
            batch_id=event.batch_id,
            metadata={
                "subscription_id": subscription.external_subscription_id, "plan_id": subscription.plan_id,
                "mandate_type": subscription.mandate_type, "payment_method": subscription.payment_method,
                "auth_attempts": subscription.auth_attempts, "gateway_charge_at": subscription.charge_at.isoformat() if subscription.charge_at else None,
                "subscription_status": subscription.status, "halted": subscription.status == "HALTED",
            },
            actor_id="mandate_detector_v1", ledger_reference=event.external_payment_id or subscription.external_subscription_id,
        )
        return case

    @classmethod
    def on_subscription_pending(cls, db: Session, *, event: NormalizedEvent, customer: Customer, db_event: Event, payment: Optional[Payment]) -> RecoveryCase:
        sub = cls.upsert_subscription(db, event=event, customer=customer)
        sub.status = "PENDING"
        case = cls._open_case(db, event=event, customer=customer, subscription=sub, db_event=db_event, payment=payment)
        MandateRetrySequencer.schedule_next(db, case=case, subscription=sub, now=event.occurred_at)
        return case

    @classmethod
    def on_subscription_halted(cls, db: Session, *, event: NormalizedEvent, customer: Customer, db_event: Event, payment: Optional[Payment]) -> RecoveryCase:
        sub = cls.upsert_subscription(db, event=event, customer=customer)
        sub.status = "HALTED"
        sub.halted_at = sub.halted_at or event.occurred_at
        case = cls._open_case(db, event=event, customer=customer, subscription=sub, db_event=db_event, payment=payment)
        meta = dict(case.case_metadata or {})
        meta.update({"halted": True, "subscription_status": "HALTED", "playbook": "REAUTHORISE_MANDATE"})
        case.case_metadata = meta
        # A halted mandate cannot be retried: cancel pending retries, the playbook is re-authorisation.
        for r in db.scalars(select(MandateRetry).where(MandateRetry.recovery_case_id == case.id, MandateRetry.status.in_(["SCHEDULED", "NOTIFIED", "HELD"]))).all():
            r.status = "CANCELLED"
            r.reason = (r.reason or "") + " | cancelled: subscription halted, re-authorisation required"
        db.add(AuditLog(
            recovery_case_id=case.id, actor_type="SYSTEM", actor_id="mandate_detector_v1", action="MANDATE_HALTED",
            entity_type="Subscription", entity_id=sub.external_subscription_id,
            audit_metadata={"auth_attempts": sub.auth_attempts, "playbook": "REAUTHORISE_MANDATE"},
        ))
        db.flush()
        return case

    @classmethod
    def on_subscription_recovered(cls, db: Session, *, event: NormalizedEvent, customer: Customer) -> Optional[RecoveryCase]:
        """`subscription.charged` / `activated` / `resumed`: the mandate works again."""
        sub = cls.upsert_subscription(db, event=event, customer=customer)
        sub.status = "ACTIVE"
        sub.halted_at = None
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.subscription_id == sub.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"])))
        if not case:
            return None
        for r in db.scalars(select(MandateRetry).where(MandateRetry.recovery_case_id == case.id, MandateRetry.status.in_(["SCHEDULED", "NOTIFIED", "HELD", "EXECUTED"]))).all():
            r.status = "SUCCEEDED" if r.status == "EXECUTED" else "CANCELLED"
        captured = event.amount if event.amount and event.amount > 0 else case.amount_at_risk
        OutcomeEngine.process_payment_capture(
            db=db, recovery_case=case, captured_amount=captured, captured_at=event.occurred_at,
            provider_event_id=event.event_id, provider_payment_id=event.external_payment_id,
        )
        return case
