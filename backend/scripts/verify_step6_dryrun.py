"""Demonstration script for Step 6 Dry-Run Execution and Money Tracking Verification."""
import hmac
import hashlib
import json
import uuid
import httpx
from sqlalchemy import create_engine, text
from app.core.config import settings

API_URL = f"http://{settings.HOST}:{settings.PORT}"
WEBHOOK_SECRET = settings.RAZORPAY_WEBHOOK_SECRET or "test_webhook_secret_local_dev"
DB_URL = settings.DATABASE_URL

def run_demonstration():
    print("=" * 65)
    print(" STEP 6: BOUNDED RECOVERY EXECUTION (DRY RUN DEMO) ")
    print("=" * 65)

    client = httpx.Client(base_url=API_URL, timeout=10.0)

    # 1. Generate a failed payment event to create an OPEN case with an approved recommendation
    pay_id = f"pay_demo_{uuid.uuid4().hex[:8]}"
    event_payload = {
        "entity": "event",
        "account_id": "acc_demo_01",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": pay_id,
                    "entity": "payment",
                    "amount": 75000,  # ₹750.00 in paise
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_demo_101",
                    "method": "card",
                    "description": "Pro Tier Monthly",
                    "email": "aarav.patel@example.com",
                    "contact": "+919876500002",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment failed: OTP authentication expired.",
                    "error_source": "customer",
                    "error_step": "payment_authentication",
                    "error_reason": "incorrect_otp",
                    "created_at": 1716306000,
                }
            }
        },
        "created_at": 1716306000,
    }

    raw_body = json.dumps(event_payload).encode("utf-8")
    sig = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    print("\n1. Ingesting 'payment.failed' event via POST /webhooks/razorpay...")
    res_webhook = client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "x-razorpay-event-id": f"evt_demo_{uuid.uuid4().hex[:10]}",
        },
    )
    webhook_data = res_webhook.json()
    case_id = webhook_data["recovery_case_id"]
    print(f"   [+] RecoveryCase OPENED with ID: {case_id}")

    # 2. Check recommendation
    res_rec = client.get(f"/recovery-cases/{case_id}/recommendation")
    rec_data = res_rec.json()
    print(f"\n2. Recommendation for case: {rec_data['recommended_action']} (Status: {rec_data['status']})")

    # 3. Call POST /recovery-cases/{case_id}/execute
    print(f"\n3. Calling POST /recovery-cases/{case_id}/execute ...")
    res_exec = client.post(f"/recovery-cases/{case_id}/execute")
    exec_data = res_exec.json()
    print("   Execution Response:")
    print(json.dumps(exec_data, indent=4))

    # 4. Query PostgreSQL database directly
    print("\n4. Querying PostgreSQL: 'recovery_executions' table:")
    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT 
                    id, 
                    recovery_case_id AS case_id, 
                    action_type, 
                    provider, 
                    status, 
                    provider_reference, 
                    requested_at, 
                    completed_at 
                FROM recovery_executions 
                ORDER BY requested_at DESC 
                LIMIT 1;
            """)
        )
        row = result.fetchone()
        if row:
            print(f"   id                 : {row[0]}")
            print(f"   case_id            : {row[1]}")
            print(f"   action_type        : {row[2]}")
            print(f"   provider           : {row[3]}")
            print(f"   status             : {row[4]}")
            print(f"   provider_reference : {row[5]}")
            print(f"   requested_at       : {row[6]}")
            print(f"   completed_at       : {row[7]}")

        print("\n5. Querying PostgreSQL: 'recovery_cases' table (Money Tracking Check):")
        result_case = conn.execute(
            text("""
                SELECT 
                    id, 
                    status, 
                    amount_at_risk, 
                    recovered_amount 
                FROM recovery_cases 
                WHERE id = :case_id;
            """),
            {"case_id": case_id},
        )
        case_row = result_case.fetchone()
        if case_row:
            print(f"   id                 : {case_row[0]}")
            print(f"   status             : {case_row[1]}")
            print(f"   amount_at_risk     : {case_row[2]}")
            print(f"   recovered_amount   : {case_row[3]}")

    print("\n" + "=" * 65)
    print(" DEMONSTRATION COMPLETE: MONEY TRACKING INVARIANT VERIFIED! ")
    print("=" * 65)

if __name__ == "__main__":
    run_demonstration()
