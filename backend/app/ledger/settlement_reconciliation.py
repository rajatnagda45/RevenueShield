"""Settlement reconciliation: prove recovered rupees reached the merchant's bank.

Razorpay's settlement recon (`GET /v1/settlements/recon/combined`) lists every payment that was
part of a settlement together with the settlement id and UTR. We match those payment ids against
ledger RECOVERED_* entries and post a SETTLED entry per case. Captured-but-unsettled money is
visible on the scorecard as the gap between the two figures.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.ledger.service import RECOVERY_TYPES, LedgerService
from app.models.audit_log import AuditLog
from app.models.ledger_entry import LedgerEntry
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


class SettlementReconRow(BaseModel):
    """One row of Razorpay settlement recon, normalised to rupees."""

    entity_id: str = Field(..., description="Razorpay payment id (pay_xxx)")
    settlement_id: str = Field(..., description="Razorpay settlement id (setl_xxx)")
    amount: Optional[Decimal] = Field(None, description="Settled amount in rupees (None = use captured amount)")
    settled_at: Optional[datetime] = None
    settlement_utr: Optional[str] = None
    type: str = "payment"

    @classmethod
    def from_razorpay(cls, item: Dict[str, Any]) -> "SettlementReconRow":
        settled_ts = item.get("settled_at")
        settled_dt = datetime.fromtimestamp(int(settled_ts), tz=timezone.utc) if settled_ts else None
        amount_paise = item.get("amount")
        return cls(
            entity_id=str(item.get("entity_id")),
            settlement_id=str(item.get("settlement_id") or ""),
            amount=(Decimal(str(amount_paise)) / Decimal("100")).quantize(Decimal("0.01")) if amount_paise is not None else None,
            settled_at=settled_dt,
            settlement_utr=item.get("settlement_utr"),
            type=str(item.get("type") or "payment"),
        )


class SettlementReconciliationService:
    """Marks recovered rupees as settled once Razorpay confirms the payout."""

    @classmethod
    def reconcile_rows(cls, db: Session, rows: Iterable[SettlementReconRow]) -> Dict[str, Any]:
        matched: List[Dict[str, Any]] = []
        unmatched: List[str] = []
        settled_total = Decimal("0.00")

        for row in rows:
            if row.type != "payment" or not row.settlement_id:
                continue
            recovery_entry = db.scalar(
                select(LedgerEntry).where(
                    LedgerEntry.provider_reference == row.entity_id,
                    LedgerEntry.entry_type.in_(RECOVERY_TYPES),
                )
            )
            if not recovery_entry:
                unmatched.append(row.entity_id)
                continue

            case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_entry.recovery_case_id))
            amount = row.amount if row.amount is not None else recovery_entry.amount
            entry = LedgerService.post_settlement(
                db, case, amount, settlement_id=row.settlement_id, occurred_at=row.settled_at,
                metadata={"payment_id": row.entity_id, "utr": row.settlement_utr},
            )
            settled_total += Decimal(str(entry.amount))
            matched.append({"case_id": str(case.id), "payment_id": row.entity_id, "settlement_id": row.settlement_id, "amount": float(entry.amount)})
            db.add(
                AuditLog(
                    recovery_case_id=case.id,
                    actor_type="SYSTEM",
                    actor_id="settlement_reconciliation_v1",
                    action="SETTLEMENT_RECONCILED",
                    entity_type="LedgerEntry",
                    entity_id=str(entry.id),
                    audit_metadata={"payment_id": row.entity_id, "settlement_id": row.settlement_id, "amount": float(entry.amount), "utr": row.settlement_utr},
                )
            )

        db.flush()
        summary = {
            "matched_count": len(matched),
            "unmatched_count": len(unmatched),
            "settled_amount": float(settled_total),
            "matched": matched,
            "unmatched_payment_ids": unmatched[:100],
        }
        logger.info(f"[SETTLEMENT_RECON] matched={len(matched)} unmatched={len(unmatched)} settled={settled_total}")
        return summary

    @classmethod
    def fetch_razorpay_recon(cls, year: int, month: int, day: Optional[int] = None, timeout: float = 15.0) -> List[SettlementReconRow]:
        """Fetch combined settlement recon from Razorpay (test or live keys)."""
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            raise RuntimeError("Razorpay API credentials are not configured.")
        params: Dict[str, Any] = {"year": year, "month": month, "count": 1000}
        if day:
            params["day"] = day
        url = "https://api.razorpay.com/v1/settlements/recon/combined"
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params=params, auth=httpx.BasicAuth(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            resp.raise_for_status()
            data = resp.json()
        return [SettlementReconRow.from_razorpay(item) for item in data.get("items", [])]
