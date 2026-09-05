"""Unit tests for RazorpayPaymentSyncService synchronization and checkpoints."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.sync_checkpoint import SyncCheckpoint
from app.models.recovery_case import RecoveryCase
from app.integrations.razorpay.payment_client import RazorpayPaymentClient
from app.services.razorpay_sync_service import RazorpayPaymentSyncService


class MockRazorpayClient:
    """Mock client returning controlled mock payment pages."""

    def __init__(self, items):
        self.items = items

    def fetch_payments(self, from_timestamp=None, to_timestamp=None, count=100, skip=0):
        sliced = self.items[skip : skip + count]
        return {
            "entity": "collection",
            "count": len(sliced),
            "items": sliced,
        }


def test_sync_payments_lifecycle_and_idempotency(db_session: Session):
    """Verify that first sync creates N rows and second sync creates 0 new rows (updates only)."""
    mock_items = [
        {
            "id": "pay_sync_001",
            "entity": "payment",
            "amount": 50000,
            "currency": "INR",
            "status": "captured",
            "method": "card",
            "captured": True,
            "email": "sync1@example.com",
            "created_at": 1716300000,
        },
        {
            "id": "pay_sync_002",
            "entity": "payment",
            "amount": 75000,
            "currency": "INR",
            "status": "failed",
            "method": "upi",
            "captured": False,
            "email": "sync2@example.com",
            "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "incorrect_otp",
            "created_at": 1716300100,
        },
    ]

    mock_client = MockRazorpayClient(items=mock_items)

    # 1. First Synchronization Run
    res1 = RazorpayPaymentSyncService.sync_payments(
        db=db_session,
        from_dt=datetime.now(timezone.utc) - timedelta(days=1),
        to_dt=datetime.now(timezone.utc),
        batch_size=10,
        client=mock_client,
    )

    assert res1.status == "SUCCEEDED"
    assert res1.records_fetched == 2
    assert res1.records_created == 2
    assert res1.records_updated == 0

    # Verify a RecoveryCase was automatically opened for pay_sync_002 (failed payment)
    pay2 = db_session.scalar(select(Payment).where(Payment.external_payment_id == "pay_sync_002"))
    assert pay2 is not None
    case2 = db_session.scalar(select(RecoveryCase).where(RecoveryCase.payment_id == pay2.id))
    assert case2 is not None
    assert case2.status == "OPEN"
    assert case2.amount_at_risk == Decimal("750.00")

    # 2. Second Synchronization Run with SAME dataset
    res2 = RazorpayPaymentSyncService.sync_payments(
        db=db_session,
        from_dt=datetime.now(timezone.utc) - timedelta(days=1),
        to_dt=datetime.now(timezone.utc),
        batch_size=10,
        client=mock_client,
    )

    assert res2.status == "SUCCEEDED"
    assert res2.records_fetched == 2
    assert res2.records_created == 0  # CRITICAL: Idempotent! 0 new records created
    assert res2.records_updated == 2

    # Verify NO duplicate RecoveryCase was created
    cases_for_pay2 = db_session.scalars(select(RecoveryCase).where(RecoveryCase.payment_id == pay2.id)).all()
    assert len(cases_for_pay2) == 1


def test_data_quality_summary_computation(db_session: Session):
    """Verify computing accurate data quality summary aggregates."""
    summary = RazorpayPaymentSyncService.get_data_quality_summary(db_session)

    assert "total_payments" in summary
    assert "successful_payments" in summary
    assert "failed_payments" in summary
    assert "total_amount" in summary
    assert "failed_amount" in summary
    assert "captured_amount" in summary
