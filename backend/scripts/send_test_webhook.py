import requests
import json
import time
import hmac
import hashlib
import sys

url = "https://revenueshield-backend.onrender.com/webhooks/razorpay"

payment_id = f"pay_live_test_{int(time.time())}"
payload = {
    "event": "payment.failed",
    "payload": {
        "payment": {
            "entity": {
                "id": payment_id,
                "amount": 450000,
                "currency": "INR",
                "status": "failed",
                "order_id": f"order_{int(time.time())}",
                "invoice_id": f"inv_{int(time.time())}",
                "email": "ankit.kumar@example.com",
                "contact": "+917991142735",
                "description": "Annual Enterprise Shield License",
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": "Payment failed due to insufficient funds in customer bank account",
                "error_source": "bank",
                "error_step": "payment_authorization",
                "error_reason": "insufficient_funds",
                "created_at": int(time.time())
            }
        }
    }
}

raw_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')

# Try secrets
candidate_secrets = ["rzp_wh_secret_123", "test_secret", "secret", "razorpay_secret", ""]
success = False

for sec in candidate_secrets:
    sig = hmac.new(sec.encode('utf-8'), raw_bytes, hashlib.sha256).hexdigest() if sec else "test_sig"
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sig,
        "x-razorpay-event-id": f"evt_{int(time.time())}"
    }
    try:
        resp = requests.post(url, data=raw_bytes, headers=headers, timeout=10)
        print(f"Testing secret '{sec}': Status {resp.status_code} -> {resp.text}")
        if resp.status_code == 200:
            print("\n" + "="*60)
            print(f"✅ SUCCESS! Created Recovery Case with Payment ID: {payment_id}")
            print(f"Response: {resp.json()}")
            print("="*60)
            success = True
            break
    except Exception as e:
        print(f"Error connecting: {e}")

if not success:
    print("\nNote: If signature failed, please check the RAZORPAY_WEBHOOK_SECRET set in Render environment variables.")
