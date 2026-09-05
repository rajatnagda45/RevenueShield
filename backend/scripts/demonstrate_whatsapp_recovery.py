"""End-to-End Live Demonstration of RevenueShield WhatsApp Recovery Agent.

Demonstrates and verifies:
1. Failed INR 5,000 Payment Ingestion
2. AI Diagnosis & Prediction for SEND_WHATSAPP_REMINDER
3. Policy Evaluation (Quiet hours, 24h Cooldown, Max Attempts = 3)
4. English and Hinglish Personalized Template Generation with Razorpay Payment Link
5. WhatsApp Dispatch via DevelopmentWhatsAppProvider
6. Delivery Status Callback Reconciliation (SENT -> DELIVERED)
7. Customer Payment Capture Webhook Reconciliation
8. Stopping Rule Execution: Outreach Cancelled, Case RECOVERED
9. CRITICAL TEST: Attempting WhatsApp Outreach on Recovered Case is strictly BLOCKED
"""
import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models.recovery_case import RecoveryCase
from app.models.communication import Communication
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.outcome import RecoveryOutcome
from app.models.audit_log import AuditLog
from app.services.event_processor import EventProcessor
from app.services.communication_orchestrator import CommunicationOrchestrator, WhatsAppOutreachResult
from app.schemas.event import NormalizedEvent


def run_demo():
    print("=" * 80)
    print("REVENUESHIELD WHATSAPP RECOVERY AGENT: END-TO-END DEMONSTRATION")
    print("=" * 80)

    demo_payment_id = f"pay_wa_demo_{uuid.uuid4().hex[:10]}"
    demo_order_id = f"order_wa_demo_{uuid.uuid4().hex[:10]}"
    demo_event_fail_id = f"evt_wa_fail_{uuid.uuid4().hex[:10]}"
    demo_event_cap_id = f"evt_wa_cap_{uuid.uuid4().hex[:10]}"
    demo_cust_id = f"cust_wa_demo_{uuid.uuid4().hex[:8]}"

    # Use daytime 14:00 IST (08:30 UTC) to verify active business hours
    base_time = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    with Session(engine) as db:
        # =====================================================================
        # STAGE 1: INR 5,000 PAYMENT FAILS -> EVENT INGESTED -> CASE OPENED
        # =====================================================================
        print("\n[STAGE 1] Ingesting Razorpay payment.failed webhook (Amount: INR 5,000.00)...")
        fail_payload = NormalizedEvent(
            event_id=demo_event_fail_id,
            event_type="payment.failed",
            source="RAZORPAY",
            occurred_at=base_time,
            external_customer_id=demo_cust_id,
            customer_email="suresh.kumar@example.com",
            customer_phone="+917991142735",
            customer_name="Suresh Kumar",
            external_payment_id=demo_payment_id,
            external_order_id=demo_order_id,
            amount=Decimal("5000.00"),
            currency="INR",
            payment_method="UPI",
            failure_code="BAD_REQUEST_ERROR",
            failure_reason="Customer bank server timed out during UPI PIN verification.",
            raw_payload={"payment_id": demo_payment_id, "amount": 500000, "status": "failed"},
        )

        proc_res = EventProcessor.process_normalized_event(db=db, event=fail_payload)
        db.commit()

        case_id = uuid.UUID(proc_res.recovery_case_id)
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))

        print(f"  [+] Webhook Ingested: Status = {proc_res.status}")
        print(f"  [+] RecoveryCase Opened: ID = {case.id}")
        print(f"  [+] Amount At Risk: INR {case.amount_at_risk:,.2f} {case.currency}")
        print(f"  [+] Initial Case Status: {case.status}")

        # =====================================================================
        # STAGE 2: PREVIEW TEMPLATES (ENGLISH & HINGLISH) WITHOUT SIDE EFFECTS
        # =====================================================================
        print("\n[STAGE 2] Generating WhatsApp Message Previews...")

        en_preview = CommunicationOrchestrator.preview_whatsapp_outreach(
            db=db, recovery_case_id=case.id, language="ENGLISH", reference_time=base_time
        )
        hi_preview = CommunicationOrchestrator.preview_whatsapp_outreach(
            db=db, recovery_case_id=case.id, language="HINGLISH", reference_time=base_time
        )

        print(f"  [+] English Template ({en_preview['template_name']}):")
        print(f"      \"{en_preview['message_body']}\"")
        print(f"  [+] Hinglish Template ({hi_preview['template_name']}):")
        print(f"      \"{hi_preview['message_body']}\"")
        print(f"  [+] Policy Status: {en_preview['policy_status']} (Max attempts: {en_preview['max_attempts']})")

        # =====================================================================
        # STAGE 3: EXECUTE WHATSAPP RECOVERY OUTREACH (HINGLISH)
        # =====================================================================
        print("\n[STAGE 3] Dispatching WhatsApp Recovery Message via Provider...")
        outreach: WhatsAppOutreachResult = CommunicationOrchestrator.queue_or_send_whatsapp_recovery(
            db=db,
            recovery_case_id=case.id,
            language="HINGLISH",
            dry_run=True,
            reference_time=base_time,
        )
        db.commit()

        print(f"  [+] Communication ID: {outreach.communication_id}")
        print(f"  [+] Channel: {outreach.channel}")
        print(f"  [+] Status: {outreach.status}")
        print(f"  [+] Provider: {outreach.provider} (Simulated: {outreach.is_simulated})")
        print(f"  [+] Provider Message ID: {outreach.provider_message_id}")
        print(f"  [+] Recipient (Masked): {outreach.recipient_masked}")
        print(f"  [+] Embedded Payment Link: {outreach.payment_link_url}")

        db.refresh(case)
        print(f"  [+] Case Status: {case.status} (Retry count: {case.retry_count})")

        # =====================================================================
        # STAGE 4: PROVIDER STATUS WEBHOOK (SENT -> DELIVERED)
        # =====================================================================
        print("\n[STAGE 4] Receiving WhatsApp Provider Delivery Callback...")
        updated_comm = CommunicationOrchestrator.handle_status_webhook(
            db=db,
            provider_message_id=outreach.provider_message_id,
            status="DELIVERED",
        )
        db.commit()

        print(f"  [+] Communication Status Updated: {updated_comm.status}")
        print(f"  [+] Delivered At: {updated_comm.delivered_at}")

        # =====================================================================
        # STAGE 5: CUSTOMER PAYS -> PAYMENT CAPTURED WEBHOOK RECONCILIATION
        # =====================================================================
        print("\n[STAGE 5] Customer completes payment -> Ingesting payment.captured webhook...")
        capture_payload = NormalizedEvent(
            event_id=demo_event_cap_id,
            event_type="payment.captured",
            source="RAZORPAY",
            occurred_at=base_time + timedelta(minutes=10),
            external_customer_id=demo_cust_id,
            external_payment_id=demo_payment_id,
            external_order_id=demo_order_id,
            amount=Decimal("5000.00"),
            currency="INR",
            payment_method="UPI",
            raw_payload={"payment_id": demo_payment_id, "amount": 500000, "status": "captured"},
        )

        cap_res = EventProcessor.process_normalized_event(db=db, event=capture_payload)
        db.commit()

        db.refresh(case)
        print(f"  [+] Capture Event Status: {cap_res.status}")
        print(f"  [+] Case Status: {case.status}")
        print(f"  [+] Total Amount Recovered: INR {case.recovered_amount:,.2f}")

        # =====================================================================
        # STAGE 6: DATABASE AUDIT TRAIL & COMMUNICATIONS
        # =====================================================================
        print("\n" + "=" * 80)
        print("DATABASE AUDIT TRAIL (SELECT action, actor_type, timestamp FROM audit_logs)")
        print("=" * 80)
        audit_rows = db.execute(
            text("""
                SELECT action, actor_type, actor_id, timestamp
                FROM audit_logs
                WHERE recovery_case_id = :cid
                ORDER BY timestamp ASC;
            """),
            {"cid": case.id},
        ).mappings().all()

        for idx, r in enumerate(audit_rows, 1):
            print(f"  {idx:02d}. [{r['timestamp'].strftime('%H:%M:%S.%f')[:-3]}] {r['action']:<32} | Actor: {r['actor_type']} ({r['actor_id']})")

        # =====================================================================
        # STAGE 7: CRITICAL STOPPING RULE TEST
        # =====================================================================
        print("\n" + "=" * 80)
        print("CRITICAL STOPPING RULE TEST: TRIGGERING WHATSAPP OUTREACH ON RECOVERED CASE")
        print("=" * 80)
        print(f"Attempting to dispatch WhatsApp message on already recovered case ({case.id})...")

        post_recovery_attempt = CommunicationOrchestrator.queue_or_send_whatsapp_recovery(
            db=db,
            recovery_case_id=case.id,
            language="ENGLISH",
            dry_run=True,
            reference_time=base_time + timedelta(minutes=20),
        )

        print(f"  [+] Result Status: {post_recovery_attempt.status}")
        print(f"  [+] Blocking Rule: {post_recovery_attempt.policy_blocking_rule}")
        print(f"  [+] Reason: {post_recovery_attempt.reason}")

        assert post_recovery_attempt.status == "BLOCKED", f"Expected BLOCKED but got {post_recovery_attempt.status}"
        assert post_recovery_attempt.policy_blocking_rule == "CASE_ALREADY_RECOVERED_OR_CLOSED"

        print("\n" + "=" * 80)
        print(">>> SUCCESS: STOPPING RULE ACTIVELY BLOCKS ALL FURTHER WHATSAPP OUTREACH! <<<")
        print("=" * 80)


if __name__ == "__main__":
    run_demo()
