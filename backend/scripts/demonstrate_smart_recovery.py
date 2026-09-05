"""End-to-End Live Demonstration of RevenueShield Smart Payment Recovery Intervention.

Simulates and verifies:
1. Failed INR 5,000 Payment Event Ingested via Webhook
2. Case Detection & Customer Profile Creation
3. Root Cause Diagnosis Engine
4. Predictive AI Scoring & Policy Approval
5. Razorpay Payment Link Generation & Customer Outreach
6. Customer Payment & `payment.captured` Webhook Reconciliation
7. Stopping Rule Execution: Case marked RECOVERED, Link marked PAID, Outreach Stopped
8. Database Auditing & Verification Queries
9. CRITICAL STOPPING RULE TEST: Re-triggering intervention on recovered case is strictly BLOCKED
"""
import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from datetime import datetime, timezone
from decimal import Decimal
import json
import time
import uuid
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.intervention import Intervention
from app.models.outcome import RecoveryOutcome
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.event import Event
from app.services.event_processor import EventProcessor
from app.services.intervention_service import InterventionService
from app.schemas.event import NormalizedEvent


def run_demo():
    print("=" * 80)
    print("REVENUESHIELD SMART RECOVERY INTERVENTION: END-TO-END DEMONSTRATION")
    print("=" * 80)

    demo_payment_id = f"pay_demo_{uuid.uuid4().hex[:10]}"
    demo_order_id = f"order_demo_{uuid.uuid4().hex[:10]}"
    demo_event_fail_id = f"evt_fail_{uuid.uuid4().hex[:10]}"
    demo_event_cap_id = f"evt_cap_{uuid.uuid4().hex[:10]}"
    demo_cust_id = f"cust_demo_{uuid.uuid4().hex[:8]}"

    with Session(engine) as db:
        # =====================================================================
        # STAGE 1 & 2: INR 5,000 PAYMENT FAILS -> WEBHOOK INGESTION -> CASE OPENED
        # =====================================================================
        print("\n[STAGE 1 & 2] Ingesting Razorpay payment.failed webhook (Amount: INR 5,000.00)...")
        fail_payload = NormalizedEvent(
            event_id=demo_event_fail_id,
            event_type="payment.failed",
            source="RAZORPAY",
            occurred_at=datetime.now(timezone.utc),
            external_customer_id=demo_cust_id,
            customer_email="rahul.verma@example.com",
            customer_phone="+919876543210",
            customer_name="Rahul Verma",
            external_payment_id=demo_payment_id,
            external_order_id=demo_order_id,
            amount=Decimal("5000.00"),
            currency="INR",
            payment_method="UPI",
            failure_code="BAD_REQUEST_ERROR",
            failure_reason="Customer bank server timed out during UPI PIN verification.",
            failure_source="customer_bank",
            failure_step="payment_authentication",
            raw_payload={"payment_id": demo_payment_id, "amount": 500000, "status": "failed"},
        )

        proc_res = EventProcessor.process_normalized_event(db=db, event=fail_payload)
        db.commit()

        case_id = uuid.UUID(proc_res.recovery_case_id)
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))

        print(f"  [+] Webhook Processed: Status = {proc_res.status}")
        print(f"  [+] RecoveryCase Opened: ID = {case.id}")
        print(f"  [+] Amount At Risk: INR {case.amount_at_risk:,.2f} {case.currency}")
        print(f"  [+] Initial Case Status: {case.status}")

        # =====================================================================
        # STAGE 3 & 4: AI DIAGNOSIS + PREDICTION + POLICY PREVIEW
        # =====================================================================
        print("\n[STAGE 3 & 4] Generating AI Diagnosis, Expected Value Prediction & Policy Preview...")
        preview = InterventionService.preview_intervention(db=db, recovery_case_id=case.id)

        print(f"  [+] Recommended Action: {preview['recommended_action']}")
        print(f"  [+] Recovery Probability: {preview['probability']:.1%}")
        print(f"  [+] Expected Recovered Value: INR {preview['expected_recovered_value']:,.2f}")
        print(f"  [+] Policy Authorization Status: {preview['policy_status']}")
        print(f"  [+] Policy Reasons: {preview['policy_reasons']}")

        # =====================================================================
        # STAGE 5: EXECUTE INTERVENTION -> CREATE PAYMENT LINK + NOTIFICATION
        # =====================================================================
        print("\n[STAGE 5] Executing Smart Recovery Intervention (SEND_PAYMENT_LINK)...")
        interv_res = InterventionService.execute_intervention(
            db=db, recovery_case_id=case.id, dry_run=True
        )
        db.commit()

        print(f"  [+] Intervention ID: {interv_res.intervention_id}")
        print(f"  [+] Intervention Status: {interv_res.status}")
        print(f"  [+] Razorpay Payment Link ID: {interv_res.payment_link.razorpay_payment_link_id}")
        print(f"  [+] Customer Payment URL: {interv_res.payment_link.url}")
        print(f"  [+] Link Amount: INR {interv_res.payment_link.amount:,.2f} {interv_res.payment_link.currency}")

        db.refresh(case)
        print(f"  [+] Case Transitioned To: {case.status} (Retry count: {case.retry_count})")

        # =====================================================================
        # STAGE 6 & 7: CUSTOMER PAYS -> WEBHOOK CAPTURE -> STOPPING RULE TRIGGER
        # =====================================================================
        print("\n[STAGE 6 & 7] Customer completes payment -> Ingesting Razorpay payment.captured webhook...")
        capture_payload = NormalizedEvent(
            event_id=demo_event_cap_id,
            event_type="payment.captured",
            source="RAZORPAY",
            occurred_at=datetime.now(timezone.utc),
            external_customer_id=demo_cust_id,
            external_payment_id=demo_payment_id,
            external_order_id=demo_order_id,
            amount=Decimal("5000.00"),
            currency="INR",
            payment_method="UPI",
            raw_payload={"payment_id": demo_payment_id, "amount": 500000, "status": "captured"},
        )

        cap_proc_res = EventProcessor.process_normalized_event(db=db, event=capture_payload)
        db.commit()

        db.refresh(case)
        print(f"  [+] Capture Webhook Processed: Status = {cap_proc_res.status}")
        print(f"  [+] Case Status: {case.status}")
        print(f"  [+] Total Amount Recovered: INR {case.recovered_amount:,.2f}")

        # Verify active intervention marked SUCCEEDED
        interv_rec = db.scalar(select(Intervention).where(Intervention.id == uuid.UUID(interv_res.intervention_id)))
        print(f"  [+] Intervention State: {interv_rec.status} (Completed At: {interv_rec.completed_at})")

        # Verify payment link marked PAID
        plink_rec = db.scalar(select(RecoveryPaymentLink).where(RecoveryPaymentLink.id == uuid.UUID(interv_res.payment_link.id)))
        print(f"  [+] Recovery Payment Link State: {plink_rec.status} (Paid At: {plink_rec.paid_at})")

        # =====================================================================
        # STAGE 8: DATABASE AUDIT QUERIES
        # =====================================================================
        print("\n" + "=" * 80)
        print("DATABASE VERIFICATION: 1. SELECT * FROM recovery_cases (Latest 5)")
        print("=" * 80)
        case_rows = db.execute(
            text("""
                SELECT id, case_type, amount_at_risk, recovered_amount, currency, status, retry_count, created_at
                FROM recovery_cases
                ORDER BY created_at DESC
                LIMIT 5;
            """)
        ).mappings().all()

        for r in case_rows:
            print(f"  * ID: {r['id']} | Type: {r['case_type']} | Risk: INR {r['amount_at_risk']} | Recovered: INR {r['recovered_amount']} | Status: {r['status']} | Retries: {r['retry_count']}")

        print("\n" + "=" * 80)
        print("DATABASE VERIFICATION: 2. SELECT * FROM recovery_outcomes (Latest 5)")
        print("=" * 80)
        outcome_rows = db.execute(
            text("""
                SELECT id, recovery_case_id, outcome_type, attribution, amount_at_risk, amount_recovered, recovery_percentage, occurred_at
                FROM recovery_outcomes
                ORDER BY occurred_at DESC
                LIMIT 5;
            """)
        ).mappings().all()

        for r in outcome_rows:
            print(f"  * ID: {r['id']} | Case: {r['recovery_case_id']} | Outcome: {r['outcome_type']} | Attribution: {r['attribution']} | Recovered: INR {r['amount_recovered']} ({r['recovery_percentage']}%)")

        print("\n" + "=" * 80)
        print("DATABASE VERIFICATION: 3. SELECT action, actor_type, created_at FROM audit_logs (Audit Trail)")
        print("=" * 80)
        audit_rows = db.execute(
            text("""
                SELECT action, actor_type, actor_id, timestamp
                FROM audit_logs
                WHERE recovery_case_id = :cid
                ORDER BY timestamp ASC
                LIMIT 20;
            """),
            {"cid": case.id},
        ).mappings().all()

        for idx, r in enumerate(audit_rows, 1):
            print(f"  {idx:02d}. [{r['timestamp'].strftime('%H:%M:%S.%f')[:-3]}] {r['action']:<30} | Actor: {r['actor_type']} ({r['actor_id']})")

        # =====================================================================
        # STAGE 9: CRITICAL STOPPING RULE TEST
        # =====================================================================
        print("\n" + "=" * 80)
        print("CRITICAL STOPPING RULE TEST: TRIGGERING INTERVENTION AFTER PAYMENT SUCCEEDS")
        print("=" * 80)
        print(f"Triggering execute_intervention on already recovered case ({case.id})...")

        second_attempt = InterventionService.execute_intervention(
            db=db, recovery_case_id=case.id, dry_run=True
        )

        print(f"  [+] Result Status: {second_attempt.status}")
        print(f"  [+] Reason: {second_attempt.reason}")
        print(f"  [+] Payment Link Dispatched: {second_attempt.payment_link}")

        assert second_attempt.status in ["ALREADY_RECOVERED", "BLOCKED"], f"Expected ALREADY_RECOVERED or BLOCKED but got {second_attempt.status}"
        assert second_attempt.payment_link is None, "Payment link was incorrectly created for a recovered case!"
        assert "RECOVERED" in (second_attempt.reason or ""), f"Unexpected reason: {second_attempt.reason}"

        print("\n" + "=" * 80)
        print(">>> SUCCESS: STOPPING RULE ACTIVELY BLOCKS ALL FURTHER OUTREACH! <<<")
        print("=" * 80)


if __name__ == "__main__":
    run_demo()
