# Real-Time Recovery Demonstration Guide

This walkthrough explains how to demonstrate the live, end-to-end payment recovery workflow:

$$\text{FAILED PAYMENT} \longrightarrow \text{AI DIAGNOSIS} \longrightarrow \text{PAYMENT LINK} \longrightarrow \text{REAL WHATSAPP} \longrightarrow \text{PAYMENT} \longrightarrow \text{WEBHOOK} \longrightarrow \text{RECOVERED}$$

---

## 1. Prerequisites

1. **PostgreSQL Database Running**: Port 5432 / 5435.
2. **Razorpay Test Mode Credentials**: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`.
3. **Twilio WhatsApp Sandbox Credentials**:
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
   - `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`
   - `TWILIO_WHATSAPP_TO=whatsapp:+91<your-mobile-number>` (Joined to sandbox)
4. **Backend Server Running**:
   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

---

## 2. Step-by-Step Live Demonstration

### Step 1: Simulate or Trigger a Razorpay Test Mode Payment Failure
Submit a `payment.failed` webhook to the backend:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/razorpay \
  -H "Content-Type: application/json" \
  -H "X-Razorpay-Signature: <computed_signature>" \
  -d '{
    "event": "payment.failed",
    "payload": {
      "payment": {
        "entity": {
          "id": "pay_test_demo_001",
          "amount": 500000,
          "currency": "INR",
          "status": "failed",
          "method": "upi",
          "email": "demo.user@example.com",
          "contact": "+919876543210",
          "error_code": "BAD_REQUEST_ERROR",
          "error_description": "Payment authorization timed out."
        }
      }
    }
  }'
```

**What Happens Behind the Scenes:**
1. Event is normalized and ingested idempotently.
2. `DiagnosisEngine` determines category `AUTHENTICATION_FAILED` (Score: 35.0).
3. `RecoveryCase` is opened with status `OPEN`, Amount at Risk: `₹5,000.00`.
4. `DecisionEngine` recommends `SEND_WHATSAPP_REMINDER`.

---

### Step 2: Trigger Real WhatsApp Recovery Outreach
Call the recovery endpoint:

```bash
curl -X POST http://127.0.0.1:8000/recovery-cases/<case_id>/whatsapp-recovery \
  -H "Content-Type: application/json" \
  -d '{"language": "HINGLISH"}'
```

**What Happens Behind the Scenes:**
1. Policy engine checks DND quiet hours, cooldown, and attempt limits.
2. `RazorpayPaymentLinkClient` creates a real Razorpay Test Mode payment link (`https://rzp.io/i/...`).
3. `TwilioWhatsAppClient` sends a real WhatsApp message to your test phone number.
4. Message arrives on your phone:
   > *"Hi Rahul, aapka ₹5,000.00 ka payment complete nahi ho paya. Aap yahan se securely payment complete kar sakte hain: https://rzp.io/i/plink_xxxxx"*

---

### Step 3: Complete Payment via Payment Link
1. Open the received payment link on your phone or browser.
2. Select **UPI / Card / Netbanking (Razorpay Test Mode)**.
3. Click **Success** to simulate successful payment capture.

---

### Step 4: Ingest `payment.captured` Webhook
Razorpay dispatches the `payment.captured` webhook to your server:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/razorpay \
  -H "Content-Type: application/json" \
  -H "X-Razorpay-Signature: <computed_signature>" \
  -d '{
    "event": "payment.captured",
    "payload": {
      "payment": {
        "entity": {
          "id": "pay_test_demo_001",
          "amount": 500000,
          "currency": "INR",
          "status": "captured"
        }
      }
    }
  }'
```

**What Happens Behind the Scenes:**
1. `OutcomeEngine` reconciles payment capture with `RecoveryCase`.
2. Case status transitions to `RECOVERED` (Recovered Amount: `₹5,000.00`).
3. **Stopping Rule Triggers**: Cancels any future pending WhatsApp jobs.
4. `RecoveryOutcome` and `LearningExample` records are finalized.

---

### Step 5: Verify Critical Stopping Rule
Try triggering the WhatsApp recovery endpoint a second time on the same case:

```bash
curl -X POST http://127.0.0.1:8000/recovery-cases/<case_id>/whatsapp-recovery \
  -H "Content-Type: application/json" \
  -d '{"language": "ENGLISH"}'
```

**Response:**
```json
{
  "case_id": "<case_id>",
  "action": "WHATSAPP_PAYMENT_RECOVERY",
  "status": "BLOCKED",
  "reason": "Recovery case is already RECOVERED. Outreach is strictly prohibited.",
  "policy_blocking_rule": "CASE_ALREADY_RECOVERED_OR_CLOSED"
}
```

No second message is dispatched. Stopping rule is proven active!
