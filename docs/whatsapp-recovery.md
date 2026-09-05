# WhatsApp Recovery Agent Engine

This document details the architecture, provider abstraction, message templating, personalization, Hinglish support, compliance policies, DND quiet hours, idempotency, stopping rules, and webhook status reconciliation implemented in **Step 10 (WhatsApp Recovery Agent)**.

---

## 1. System Architecture & End-to-End Flow

```
[Failed Payment Event] ──► [RecoveryCase (OPEN)]
                                  │
                                  ▼
                        [PredictionService]
                                  │ (Probability & Expected Value for SEND_WHATSAPP_REMINDER)
                                  ▼
                         [DecisionEngine]
                                  │
                                  ▼
                   [CommunicationScheduler (Policy)]
                   ├── Case Status (OPEN / IN_PROGRESS)
                   ├── Promise-to-Pay Check (Paused if ACTIVE)
                   ├── Attempt Cap (MAX_WHATSAPP_ATTEMPTS = 3)
                   ├── Cooldown (WHATSAPP_COOLDOWN_MINUTES = 1440)
                   ├── DND / Quiet Hours (20:00 - 08:00 in Customer Timezone)
                   └── Customer Consent (whatsapp_allowed, dnd_enabled)
                                  │
                   ┌──────────────┴──────────────┐
                   ▼ (APPROVED)                  ▼ (BLOCKED)
       [CommunicationOrchestrator]     [Communication(BLOCKED)]
       ├── [RecoveryMessageGenerator]  └── AuditLog(WHATSAPP_BLOCKED)
       │   ├── English: PAYMENT_RECOVERY_EN_V1
       │   └── Hinglish: PAYMENT_RECOVERY_HI_V1
       ├── [RecoveryPaymentLink] (Reused/Created)
       ├── Idempotency Key Guard
       └── [WhatsAppProvider]
           ├── DevelopmentWhatsAppProvider (is_simulated=True)
           └── TwilioWhatsAppProvider (Live Messages API)
                   │
                   ▼
       [Customer Receives WhatsApp & Pays]
                   │
                   ▼
     [Razorpay Webhook: payment.captured]
                   │
                   ▼
       [Stopping Rule Engine]
       ├── Cancel any pending/queued WhatsApp messages (status: CANCELLED)
       ├── Mark status: STOPPED_AFTER_RECOVERY
       ├── Transition RecoveryCase -> RECOVERED
       ├── Reconcile RecoveryPaymentLink -> PAID
       └── Block all future WhatsApp outreach on this case
```

---

## 2. Provider Abstraction

The system decouples recovery business logic from external messaging APIs via the `WhatsAppProvider` interface:

```python
class WhatsAppProvider(ABC):
    @abstractmethod
    def send_message(self, recipient: str, message: str, template_name: str, context: Optional[dict]) -> WhatsAppSendResult: ...
    @abstractmethod
    def get_message_status(self, provider_message_id: str) -> WhatsAppStatusResult: ...
    @abstractmethod
    def verify_webhook_signature(self, payload_bytes: bytes, signature: Optional[str], ...) -> bool: ...
```

### 2.1 DevelopmentWhatsAppProvider
- Default provider in `dry_run` and development environments.
- Returns simulated IDs (`wa_sim_...`) and sets `is_simulated=True`.
- Keeps in-memory and database records without calling external network APIs.
- Masks recipient phone numbers in all log outputs (`+919*****3210`).

### 2.2 TwilioWhatsAppProvider
- Production provider using Twilio Messages REST API (`POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json`).
- Automatically enabled when `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_WHATSAPP_NUMBER` are configured in `.env`.
- Implements bounded exponential backoff retries on 5xx/network timeouts and fails fast on non-retryable 4xx client errors.
- Never logs credentials or unmasked phone numbers.

---

## 3. Deterministic Templates & Personalization

The template engine (`RecoveryMessageGenerator`) renders customer-safe messages with strict anti-leakage guarantees:

### 3.1 English (`PAYMENT_RECOVERY_EN_V1`)
- **With Name**: `"Hi {first_name}, your payment of {formatted_amount} could not be completed. You can securely complete it here: {payment_link}"`
- **Generic**: `"Your payment of {formatted_amount} could not be completed. You can securely complete it here: {payment_link}"`

### 3.2 Hinglish (`PAYMENT_RECOVERY_HI_V1`)
- **With Name**: `"Hi {first_name}, aapka {formatted_amount} ka payment complete nahi ho paya. Aap yahan se securely payment complete kar sakte hain: {payment_link}"`
- **Generic**: `"Aapka {formatted_amount} ka payment complete nahi ho paya. Aap yahan se securely payment complete kar sakte hain: {payment_link}"`

> [!NOTE]
> **Anti-Leakage Guarantee**: Technical failure strings (`BAD_REQUEST_ERROR`, `PAYMENT_AUTHENTICATION_FAILURE`), internal model scores, and AI diagnostic metrics are strictly forbidden from customer-facing copy.

---

## 4. Safety Policies & Scheduling

| Policy Rule | Config Parameter | Default | Behavior |
| :--- | :--- | :--- | :--- |
| **Max Attempts** | `MAX_WHATSAPP_ATTEMPTS` | `3` | Maximum 3 successful/queued outreach messages per recovery case. |
| **Cooldown** | `WHATSAPP_COOLDOWN_MINUTES` | `1440` (24h) | Minimum 24 hours required between consecutive messages for the same case. |
| **Quiet Hours / DND** | `WHATSAPP_DND_START_HOUR`, `WHATSAPP_DND_END_HOUR` | `20:00` – `08:00` | No customer messages during quiet hours in the customer's timezone (or `Asia/Kolkata`). |
| **Promise-to-Pay** | N/A | Active record | If customer has an active Promise-to-Pay agreement, routine outreach is paused. |
| **Customer Consent** | `whatsapp_allowed`, `dnd_enabled` | `True`, `False` | Explicit opt-outs and DND flags immediately block outreach. |

---

## 5. Communication Idempotency & Unique Keys

To prevent duplicate outreach caused by repeated API calls or retry triggers, each communication attempt generates a deterministic idempotency key:

$$\text{idempotency\_key} = \text{comm\_\{recovery\_case\_id\}\_WHATSAPP\_\{attempt\_number\}}$$

This key is enforced with a database unique constraint (`uq_communications_idempotency_key`). Repeated submissions return the existing `Communication` record without re-sending.

---

## 6. Payment Success Stopping Rule

When a `payment.captured` event arrives:
1. `OutcomeEngine.process_payment_capture` triggers `CommunicationOrchestrator.stop_whatsapp_on_recovery`.
2. Any `QUEUED` or `GENERATED` communication is immediately marked `CANCELLED` (`cancelled_at = now`).
3. Audit log `WHATSAPP_STOPPED_AFTER_RECOVERY` is emitted.
4. Any future attempt to send WhatsApp on the recovered case is strictly `BLOCKED` with `CASE_ALREADY_RECOVERED_OR_CLOSED`.

---

## 7. API Reference

| Method | Path | Description |
| :--- | :--- | :--- |
| `POST` | `/recovery-cases/{id}/communications/whatsapp` | Dispatch or queue WhatsApp message (`{"language": "ENGLISH" \| "HINGLISH"}`). |
| `GET` | `/recovery-cases/{id}/communications/whatsapp/preview` | Preview personalized message and policy status without side effects. |
| `POST` | `/webhooks/whatsapp/status` | Receive asynchronous delivery callbacks (`SENT`, `DELIVERED`, `READ`, `FAILED`). |
| `GET` | `/admin/communications/whatsapp/dashboard` | Performance analytics (messages sent, delivered, recovery rate, recovered revenue). |
