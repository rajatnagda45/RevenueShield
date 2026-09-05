# Real Twilio WhatsApp Sandbox Integration Guide

This guide explains how RevenueShield utilizes the **Twilio WhatsApp Sandbox** as the real messaging communication provider during development and Razorpay Test Mode demonstrations.

---

## 1. Twilio Sandbox Setup

### Step 1: Obtain Twilio Account SID & Auth Token
1. Log in to your [Twilio Console](https://console.twilio.com/).
2. Copy your **Account SID** and **Auth Token** from the Dashboard header.

### Step 2: Join the Twilio WhatsApp Sandbox
1. In the Twilio Console, navigate to **Messaging** > **Try it out** > **Send a WhatsApp message**.
2. Follow the on-screen instructions from your physical phone:
   - Save the Twilio WhatsApp number (typically `+1 415 523 8886`) as a contact.
   - Send the join code message (e.g. `join <unique-two-words>`) to the Twilio number via WhatsApp.
   - Wait for Twilio's confirmation reply: *"You are all set!"*.

### Step 3: Configure Environment Variables
Add the following configuration to your `.env` file:

```env
# Twilio WhatsApp Credentials
TWILIO_ACCOUNT_SID=ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_WHATSAPP_TO=whatsapp:+919876543210

# Operation Modes
TWILIO_WHATSAPP_MODE=SANDBOX
WHATSAPP_MODE=REAL
EXECUTION_MODE=razorpay_test

# Safety Rules
MAX_WHATSAPP_ATTEMPTS=3
WHATSAPP_COOLDOWN_MINUTES=1440
DND_START_TIME=20:00
DND_END_TIME=08:00
DEFAULT_TIMEZONE=Asia/Kolkata
```

---

## 2. Sandbox Recipient Restriction & Safety Guards

In `TWILIO_WHATSAPP_MODE=SANDBOX`, outgoing WhatsApp messages are strictly locked to the configured `TWILIO_WHATSAPP_TO` recipient.

```
[Target Customer Number]
          │
          ▼
   [Sandbox Check]
   Does target == TWILIO_WHATSAPP_TO?
          ├── YES ──► [Twilio API Dispatch] (Message sent to developer's phone)
          └── NO  ──► [BLOCKED: SANDBOX_RECIPIENT_RESTRICTION] (Safety guard prevents spam)
```

---

## 3. Real-Time End-to-End Recovery Flow

```
Razorpay payment.failed Webhook
             │
             ▼
   [Event Ingestion & Normalization]
             │
             ▼
    [DiagnosisEngine: Root Cause]
             │
             ▼
  [PredictionService: Expected Value]
             │
             ▼
    [DecisionEngine: Recommended Action]
             │
             ▼
    [WhatsAppRecoveryService: Policy Guard]
    ├── Case Status (OPEN)
    ├── Cooldown (1440 min)
    ├── DND Quiet Hours (20:00 - 08:00)
    ├── Max Attempts (3)
    └── Active Promise-to-Pay
             │
             ▼
 [RazorpayPaymentLinkClient: Real Test Link] (https://rzp.io/i/...)
             │
             ▼
 [TwilioWhatsAppClient: Send WhatsApp Message] (Real Sandbox SMS/WA)
             │
             ▼
  [Customer Receives Link & Pays]
             │
             ▼
Razorpay payment.captured Webhook
             │
             ▼
   [Payment Success Stopping Rule]
   ├── Cancel pending WhatsApp jobs
   ├── Mark RecoveryCase = RECOVERED
   ├── Create RecoveryOutcome & LearningExample
   └── Strictly BLOCK all subsequent outreach
```

---

## 4. Message Templates & Personalization

Deterministic templates prevent internal AI leakage:

### English Template
```text
Hi Suresh, your payment of ₹5,000.00 could not be completed.

You can securely complete your payment here:
https://rzp.io/i/plink_xxxxxxxxxxxx

Thank you.
```

### Hinglish Template
```text
Hi Suresh, aapka ₹5,000.00 ka payment complete nahi ho paya.

Aap yahan se securely payment complete kar sakte hain:
https://rzp.io/i/plink_xxxxxxxxxxxx

Thank you.
```

---

## 5. API Reference

### 1. Trigger WhatsApp Recovery
```http
POST /recovery-cases/{case_id}/whatsapp-recovery
Content-Type: application/json

{
  "language": "HINGLISH",
  "dry_run": false
}
```

**Response:**
```json
{
  "case_id": "8f376f5b-9b4e-4b68-8fc8-508b98b0f443",
  "action": "WHATSAPP_PAYMENT_RECOVERY",
  "status": "SENT",
  "payment_link": {
    "url": "https://rzp.io/i/plink_PO123456789",
    "amount": 5000.0,
    "currency": "INR"
  },
  "communication": {
    "id": "c1f7a2d4-1b2c-3d4e-5f6a-7b8c9d0e1f2a",
    "provider": "TWILIO",
    "status": "SENT",
    "provider_message_id": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  }
}
```

### 2. Preview WhatsApp Recovery
```http
GET /recovery-cases/{case_id}/whatsapp-preview?language=ENGLISH
```

### 3. Recovery Dashboard & Timeline
```http
GET /admin/communications/whatsapp/dashboard
```

---

## 6. Troubleshooting

| Issue | Cause | Solution |
| :--- | :--- | :--- |
| `SANDBOX_RECIPIENT_RESTRICTION` | Customer phone does not match `TWILIO_WHATSAPP_TO`. | Update `TWILIO_WHATSAPP_TO` in `.env` or join sandbox with that number. |
| `TWILIO_NOT_CONFIGURED` | Credentials missing in environment. | Ensure `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set in `.env`. |
| `COOLDOWN_PERIOD_ACTIVE` | Less than 24 hours since last message. | Wait for cooldown or set `WHATSAPP_COOLDOWN_MINUTES=0` for testing. |
| `QUIET_HOURS_DND_PROHIBITED` | Current time is between 20:00 and 08:00 IST. | Test during daytime hours or configure `DND_START_TIME`/`DND_END_TIME`. |
