# Webhook & Event Ingestion Architecture

This document describes the architectural design and flow of the event ingestion pipeline in the **Revenue Recovery AI** platform.

---

## High-Level Architecture

```
[Razorpay / External Gateway]
         │
         │ (HTTP POST with raw payload & headers)
         ▼
[POST /webhooks/razorpay] ───────────────► [HMAC-SHA256 Signature Verification]
(FastAPI Route)                                     │
                                                    │ (Passes raw body)
                                                    ▼
                                           [RazorpayAdapter]
                                           (Normalizes to NormalizedEvent)
                                                    │
                                                    ▼
                                           [EventProcessor Service]
                                                    │
                   ┌────────────────────────────────┴───────────────────────────────┐
                   ▼                                                                ▼
          [Idempotency Check]                                            [Customer Mapping]
     (x-razorpay-event-id in DB)                                    (External ID / Email / Phone)
                   │                                                                │
                   ▼                                                                ▼
         [Store Event & Payload] ─────────────────────────────────────────► [RecoveryCase Transition]
              (PostgreSQL)                                                • payment.failed  ➔ OPEN
                                                                          • payment.captured ➔ RECOVERED
                                                                                    │
                                                                                    ▼
                                                                           [Immutable AuditLog]
```

---

## 1. Why Cryptographic Signature Verification is Mandatory

Payment webhooks arrive over public HTTP connections. An attacker could forge webhook payloads, falsely triggering recovery workflows or marking unpaid invoices as recovered.

- **Algorithm**: HMAC-SHA256.
- **Key**: `RAZORPAY_WEBHOOK_SECRET`.
- **Raw Request Body**: Verification **must** occur against the raw binary bytes before JSON parsing. Any intermediate serialization or whitespace mutation alters the cryptographic hash.
- **Timing-Safe Comparison**: `hmac.compare_digest` is used to prevent side-channel timing attacks.

---

## 2. Event Idempotency Guarantees

Payment gateways retry webhooks upon network timeouts or transient errors. To prevent duplicate cases or double recovery:
1. **Header-Based Idempotency**: The gateway's unique `x-razorpay-event-id` is mapped to `Event.external_event_id`.
2. **Application Check**: `EventProcessor` queries existing events by `external_event_id`. If found, it immediately skips processing and safely returns `status="duplicate"`.
3. **Database Uniqueness**: The `events.external_event_id` column carries a strict SQL unique constraint index to eliminate race conditions under high concurrent throughput.

---

## 3. Provider Decoupling via NormalizedEvent

The core business domain and recovery state machine are decoupled from gateway-specific JSON formats.

```python
NormalizedEvent(
    event_id="evt_...",
    event_type="payment.failed",
    source="RAZORPAY",
    amount=Decimal("450.00"),
    currency="INR",
    external_customer_id="...",
    customer_email="...",
    external_payment_id="pay_...",
    failure_code="INSUFFICIENT_FUNDS",
    failure_description="...",
    metadata={...},
    raw_payload={...}
)
```

The raw original payload is preserved unmodified in PostgreSQL `Event.payload` (JSONB) for audit trails and machine learning feature extraction.

---

## 4. Recovery Lifecycle Transitions

### `payment.failed`
1. Payment record is created/updated with `status="FAILED"` and diagnostic failure codes (`error_code`, `error_reason`, `error_description`).
2. A central `RecoveryCase` is opened (`case_type="PAYMENT_FAILURE"`, `status="OPEN"`, `amount_at_risk=amount`).
3. An `AuditLog` entry documents the opened recovery case.

### `payment.captured`
1. Payment record status transitions to `SUCCESS` with `paid_at=timestamp`.
2. Open `RecoveryCase` is located and transitioned to `status="RECOVERED"`, setting `recovered_amount` and `closed_at`.
3. An `AuditLog` records the recovery outcome for compliance and reporting.

---

## 5. Lightweight Webhook Execution

The webhook endpoint only executes lightweight database validation, persistence, and state transitions. Heavy processing (such as LLM reasoning, ML inference, and external messaging) is reserved for asynchronous background workers in later phases.
