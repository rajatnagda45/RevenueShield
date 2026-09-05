"""Customer Intelligence & Historical Feature Extraction Service.

Computes historical payment, churn, and recovery metrics for a customer.
Serves as an explainable feature store layer for rule-based heuristics and future ML models.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase


def _to_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is offset-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class CustomerHistoricalFeatures:
    """Historical transaction and recovery profile metrics for a customer."""

    customer_id: str
    total_attempts: int = 0
    successful_count: int = 0
    failed_count: int = 0
    success_rate: float = 0.0
    recent_success_count: int = 0  # within last 30 days
    recent_failure_count: int = 0  # within last 30 days
    avg_transaction_amount: float = 0.0
    last_successful_payment_at: Optional[str] = None
    last_failed_payment_at: Optional[str] = None
    previous_recovery_cases_count: int = 0
    previous_recovered_amount: float = 0.0
    consecutive_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CustomerIntelligenceService:
    """Calculates historical metrics from customer payments and recovery cases."""

    @classmethod
    def get_customer_features(
        cls, db: Session, customer_id: uuid.UUID, reference_time: Optional[datetime] = None
    ) -> CustomerHistoricalFeatures:
        """Extract historical features for a customer up to the given reference time.

        Args:
            db: Database session.
            customer_id: Customer UUID.
            reference_time: Evaluation timestamp (defaults to current UTC time).

        Returns:
            CustomerHistoricalFeatures populated instance.
        """
        ref_time = _to_utc_aware(reference_time) or datetime.now(timezone.utc)
        thirty_days_ago = ref_time - timedelta(days=30)

        # 1. Fetch payments for customer
        payments: List[Payment] = db.scalars(
            select(Payment)
            .where(Payment.customer_id == customer_id)
            .order_by(Payment.created_at.desc())
        ).all()

        total_attempts = 0
        successful_count = 0
        failed_count = 0
        recent_success_count = 0
        recent_failure_count = 0
        total_amount = Decimal("0.00")
        last_success_dt: Optional[datetime] = None
        last_failed_dt: Optional[datetime] = None

        consecutive_failures = 0
        counting_consecutive = True

        for p in payments:
            p_time = _to_utc_aware(p.created_at)
            if p_time and p_time > ref_time:
                continue

            total_attempts += 1
            amt = p.amount or Decimal("0.00")
            total_amount += amt

            is_recent = p_time >= thirty_days_ago if p_time else False

            if p.status == "SUCCESS":
                successful_count += 1
                if is_recent:
                    recent_success_count += 1
                if not last_success_dt and p_time:
                    last_success_dt = p_time
                counting_consecutive = False
            elif p.status == "FAILED":
                failed_count += 1
                if is_recent:
                    recent_failure_count += 1
                if not last_failed_dt and p_time:
                    last_failed_dt = p_time
                if counting_consecutive:
                    consecutive_failures += 1

        success_rate = (
            float(successful_count) / float(total_attempts)
            if total_attempts > 0
            else 0.0
        )
        avg_amount = (
            float(total_amount / Decimal(total_attempts))
            if total_attempts > 0
            else 0.0
        )

        # 2. Fetch past recovery cases
        cases: List[RecoveryCase] = db.scalars(
            select(RecoveryCase)
            .where(RecoveryCase.customer_id == customer_id)
        ).all()

        valid_cases_count = 0
        prev_recovered_sum = Decimal("0.00")
        for c in cases:
            c_time = _to_utc_aware(c.created_at)
            if c_time and c_time > ref_time:
                continue
            valid_cases_count += 1
            if c.status == "RECOVERED" and c.recovered_amount:
                prev_recovered_sum += c.recovered_amount

        return CustomerHistoricalFeatures(
            customer_id=str(customer_id),
            total_attempts=total_attempts,
            successful_count=successful_count,
            failed_count=failed_count,
            success_rate=round(success_rate, 4),
            recent_success_count=recent_success_count,
            recent_failure_count=recent_failure_count,
            avg_transaction_amount=round(avg_amount, 2),
            last_successful_payment_at=last_success_dt.isoformat() if last_success_dt else None,
            last_failed_payment_at=last_failed_dt.isoformat() if last_failed_dt else None,
            previous_recovery_cases_count=valid_cases_count,
            previous_recovered_amount=float(prev_recovered_sum),
            consecutive_failures=consecutive_failures,
        )

    build_customer_profile = get_customer_features
    get_customer_features_for_case = get_customer_features
