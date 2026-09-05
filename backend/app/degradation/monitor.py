"""Degradation monitor: is this failure the customer's problem, or the bank's?

Every recovery engine in v1 treated a failed payment as a fact about one customer. When HDFC UPI
is down for twenty minutes, that assumption makes the system message hundreds of customers who did
nothing wrong and retry payments that cannot succeed. The monitor watches the (bank, method)
failure matrix over a rolling window against a trailing baseline and opens a DegradationIncident
when a cell spikes. While an incident is SUSPECTED/CONFIRMED:

* affected open cases are tagged `systemic_hold=True` and the PolicyEngine blocks retries and
  outreach with `ISSUER_DEGRADED_HOLD`;
* new failures in that cell are diagnosed `SYSTEMIC_ISSUER_DEGRADATION` instead of a customer cause;
* the merchant gets one incident, not N customer pings.

When the cell recovers (two consecutive clean windows) the incident closes, holds are released and
affected plans are re-evaluated with a small jitter so retries do not all fire in the same second.
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.degradation_incident import DegradationIncident
from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("SUSPECTED", "CONFIRMED")
UNKNOWN_BANK = "UNKNOWN"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DegradationMonitor:
    """Rolling-window anomaly detection over the payment failure matrix."""

    # ------------------------------------------------------------------ measurement

    @classmethod
    def _cell_counts(cls, db: Session, start: datetime, end: datetime) -> Dict[Tuple[str, str], Tuple[int, int]]:
        """(bank, method) -> (total, failed) for payments created in [start, end)."""
        ts = func.coalesce(Payment.razorpay_created_at, Payment.created_at)
        rows = db.execute(
            select(
                func.coalesce(Payment.bank, UNKNOWN_BANK),
                func.upper(Payment.payment_method),
                func.count(Payment.id),
                func.sum(case((Payment.status == "FAILED", 1), else_=0)),
            )
            .where(ts >= start, ts < end)
            .group_by(func.coalesce(Payment.bank, UNKNOWN_BANK), func.upper(Payment.payment_method))
        ).all()
        return {(str(b), str(m)): (int(total or 0), int(failed or 0)) for b, m, total, failed in rows}

    @classmethod
    def health_matrix(cls, db: Session, now: Optional[datetime] = None, window_minutes: Optional[int] = None) -> Dict[str, Any]:
        """Current window vs baseline per cell, with z-scores. Read-only."""
        now = now or datetime.now(timezone.utc)
        window = window_minutes or settings.DEGRADATION_WINDOW_MINUTES
        baseline_days = settings.DEGRADATION_BASELINE_DAYS
        win_start = now - timedelta(minutes=window)
        base_start = win_start - timedelta(days=baseline_days)

        current = cls._cell_counts(db, win_start, now)
        baseline = cls._cell_counts(db, base_start, win_start)
        global_total = sum(t for t, _ in baseline.values())
        global_failed = sum(f for _, f in baseline.values())
        global_rate = (global_failed / global_total) if global_total else settings.DEGRADATION_BASELINE_FLOOR

        cells = []
        for (bank, method), (total, failed) in current.items():
            b_total, b_failed = baseline.get((bank, method), (0, 0))
            if b_total >= settings.DEGRADATION_MIN_BASELINE_SAMPLES:
                base_rate = b_failed / b_total
                base_source = "cell"
            else:
                base_rate = global_rate
                base_source = "global"
            base_rate = max(base_rate, settings.DEGRADATION_BASELINE_FLOOR)
            obs_rate = failed / total if total else 0.0
            se = math.sqrt(base_rate * (1 - base_rate) / total) if total else float("inf")
            z = (obs_rate - base_rate) / se if se > 0 and total else 0.0
            cells.append({
                "bank": bank, "payment_method": method, "total": total, "failed": failed,
                "failure_rate": round(obs_rate, 4), "baseline_failure_rate": round(base_rate, 4), "baseline_source": base_source,
                "baseline_samples": b_total, "z_score": round(z, 3),
                "degraded": bool(total >= settings.DEGRADATION_MIN_SAMPLES and z >= settings.DEGRADATION_Z_THRESHOLD and (obs_rate - base_rate) >= settings.DEGRADATION_MIN_ABS_DELTA),
            })
        cells.sort(key=lambda c: c["z_score"], reverse=True)
        return {"reference_time": now.isoformat(), "window_minutes": window, "baseline_days": baseline_days, "global_baseline_failure_rate": round(global_rate, 4), "cells": cells}

    # ------------------------------------------------------------------ incident state machine

    @classmethod
    def active_incident(cls, db: Session, bank: Optional[str], payment_method: Optional[str]) -> Optional[DegradationIncident]:
        if not payment_method:
            return None
        return db.scalar(
            select(DegradationIncident).where(
                DegradationIncident.bank == (bank or UNKNOWN_BANK),
                DegradationIncident.payment_method == payment_method.upper(),
                DegradationIncident.status.in_(ACTIVE_STATUSES),
            ).order_by(DegradationIncident.opened_at.desc())
        )

    @classmethod
    def case_is_held(cls, db: Session, case: Optional[RecoveryCase]) -> bool:
        """True when the case is tagged to an incident that is still active."""
        if case is None:
            return False
        meta = case.case_metadata or {}
        if not meta.get("systemic_hold"):
            return False
        incident_id = meta.get("systemic_incident_id")
        if not incident_id:
            return False
        inc = db.scalar(select(DegradationIncident).where(DegradationIncident.id == incident_id))
        return bool(inc and inc.status in ACTIVE_STATUSES)

    @classmethod
    def evaluate(cls, db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
        """One monitoring tick: open / confirm / recover / close incidents and tag or release cases."""
        now = now or datetime.now(timezone.utc)
        matrix = cls.health_matrix(db, now=now)
        opened, confirmed, recovering, closed, unchanged = [], [], [], [], []

        active = {(i.bank, i.payment_method): i for i in db.scalars(select(DegradationIncident).where(DegradationIncident.status.in_(("SUSPECTED", "CONFIRMED", "RECOVERING")))).all()}
        seen_cells = set()

        for cell in matrix["cells"]:
            key = (cell["bank"], cell["payment_method"])
            seen_cells.add(key)
            incident = active.get(key)
            if cell["degraded"]:
                if incident is None:
                    incident = DegradationIncident(
                        bank=key[0], payment_method=key[1], status="SUSPECTED", opened_at=now, last_evaluated_at=now,
                        window_minutes=matrix["window_minutes"], observed_total=cell["total"], observed_failures=cell["failed"],
                        observed_failure_rate=cell["failure_rate"], baseline_failure_rate=cell["baseline_failure_rate"], z_score=cell["z_score"],
                        consecutive_detections=1, incident_metadata={"history": [cls._snapshot(cell, now)]},
                    )
                    db.add(incident)
                    db.flush()
                    tagged = cls._tag_cases(db, incident, now)
                    incident.affected_case_count = tagged
                    cls._audit(db, incident, "DEGRADATION_INCIDENT_OPENED", {"cell": cell, "affected_cases": tagged})
                    opened.append(str(incident.id))
                else:
                    incident.consecutive_detections += 1
                    incident.consecutive_clean = 0
                    cls._update_observation(incident, cell, now)
                    if incident.status == "RECOVERING":
                        incident.status = "CONFIRMED"
                        incident.recovering_at = None
                        cls._audit(db, incident, "DEGRADATION_INCIDENT_RELAPSED", {"cell": cell})
                    elif incident.status == "SUSPECTED" and (incident.consecutive_detections >= 2 or cell["z_score"] >= settings.DEGRADATION_Z_THRESHOLD + 2):
                        incident.status = "CONFIRMED"
                        incident.confirmed_at = now
                        cls._audit(db, incident, "DEGRADATION_INCIDENT_CONFIRMED", {"cell": cell})
                        confirmed.append(str(incident.id))
                    incident.affected_case_count = max(incident.affected_case_count, cls._tag_cases(db, incident, now))
                    unchanged.append(str(incident.id))
            elif incident is not None:
                cls._advance_recovery(db, incident, cell, now, recovering, closed)

        # Cells with no traffic this window but an active incident: treat as clean.
        for key, incident in active.items():
            if key not in seen_cells:
                cls._advance_recovery(db, incident, {"total": 0, "failed": 0, "failure_rate": 0.0, "baseline_failure_rate": incident.baseline_failure_rate, "z_score": 0.0}, now, recovering, closed)

        db.flush()
        result = {"reference_time": now.isoformat(), "opened": opened, "confirmed": confirmed, "recovering": recovering, "closed": closed, "active": unchanged, "cells_evaluated": len(matrix["cells"])}
        logger.info(f"[DEGRADATION_TICK] {result}")
        return result

    @classmethod
    def _advance_recovery(cls, db: Session, incident: DegradationIncident, cell: Dict[str, Any], now: datetime, recovering: List[str], closed: List[str]) -> None:
        incident.consecutive_clean += 1
        incident.consecutive_detections = 0
        cls._update_observation(incident, cell, now)
        if incident.status in ("SUSPECTED", "CONFIRMED"):
            incident.status = "RECOVERING"
            incident.recovering_at = now
            cls._audit(db, incident, "DEGRADATION_INCIDENT_RECOVERING", {"cell": cell})
            recovering.append(str(incident.id))
        elif incident.status == "RECOVERING" and incident.consecutive_clean >= settings.DEGRADATION_CLEAN_WINDOWS_TO_CLOSE:
            incident.status = "CLOSED"
            incident.closed_at = now
            released = cls._release_cases(db, incident, now)
            cls._audit(db, incident, "DEGRADATION_INCIDENT_CLOSED", {"cell": cell, "released_cases": released})
            closed.append(str(incident.id))

    @staticmethod
    def _snapshot(cell: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        return {"at": now.isoformat(), "total": cell["total"], "failed": cell["failed"], "failure_rate": cell["failure_rate"], "z": cell["z_score"]}

    @classmethod
    def _update_observation(cls, incident: DegradationIncident, cell: Dict[str, Any], now: datetime) -> None:
        incident.last_evaluated_at = now
        incident.observed_total = int(cell["total"])
        incident.observed_failures = int(cell["failed"])
        incident.observed_failure_rate = float(cell["failure_rate"])
        incident.baseline_failure_rate = float(cell.get("baseline_failure_rate", incident.baseline_failure_rate))
        incident.z_score = float(cell["z_score"])
        meta = dict(incident.incident_metadata or {})
        history = list(meta.get("history", []))
        history.append(cls._snapshot(cell, now))
        meta["history"] = history[-48:]
        incident.incident_metadata = meta

    # ------------------------------------------------------------------ case tagging

    @classmethod
    def _affected_open_cases(cls, db: Session, incident: DegradationIncident) -> List[RecoveryCase]:
        bank_clause = Payment.bank.is_(None) if incident.bank == UNKNOWN_BANK else Payment.bank == incident.bank
        return list(
            db.scalars(
                select(RecoveryCase)
                .join(Payment, Payment.id == RecoveryCase.payment_id)
                .where(
                    RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED"]),
                    func.upper(Payment.payment_method) == incident.payment_method,
                    bank_clause,
                )
            ).all()
        )

    @classmethod
    def _tag_cases(cls, db: Session, incident: DegradationIncident, now: datetime) -> int:
        count = 0
        for case in cls._affected_open_cases(db, incident):
            meta = dict(case.case_metadata or {})
            if meta.get("systemic_incident_id") == str(incident.id) and meta.get("systemic_hold"):
                count += 1
                continue
            meta.update({"systemic_hold": True, "systemic_incident_id": str(incident.id), "systemic_tagged_at": now.isoformat(), "systemic_cell": f"{incident.bank}/{incident.payment_method}"})
            case.case_metadata = meta
            db.add(AuditLog(
                recovery_case_id=case.id, actor_type="SYSTEM", actor_id="degradation_monitor_v1", action="CASE_HELD_SYSTEMIC_INCIDENT",
                entity_type="DegradationIncident", entity_id=str(incident.id), audit_metadata={"cell": f"{incident.bank}/{incident.payment_method}"},
            ))
            count += 1
        db.flush()
        return count

    @classmethod
    def _release_cases(cls, db: Session, incident: DegradationIncident, now: datetime) -> int:
        rng = random.Random(str(incident.id))
        released = 0
        cases = db.scalars(select(RecoveryCase).where(RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED"]))).all()
        for case in cases:
            meta = dict(case.case_metadata or {})
            if meta.get("systemic_incident_id") != str(incident.id) or not meta.get("systemic_hold"):
                continue
            meta["systemic_hold"] = False
            meta["systemic_released_at"] = now.isoformat()
            case.case_metadata = meta
            plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case.id))
            if plan and plan.status in ("WAITING", "ACTIVE"):
                jitter = timedelta(seconds=rng.randint(0, settings.DEGRADATION_RELEASE_JITTER_SECONDS))
                plan.next_evaluation_at = now + jitter
            db.add(AuditLog(
                recovery_case_id=case.id, actor_type="SYSTEM", actor_id="degradation_monitor_v1", action="CASE_RELEASED_SYSTEMIC_INCIDENT",
                entity_type="DegradationIncident", entity_id=str(incident.id), audit_metadata={"next_evaluation_at": plan.next_evaluation_at.isoformat() if plan and plan.next_evaluation_at else None},
            ))
            released += 1
        db.flush()
        return released

    @classmethod
    def _audit(cls, db: Session, incident: DegradationIncident, action: str, metadata: Dict[str, Any]) -> None:
        db.add(AuditLog(
            recovery_case_id=None, actor_type="SYSTEM", actor_id="degradation_monitor_v1", action=action,
            entity_type="DegradationIncident", entity_id=str(incident.id),
            audit_metadata={"bank": incident.bank, "payment_method": incident.payment_method, "status": incident.status, **metadata},
        ))

    # ------------------------------------------------------------------ helpers for other services

    @classmethod
    def systemic_context_for_failure(cls, db: Session, bank: Optional[str], payment_method: Optional[str]) -> Optional[Dict[str, Any]]:
        """If a new failure lands inside an active incident cell, return what to stamp on the case."""
        incident = cls.active_incident(db, bank, payment_method)
        if not incident:
            return None
        return {
            "systemic_hold": True,
            "systemic_incident_id": str(incident.id),
            "systemic_cell": f"{incident.bank}/{incident.payment_method}",
            "systemic_incident_status": incident.status,
        }
