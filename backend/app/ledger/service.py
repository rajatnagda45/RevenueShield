"""Append-only recovery ledger service."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ledger_entry import LedgerEntry
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


class LedgerEntryType(str, Enum):
    AT_RISK = "AT_RISK"                     # amount put at risk when the case opened
    RECOVERED_CAPTURE = "RECOVERED_CAPTURE"  # full capture verified by gateway webhook
    RECOVERED_PARTIAL = "RECOVERED_PARTIAL"  # partial capture (instalment / partial payment link)
    REFUNDED = "REFUNDED"                   # captured money returned to the customer
    SETTLED = "SETTLED"                     # money confirmed in the merchant's bank account
    WRITTEN_OFF = "WRITTEN_OFF"             # case closed without recovery
    COST = "COST"                           # cost of one outreach action


RECOVERY_TYPES = (LedgerEntryType.RECOVERED_CAPTURE.value, LedgerEntryType.RECOVERED_PARTIAL.value)


class LedgerService:
    """Records and aggregates immutable ledger entries per recovery case."""

    _unit_cost_cache: Optional[Dict[str, Decimal]] = None

    @classmethod
    def unit_costs(cls) -> Dict[str, Decimal]:
        """Per-channel unit costs in INR, loaded once from settings (JSON)."""
        if cls._unit_cost_cache is None:
            defaults = {
                "EMAIL": Decimal("0.05"),
                "WHATSAPP": Decimal("0.80"),
                "SMS": Decimal("0.25"),
                "VOICE": Decimal("15.00"),
                "PAYMENT_LINK": Decimal("0.00"),
                "GATEWAY": Decimal("0.00"),
            }
            raw = (settings.CHANNEL_UNIT_COST_INR or "").strip()
            if raw:
                try:
                    for k, v in json.loads(raw).items():
                        defaults[str(k).upper()] = Decimal(str(v))
                except (ValueError, TypeError):
                    logger.warning("[LEDGER_CONFIG] CHANNEL_UNIT_COST_INR is not valid JSON; using defaults.")
            cls._unit_cost_cache = defaults
        return cls._unit_cost_cache

    @classmethod
    def reset_cache(cls) -> None:
        cls._unit_cost_cache = None

    @classmethod
    def record(
        cls,
        db: Session,
        case_id: uuid.UUID,
        entry_type: LedgerEntryType | str,
        amount: Decimal | float | int,
        provider_reference: str,
        currency: str = "INR",
        channel: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEntry:
        """Idempotently append an entry. The (case, type, reference) triple is the natural key."""
        etype = entry_type.value if isinstance(entry_type, LedgerEntryType) else str(entry_type)
        existing = db.scalar(
            select(LedgerEntry).where(
                LedgerEntry.recovery_case_id == case_id,
                LedgerEntry.entry_type == etype,
                LedgerEntry.provider_reference == provider_reference,
            )
        )
        if existing:
            return existing

        when = occurred_at or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)

        entry = LedgerEntry(
            recovery_case_id=case_id,
            entry_type=etype,
            amount=Decimal(str(amount)).quantize(Decimal("0.01")),
            currency=(currency or "INR").upper(),
            channel=channel.upper() if channel else None,
            provider_reference=provider_reference,
            occurred_at=when,
            entry_metadata=metadata or {},
        )
        try:
            # Savepoint: a duplicate natural key must not roll back the caller's transaction.
            with db.begin_nested():
                db.add(entry)
                db.flush()
        except IntegrityError:
            # Concurrent writer inserted the same natural key; return theirs.
            return db.scalar(
                select(LedgerEntry).where(
                    LedgerEntry.recovery_case_id == case_id,
                    LedgerEntry.entry_type == etype,
                    LedgerEntry.provider_reference == provider_reference,
                )
            )
        try:
            from app.observability.metrics import metrics
            metrics.ledger(etype, float(entry.amount))
        except Exception:
            pass
        return entry

    # ---- convenience posters -------------------------------------------------

    @classmethod
    def post_at_risk(cls, db: Session, case: RecoveryCase, provider_reference: str) -> LedgerEntry:
        return cls.record(
            db, case.id, LedgerEntryType.AT_RISK, case.amount_at_risk or Decimal("0.00"),
            provider_reference=provider_reference, currency=case.currency or "INR",
            occurred_at=case.created_at, metadata={"case_type": case.case_type, "leak_surface": case.leak_surface},
        )

    @classmethod
    def post_capture(
        cls, db: Session, case: RecoveryCase, amount: Decimal, provider_reference: str,
        occurred_at: Optional[datetime] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEntry:
        at_risk = case.amount_at_risk or Decimal("0.00")
        etype = LedgerEntryType.RECOVERED_CAPTURE if amount >= at_risk else LedgerEntryType.RECOVERED_PARTIAL
        return cls.record(
            db, case.id, etype, amount, provider_reference=provider_reference,
            currency=case.currency or "INR", occurred_at=occurred_at, metadata=metadata,
        )

    @classmethod
    def post_refund(
        cls, db: Session, case: RecoveryCase, amount: Decimal, provider_reference: str,
        occurred_at: Optional[datetime] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEntry:
        return cls.record(
            db, case.id, LedgerEntryType.REFUNDED, amount, provider_reference=provider_reference,
            currency=case.currency or "INR", occurred_at=occurred_at, metadata=metadata,
        )

    @classmethod
    def post_settlement(
        cls, db: Session, case: RecoveryCase, amount: Decimal, settlement_id: str,
        occurred_at: Optional[datetime] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEntry:
        return cls.record(
            db, case.id, LedgerEntryType.SETTLED, amount, provider_reference=settlement_id,
            currency=case.currency or "INR", occurred_at=occurred_at, metadata=metadata,
        )

    @classmethod
    def post_write_off(cls, db: Session, case: RecoveryCase, reason: str) -> LedgerEntry:
        return cls.record(
            db, case.id, LedgerEntryType.WRITTEN_OFF, case.amount_at_risk or Decimal("0.00"),
            provider_reference=f"writeoff_{case.id}", currency=case.currency or "INR",
            metadata={"reason": reason},
        )

    @classmethod
    def post_cost(
        cls, db: Session, case: RecoveryCase, channel: str, provider_reference: str,
        amount: Optional[Decimal] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEntry:
        """Post the unit cost of one outreach. Reference = communication/call/link id."""
        cost = amount if amount is not None else cls.unit_costs().get(channel.upper(), Decimal("0.00"))
        return cls.record(
            db, case.id, LedgerEntryType.COST, cost, provider_reference=provider_reference,
            currency="INR", channel=channel, metadata=metadata or {},
        )

    # ---- readers ---------------------------------------------------------------

    @classmethod
    def entries_for_case(cls, db: Session, case_id: uuid.UUID) -> List[LedgerEntry]:
        return list(
            db.scalars(
                select(LedgerEntry).where(LedgerEntry.recovery_case_id == case_id).order_by(LedgerEntry.occurred_at.asc())
            ).all()
        )

    @classmethod
    def case_balance(cls, db: Session, case_id: uuid.UUID) -> Dict[str, Any]:
        """Roll-up per entry type plus derived net figures."""
        rows = db.execute(
            select(LedgerEntry.entry_type, func.coalesce(func.sum(LedgerEntry.amount), 0))
            .where(LedgerEntry.recovery_case_id == case_id)
            .group_by(LedgerEntry.entry_type)
        ).all()
        totals = {t.value: Decimal("0.00") for t in LedgerEntryType}
        for etype, total in rows:
            totals[etype] = Decimal(str(total or 0))
        recovered = totals[LedgerEntryType.RECOVERED_CAPTURE.value] + totals[LedgerEntryType.RECOVERED_PARTIAL.value]
        net_recovered = recovered - totals[LedgerEntryType.REFUNDED.value]
        return {
            "case_id": str(case_id),
            "totals": {k: float(v) for k, v in totals.items()},
            "recovered_gross": float(recovered),
            "recovered_net_of_refunds": float(net_recovered),
            "settled": float(totals[LedgerEntryType.SETTLED.value]),
            "cost": float(totals[LedgerEntryType.COST.value]),
            "outstanding": float(max(totals[LedgerEntryType.AT_RISK.value] - net_recovered, Decimal("0.00"))),
        }
