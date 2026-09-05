"""Test script to dispatch a live payment recovery email via Gmail SMTP."""
import sys
from pathlib import Path
import uuid
from decimal import Decimal

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy.orm import Session
from app.db.session import engine
from app.services.event_processor import EventProcessor
from app.schemas.event import NormalizedEvent
from app.services.email_recovery_service import EmailRecoveryService
from app.core.config import settings


def test_live_email_recovery(target_email: str = "kdmspokharahan@gmail.com"):
    print("=" * 80)
    print("TESTING LIVE EMAIL PAYMENT RECOVERY DISPATCH VIA GMAIL SMTP")
    print("=" * 80)
    print(f"SMTP Host:      {settings.SMTP_HOST}:{settings.SMTP_PORT}")
    print(f"Sender (From):  {settings.SMTP_FROM_EMAIL}")
    print(f"Recipient (To): {target_email}")

    with Session(engine) as db:
        # 1. Create failed payment event and open recovery case
        print("\n[Step 1] Ingesting failed payment of INR 5,000.00...")
        demo_id = uuid.uuid4().hex[:8]
        event = NormalizedEvent(
            event_id=f"evt_email_demo_{demo_id}",
            event_type="payment.failed",
            source="RAZORPAY",
            external_payment_id=f"pay_email_{demo_id}",
            external_order_id=f"order_email_{demo_id}",
            customer_email=target_email,
            customer_name="Ankit Kumar",
            customer_phone="+917991142735",
            amount=Decimal("5000.00"),
            currency="INR",
            payment_method="UPI",
            failure_code="BAD_REQUEST_ERROR",
            failure_reason="Customer bank server timed out.",
        )
        proc_res = EventProcessor.process_normalized_event(db, event)
        case_id = proc_res.recovery_case_id
        print(f"  [+] Case Opened: ID = {case_id} (Status = OPEN)")

        # 2. Execute Email Recovery
        print("\n[Step 2] Executing Email Recovery Service...")
        res = EmailRecoveryService.execute_recovery(
            db=db,
            case_id=case_id,
            recipient_email=target_email,
        )

        print("\n[Step 3] Dispatch Result:")
        print(f"  Success:          {res['success']}")
        print(f"  Status:           {res['status']}")
        print(f"  Recipient:        {res.get('recipient')}")
        print(f"  Razorpay Link:    {res.get('payment_link_url')}")
        print(f"  Communication ID: {res.get('communication_id')}")
        if res.get("error"):
            print(f"  Error:            {res['error']}")

        print("=" * 80)
        if res["success"]:
            print(">>> LIVE PAYMENT RECOVERY EMAIL DELIVERED TO INBOX! <<<")
            print(f"Check the inbox for: {target_email}")
        else:
            print(">>> EMAIL DISPATCH FAILED. CHECK DETAILS ABOVE. <<<")
        print("=" * 80)


if __name__ == "__main__":
    email_to = sys.argv[1] if len(sys.argv) > 1 else "kdmspokharahan@gmail.com"
    test_live_email_recovery(target_email=email_to)
