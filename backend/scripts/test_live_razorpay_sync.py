"""Script to test Live Razorpay Sync, Idempotency (twice-run verification), and PostgreSQL inspection."""
import json
from pathlib import Path
import sys
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from app.db.session import SessionLocal

BASE_URL = "http://127.0.0.1:8000"


def run_live_sync_verification():
    print("==========================================================")
    print("      REVENUE RECOVERY AI - RAZORPAY SYNC TEST            ")
    print("==========================================================")

    client = httpx.Client(base_url=BASE_URL, timeout=30.0)

    # 1. Health check
    res_health = client.get("/health")
    assert res_health.status_code == 200, f"Health check failed: {res_health.text}"
    print("[1/4] Server is online and database is connected.")

    # 2. First Synchronization Run
    print("\n[2/4] Triggering First Razorpay Historical Sync (POST /admin/razorpay/sync/payments)...")
    res_sync1 = client.post("/admin/razorpay/sync/payments", json={})
    print(f"  HTTP {res_sync1.status_code}")
    data_sync1 = res_sync1.json()
    print("  Sync #1 Response:", json.dumps(data_sync1, indent=4))
    assert res_sync1.status_code == 200
    assert data_sync1["status"] == "SUCCEEDED"

    fetched1 = data_sync1["records_fetched"]
    created1 = data_sync1["records_created"]
    updated1 = data_sync1["records_updated"]
    print(f"  [+] First Sync Results: Fetched={fetched1}, Created={created1}, Updated={updated1}")

    # 3. Second Synchronization Run (Idempotency Verification)
    print("\n[3/4] Triggering Second Razorpay Historical Sync (Idempotency Test)...")
    res_sync2 = client.post("/admin/razorpay/sync/payments", json={})
    print(f"  HTTP {res_sync2.status_code}")
    data_sync2 = res_sync2.json()
    print("  Sync #2 Response:", json.dumps(data_sync2, indent=4))
    assert res_sync2.status_code == 200
    assert data_sync2["status"] == "SUCCEEDED"

    fetched2 = data_sync2["records_fetched"]
    created2 = data_sync2["records_created"]
    updated2 = data_sync2["records_updated"]
    print(f"  [+] Second Sync Results: Fetched={fetched2}, Created={created2}, Updated={updated2}")

    # Verify IDEMPOTENCY INVARIANT
    assert created2 == 0, f"Idempotency failed! Created {created2} new records on 2nd sync run instead of 0."
    print("  [+] IDEMPOTENCY CONFIRMED: 0 new records created on second sync!\n")

    # 4. PostgreSQL Query Inspection
    print("[4/4] Inspecting Stored Payments in PostgreSQL...")
    db = SessionLocal()
    try:
        print("\n--- Query 1: Payments with Diagnostic & Gateway Fields ---")
        q1 = text("""
            SELECT
                COALESCE(razorpay_payment_id, external_payment_id) AS razorpay_payment_id,
                amount,
                currency,
                status,
                payment_method,
                bank,
                failure_code AS error_code,
                error_source,
                error_step,
                error_reason,
                razorpay_created_at
            FROM payments
            ORDER BY razorpay_created_at DESC NULLS LAST, created_at DESC
            LIMIT 10;
        """)
        rows1 = db.execute(q1).mappings().all()
        for r in rows1:
            print(f"  - ID: {r['razorpay_payment_id']} | {r['amount']} {r['currency']} | Status: {r['status']} | Method: {r['payment_method']} | Bank: {r['bank']} | Code: {r['error_code']} | Reason: {r['error_reason']}")

        print("\n--- Query 2: Aggregates by Status ---")
        q2 = text("""
            SELECT
                status,
                COUNT(*) AS count,
                SUM(amount) AS total_amount
            FROM payments
            GROUP BY status;
        """)
        rows2 = db.execute(q2).mappings().all()
        for r in rows2:
            print(f"  - Status: {r['status']:<10} | Count: {r['count']:<4} | Total Amount: {r['total_amount']}")

        print("\n--- Query 3: Data Quality Summary API ---")
        res_dq = client.get("/admin/razorpay/sync/data-quality")
        print("  Data Quality Response:", json.dumps(res_dq.json(), indent=4))

    finally:
        db.close()

    print("\n==========================================================")
    print("     ALL SYNC & IDEMPOTENCY VERIFICATIONS PASSED!         ")
    print("==========================================================")


if __name__ == "__main__":
    run_live_sync_verification()
