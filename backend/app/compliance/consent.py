"""Consent ledger and inbound opt-out handling.

A customer can withdraw consent in three ways and all three must stop outreach immediately:
1. a keyword in an inbound message (STOP, UNSUBSCRIBE, Hindi/Hinglish variants),
2. a voice intent ("do not call me", wrong number, dispute, human request),
3. an operator recording it on the customer's behalf.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.consent_record import ConsentRecord
from app.models.customer import Customer
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)

CHANNELS = ("WHATSAPP", "SMS", "EMAIL", "VOICE", "ALL")

# Keyword -> channel scope. "ALL" withdraws consent for every channel.
OPT_OUT_PATTERNS: List[tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*(stop|unsubscribe|cancel|quit|end|opt\s*out)\s*$", re.I), "ALL"),
    (re.compile(r"\b(stop|unsubscribe|opt\s*out)\b.*\b(message|messages|msg|whatsapp|sms|text)", re.I), "WHATSAPP"),
    (re.compile(r"\b(stop|do\s*not|don't|dont|no\s*more)\b.*\b(call|calling|calls|phone)", re.I), "VOICE"),
    (re.compile(r"\b(stop|do\s*not|don't|dont)\b.*\b(email|mail)", re.I), "EMAIL"),
    (re.compile(r"\b(do\s*not|don't|dont|never)\s+(contact|disturb|message|msg)\b", re.I), "ALL"),
    (re.compile(r"\b(mat|matt)\s+(bhejo|karo|call|message|msg)\b", re.I), "ALL"),          # Hinglish: "message mat bhejo"
    (re.compile(r"\b(band|bandh)\s+(karo|kar\s*do|kardo)\b", re.I), "ALL"),                 # Hinglish: "band karo"
    (re.compile(r"\b(pareshan|disturb)\s+(mat|na)\s+(karo|kar)\b", re.I), "ALL"),
    (re.compile(r"\bremove\s+(me|my\s+number)\b", re.I), "ALL"),
    (re.compile(r"\bwrong\s+number\b", re.I), "ALL"),
]


class ConsentService:
    """Reads and writes the consent ledger and enforces its side effects."""

    @classmethod
    def latest(cls, db: Session, customer_id: uuid.UUID, channel: str) -> Optional[ConsentRecord]:
        return db.scalar(
            select(ConsentRecord)
            .where(ConsentRecord.customer_id == customer_id, ConsentRecord.channel.in_([channel.upper(), "ALL"]))
            .order_by(ConsentRecord.recorded_at.desc(), ConsentRecord.id.desc())
        )

    @classmethod
    def is_opted_out(cls, db: Session, customer_id: uuid.UUID, channel: str) -> Optional[ConsentRecord]:
        """Return the governing OPT_OUT record if the channel (or ALL) is opted out, else None."""
        rec = cls.latest(db, customer_id, channel)
        return rec if rec and rec.status == "OPT_OUT" else None

    @classmethod
    def record(
        cls,
        db: Session,
        *,
        customer: Customer,
        channel: str,
        status: str,
        source: str,
        reason: Optional[str] = None,
        case: Optional[RecoveryCase] = None,
        metadata: Optional[Dict[str, Any]] = None,
        recorded_at: Optional[datetime] = None,
    ) -> ConsentRecord:
        channel = channel.upper()
        status = status.upper()
        if channel not in CHANNELS:
            raise ValueError(f"Unknown consent channel '{channel}'.")
        if status not in ("OPT_IN", "OPT_OUT"):
            raise ValueError(f"Unknown consent status '{status}'.")
        rec = ConsentRecord(
            customer_id=customer.id, channel=channel, status=status, source=source, reason=reason,
            recovery_case_id=case.id if case else None, consent_metadata=metadata or {},
            recorded_at=recorded_at or datetime.now(timezone.utc),
        )
        db.add(rec)
        db.flush()

        # Mirror onto legacy customer flags so v1 code paths agree with the ledger.
        if status == "OPT_OUT":
            if channel in ("WHATSAPP", "ALL"):
                customer.whatsapp_allowed = False
            if channel == "ALL":
                customer.dnd_enabled = True
        elif status == "OPT_IN":
            if channel in ("WHATSAPP", "ALL"):
                customer.whatsapp_allowed = True
            if channel == "ALL":
                customer.dnd_enabled = False

        db.add(AuditLog(
            recovery_case_id=case.id if case else None, actor_type="CUSTOMER" if source in ("CUSTOMER_KEYWORD", "VOICE_INTENT") else "OPERATOR" if source == "OPERATOR" else "SYSTEM",
            actor_id=source.lower(), action="CONSENT_RECORDED", entity_type="ConsentRecord", entity_id=str(rec.id),
            audit_metadata={"channel": channel, "status": status, "source": source, "reason": reason},
        ))

        if status == "OPT_OUT":
            cls._stop_outreach(db, customer, channel, reason or f"Customer opted out of {channel}", case)
        db.flush()
        return rec

    @classmethod
    def _stop_outreach(cls, db: Session, customer: Customer, channel: str, reason: str, case: Optional[RecoveryCase]) -> None:
        """Pause every active plan for the customer when consent is withdrawn for ALL (or freeze the channel)."""
        from app.services.recovery_scheduler import RecoveryScheduler

        cases = db.scalars(select(RecoveryCase).where(RecoveryCase.customer_id == customer.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"]))).all()
        for c in cases:
            meta = dict(c.case_metadata or {})
            blocked = set(meta.get("blocked_channels", []))
            blocked.add(channel)
            meta["blocked_channels"] = sorted(blocked)
            if channel == "ALL":
                meta["consent_withdrawn"] = True
                RecoveryScheduler.pause_plan(db, case_id=c.id, reason=f"CONSENT_OPT_OUT: {reason}")
            c.case_metadata = meta
            db.add(AuditLog(
                recovery_case_id=c.id, actor_type="SYSTEM", actor_id="consent_service_v1", action="OUTREACH_STOPPED_CONSENT_WITHDRAWN",
                entity_type="RecoveryCase", entity_id=str(c.id), audit_metadata={"channel": channel, "reason": reason},
            ))

    @classmethod
    def detect_opt_out(cls, text: Optional[str]) -> Optional[str]:
        """Return the channel scope withdrawn by this inbound text, or None."""
        if not text:
            return None
        for pattern, scope in OPT_OUT_PATTERNS:
            if pattern.search(text):
                return scope
        return None

    @classmethod
    def process_inbound_text(cls, db: Session, *, customer: Customer, text: str, channel: str, case: Optional[RecoveryCase] = None) -> Optional[ConsentRecord]:
        """Apply an inbound customer message: opt-out keywords write the ledger and stop outreach."""
        scope = cls.detect_opt_out(text)
        if not scope:
            return None
        # A STOP received on WhatsApp with no wider scope applies to that channel.
        effective = channel.upper() if scope == "WHATSAPP" and channel.upper() in ("WHATSAPP", "SMS") else scope
        if scope == "ALL" and re.match(r"^\s*(stop|unsubscribe|cancel|quit|end|opt\s*out)\s*$", text or "", re.I):
            effective = channel.upper() if channel.upper() in ("WHATSAPP", "SMS", "EMAIL") else "ALL"
        return cls.record(
            db, customer=customer, channel=effective, status="OPT_OUT", source="CUSTOMER_KEYWORD",
            reason=f"Inbound {channel.upper()} message: {text.strip()[:120]}", case=case, metadata={"raw_text": text[:500], "inbound_channel": channel.upper()},
        )

    @classmethod
    def summary(cls, db: Session, customer_id: uuid.UUID) -> Dict[str, Any]:
        rows = db.scalars(select(ConsentRecord).where(ConsentRecord.customer_id == customer_id).order_by(ConsentRecord.recorded_at.asc())).all()
        effective: Dict[str, str] = {}
        for ch in ("WHATSAPP", "SMS", "EMAIL", "VOICE"):
            rec = cls.latest(db, customer_id, ch)
            effective[ch] = rec.status if rec else "OPT_IN"
        return {
            "customer_id": str(customer_id),
            "effective": effective,
            "history": [
                {"id": str(r.id), "channel": r.channel, "status": r.status, "source": r.source, "reason": r.reason, "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None}
                for r in rows
            ],
        }
