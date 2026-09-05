"""Live end-to-end verification script for Razorpay webhook ingestion."""
import sys
import os
import json
import httpx

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings
from app.integrations.razorpay.security import compute_razorpay_signature


def run_live_test():
    base_url = "http://127.0.0.1:8000"
    secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_webhook_secret_local_dev"

    print("==========================================================")
    print("      REVENUE RECOVERY AI - LIVE WEBHOOK TEST             ")
    print("==========================================================")
    print(f"Target Server : {base_url}")
    print(f"Webhook Secret: {secret[:4]}***{secret[-3:] if len(secret) > 7 else '***'}\n")

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        # 1. Test Health Endpoints
        print("[1/5] Testing Health Endpoints...")
        res = client.get("/health")
        print(f"  GET /health -> HTTP {res.status_code}: {res.json()}")
        assert res.status_code == 200, "Service health check failed"

        res_db = client.get("/health/db")
        print(f"  GET /health/db -> HTTP {res_db.status_code}: {res_db.json()}")
        assert res_db.status_code == 200, "Database connection check failed"
        print("  [+] Health endpoints verified successfully!\n")

        # 2. Simulate Razorpay payment.failed Webhook
        print("[2/5] Simulating Razorpay 'payment.failed' Webhook...")
        import time
        run_ts = int(time.time())
        pay_id = f"pay_live_test_{run_ts}"
        event_id = f"evt_live_failed_{run_ts}"
        fail_payload = {
            "entity": "event",
            "account_id": "acc_live_test_01",
            "event": "payment.failed",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": pay_id,
                        "entity": "payment",
                        "amount": 65000,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": "order_live_991",
                        "method": "card",
                        "email": "priya.sharma@example.com",
                        "contact": "+919876500001",
                        "notes": {
                            "customer_name": "Priya Sharma",
                            "subscription_plan": "Growth Tier",
                        },
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Payment failed: OTP authentication expired.",
                        "error_source": "customer",
                        "error_step": "payment_authentication",
                        "error_reason": "incorrect_otp",
                        "created_at": 1716305000,
                    }
                }
            },
            "created_at": 1716305000,
        }

        raw_fail_body = json.dumps(fail_payload).encode("utf-8")
        signature_fail = compute_razorpay_signature(raw_fail_body, secret)

        headers_fail = {
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature_fail,
            "x-razorpay-event-id": event_id,
        }

        res_fail = client.post("/webhooks/razorpay", content=raw_fail_body, headers=headers_fail)
        print(f"  POST /webhooks/razorpay -> HTTP {res_fail.status_code}")
        fail_data = res_fail.json()
        print(f"  Response: {json.dumps(fail_data, indent=4)}")
        assert res_fail.status_code == 200
        assert fail_data["status"] == "processed"
        case_id = fail_data["recovery_case_id"]
        print(f"  [+] RecoveryCase OPENED with ID: {case_id}\n")

        # 2b. Fetch and Verify Diagnosis for Case
        print("[2b/5] Querying GET /recovery-cases/{case_id}/diagnosis...")
        res_diag = client.get(f"/recovery-cases/{case_id}/diagnosis")
        print(f"  GET /recovery-cases/{case_id}/diagnosis -> HTTP {res_diag.status_code}")
        diag_data = res_diag.json()
        print(f"  Diagnosis Response: {json.dumps(diag_data, indent=4)}")
        assert res_diag.status_code == 200
        assert diag_data["category"] == "AUTHENTICATION_FAILURE"
        assert diag_data["engine_version"] == "diagnosis_engine_v1"
        assert diag_data["confidence"] >= 0.85
        assert diag_data["risk_score"] is not None
        assert diag_data["recovery_probability"] is not None
        print(f"  [+] Case diagnosed as: {diag_data['category']} (Risk: {diag_data['risk_score']}, Recovery Prob: {diag_data['recovery_probability']})\n")

        # 2c. Fetch and Verify Next Best Action Recommendation for Case
        print("[2c/5] Querying GET /recovery-cases/{case_id}/recommendation...")
        res_rec = client.get(f"/recovery-cases/{case_id}/recommendation")
        print(f"  GET /recovery-cases/{case_id}/recommendation -> HTTP {res_rec.status_code}")
        rec_data = res_rec.json()
        print(f"  Recommendation Response: {json.dumps(rec_data, indent=4)}")
        assert res_rec.status_code == 200
        assert rec_data["recommended_action"] in ["RETRY_PAYMENT", "SEND_PAYMENT_LINK", "SEND_WHATSAPP_REMINDER"]
        assert rec_data["status"] == "APPROVED"
        assert rec_data["decision_engine_version"] == "decision_engine_v1"
        assert rec_data["policy_engine_version"] == "policy_engine_v1"
        assert rec_data["policy"]["allowed"] is True
        print(f"  [+] Recommended Action: {rec_data['recommended_action']} via {rec_data['channel']} (Status: {rec_data['status']})\n")

        # 2d. Execute the Recommended Action (Bounded Execution)
        print("[2d/5] Executing Action via POST /recovery-cases/{case_id}/execute...")
        res_exec = client.post(f"/recovery-cases/{case_id}/execute")
        print(f"  POST /recovery-cases/{case_id}/execute -> HTTP {res_exec.status_code}")
        exec_data = res_exec.json()
        print(f"  Execution Response: {json.dumps(exec_data, indent=4)}")
        assert res_exec.status_code == 200
        assert exec_data["status"] in ["SUCCEEDED", "BLOCKED"]
        print(f"  [+] Execution status: {exec_data['status']} (Provider: {exec_data['provider']}, Ref: {exec_data['provider_reference']})\n")

        # 3. Test Idempotency (Re-send the same payment.failed webhook)
        print("[3/5] Testing Idempotency (duplicate delivery of same event_id)...")
        res_dup = client.post("/webhooks/razorpay", content=raw_fail_body, headers=headers_fail)
        print(f"  POST /webhooks/razorpay (duplicate) -> HTTP {res_dup.status_code}")
        dup_data = res_dup.json()
        print(f"  Response: {json.dumps(dup_data, indent=4)}")
        assert res_dup.status_code == 200
        assert dup_data["status"] == "duplicate"
        print("  [+] Duplicate webhook safely deduplicated!\n")

        # 4. Test Signature Tampering Rejection
        print("[4/5] Testing Security (Tampered payload rejection)...")
        tampered_body = b'{"tampered": "body"}'
        res_tampered = client.post(
            "/webhooks/razorpay",
            content=tampered_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature_fail},
        )
        print(f"  POST /webhooks/razorpay (tampered) -> HTTP {res_tampered.status_code}: {res_tampered.json()}")
        assert res_tampered.status_code == 401
        print("  [+] Tampered request correctly rejected with 401 Unauthorized!\n")

        # 5. Simulate Razorpay payment.captured Webhook (Recovery)
        print("[5/5] Simulating Razorpay 'payment.captured' Webhook (Recovery)...")
        cap_event_id = f"evt_live_captured_{run_ts}"
        now_epoch = int(time.time())
        cap_payload = {
            "entity": "event",
            "account_id": "acc_live_test_01",
            "event": "payment.captured",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": pay_id,
                        "entity": "payment",
                        "amount": 65000,
                        "currency": "INR",
                        "status": "captured",
                        "order_id": "order_live_991",
                        "method": "card",
                        "email": "priya.sharma@example.com",
                        "contact": "+919876500001",
                        "created_at": now_epoch,
                    }
                }
            },
            "created_at": now_epoch,
        }

        raw_cap_body = json.dumps(cap_payload).encode("utf-8")
        signature_cap = compute_razorpay_signature(raw_cap_body, secret)

        headers_cap = {
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature_cap,
            "x-razorpay-event-id": cap_event_id,
        }

        res_cap = client.post("/webhooks/razorpay", content=raw_cap_body, headers=headers_cap)
        print(f"  POST /webhooks/razorpay -> HTTP {res_cap.status_code}")
        cap_data = res_cap.json()
        print(f"  Response: {json.dumps(cap_data, indent=4)}")
        assert res_cap.status_code == 200
        assert cap_data["status"] == "processed"
        assert cap_data["recovery_case_id"] == case_id
        print(f"  [+] RecoveryCase transitioned to RECOVERED!\n")

        # 5b. Query Outcome API
        print("[5b/5] Querying GET /recovery-cases/{case_id}/outcome...")
        res_out = client.get(f"/recovery-cases/{case_id}/outcome")
        print(f"  GET /recovery-cases/{case_id}/outcome -> HTTP {res_out.status_code}")
        out_data = res_out.json()
        print(f"  Outcome Response: {json.dumps(out_data, indent=4)}")
        assert res_out.status_code == 200
        assert out_data["outcome_type"] == "RECOVERED"
        assert out_data["recovery_percentage"] == 100.0
        assert out_data["attribution"] == "DIRECT"
        print(f"  [+] Outcome verified: {out_data['outcome_type']} (Attribution: {out_data['attribution']}, Time: {out_data['time_to_recovery_seconds']}s)\n")

        # 5c. Query Learning Dataset Example
        print("[5c/5] Querying GET /learning/examples/{case_id}...")
        res_lrn = client.get(f"/learning/examples/{case_id}")
        print(f"  GET /learning/examples/{case_id} -> HTTP {res_lrn.status_code}")
        lrn_data = res_lrn.json()
        print(f"  Learning Example: {json.dumps(lrn_data, indent=4)}")
        assert res_lrn.status_code == 200
        assert lrn_data["is_finalized"] is True
        assert lrn_data["label"] == 1
        assert lrn_data["feature_snapshot"]["diagnosis_category"] == "AUTHENTICATION_FAILURE"
        print(f"  [+] Learning Example verified: label={lrn_data['label']} (Point-in-time features valid)\n")

    print("==========================================================")
    print("     ALL LIVE END-TO-END TESTS PASSED SUCCESSFULLY!       ")
    print("==========================================================")


if __name__ == "__main__":
    run_live_test()
