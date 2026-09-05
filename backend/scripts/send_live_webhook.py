import requests
import json
import time
import hmac
import hashlib

url = "https://revenueshield-backend.onrender.com/webhooks/razorpay"
secret = "U9xRcFHXVx_viR6"

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
                "order_id": f"order_test_{int(time.time())}",
                "invoice_id": f"inv_test_{int(time.time())}",
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
sig = hmac.new(secret.encode('utf-8'), raw_bytes, hashlib.sha256).hexdigest()

headers = {
    "Content-Type": "application/json",
    "X-Razorpay-Signature": sig,
    "x-razorpay-event-id": f"evt_{int(time.time())}"
}

print(f"Sending HMAC-SHA256 signed payment.failed event to {url}...")
resp = requests.post(url, data=raw_bytes, headers=headers, timeout=15)
print(f"Response Status: {resp.status_code}")
print(f"Response Body: {resp.text}")

if resp.status_code == 200:
    print("\n" + "="*70)
    print("🎉 SUCCESS! Real Recovery Case Created on Live Render Server!")
    print(f"Payment ID: {payment_id}")
    print(f"Amount at Risk: ₹4,500.00")
    print(f"Customer: ankit.kumar@example.com (+917991142735)")
    print("="*70)
