# Razorpay Historical Data Ingestion & Synchronization Engine

This document details the architecture, data models, synchronization mechanics, state transition invariants, and deduplication guarantees implemented in **Step 8A**.

---

## 1. System Architecture

```
[Razorpay REST API]                       [Razorpay Webhooks]
        │ (GET /v1/payments)                       │ (payment.failed / payment.captured)
        ▼                                          ▼
[RazorpayPaymentClient]                   [HMAC Verification & Adapter]
        │                                          │
        └───────────────────┬──────────────────────┘
                            ▼
                  [PaymentNormalizer]
                            │
                            ▼
                   [PaymentRepository]
                   (Idempotent Upsert + State Invariant Guard)
                            │
                            ▼
                    [PostgreSQL DB]
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      [Failed Payment]          [Captured Payment]
      (If no case exists)       (If open case exists)
               │                         │
               ▼                         ▼
      [Diagnosis Engine]          [OutcomeEngine]
      & RecoveryCase Open         & Outcome / Learning Finalize
```

---

## 2. API vs Webhook Complementary Roles

| Vector | Ingestion Mechanism | Purpose & Role |
| :--- | :--- | :--- |
| **Razorpay API** | Pull / Batch Paginated Synchronization (`GET /v1/payments`) | Backfills historical data, catches missed webhook events, reconciles state across date ranges, and builds offline training datasets. |
| **Razorpay Webhooks** | Push / Real-time Event Stream | Provides sub-second trigger for failed payment diagnosis, automated recovery recommendations, and real-time capture notifications. |

Both pathways channel into the single `PaymentNormalizer` and `PaymentRepository.upsert_payment()` function, guaranteeing schema and business rule parity.

---

## 3. Pagination Architecture

The `RazorpayPaymentClient` interacts with the official Razorpay Payments API:
- Respects the Razorpay API page limit of `count <= 100`.
- Implements offset traversal via `skip` parameter:
  ```python
  skip = 0
  while True:
      page = client.fetch_payments(from_timestamp, to_timestamp, count=100, skip=skip)
      items = page.get("items", [])
      if not items:
          break
      # Upsert items...
      if len(items) < count:
          break
      skip += len(items)
  ```
- Gracefully handles variable page lengths and stops cleanly.

---

## 4. Synchronization Lifecycle & Checkpoints

Every synchronization session is tracked in the `sync_checkpoints` table:
- **`status`**: `RUNNING` ➔ `SUCCEEDED` / `FAILED`
- **Fields**: `id`, `source`, `started_at`, `completed_at`, `from_timestamp`, `to_timestamp`, `records_fetched`, `records_created`, `records_updated`, `error_message`.
- **Metrics Dashboard**: `GET /admin/razorpay/sync/data-quality` aggregates total transaction volumes, failure ratios, and last sync/webhook timestamps.

---

## 5. Idempotency & State Monotonicity Guards

### Idempotency Invariant:
Razorpay payment ID (`external_payment_id` e.g. `pay_xxx`) is universally unique.
- Running a synchronization multiple times will **never** insert duplicate rows (`records_created = 0`, `records_updated > 0`).

### State Transition Monotonicity:
A payment that has reached `CAPTURED` or `REFUNDED` status cannot be regressed to `FAILED` or `CREATED` by out-of-order event arrivals or delayed webhooks.
```python
if existing.status in TERMINAL_SUCCESS_STATES and incoming.status not in TERMINAL_SUCCESS_STATES:
    # Log warning, preserve CAPTURED status, update metadata only
```

---

## 6. Security & Credential Isolation

- Credentials (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`) are never logged in plaintext.
- Basic Auth headers are injected securely over HTTPS via `httpx`.
- Admin endpoints (`/admin/razorpay/sync/*`) are restricted and designed for internal operations.

---

## 7. Recovery Pipeline Integration

When historical or batch payments are ingested:
1. **`FAILED` payments**: If no existing `RecoveryCase` references the payment, `RazorpayPaymentSyncService` creates an initial `Event` and opens a `RecoveryCase`, automatically running the Diagnosis and Decision Engines without creating duplicates.
2. **`CAPTURED` payments**: If an open recovery case exists for the payment, `OutcomeEngine.process_payment_capture()` is invoked to calculate recovery percentage, close the case, and finalize the learning dataset example.

---

## 8. Admin API Reference

### Trigger Payment Sync
```http
POST /admin/razorpay/sync/payments
Content-Type: application/json

{
  "from": "2026-08-01T00:00:00Z",
  "to": "2026-08-22T23:59:59Z",
  "batch_size": 100
}
```
**Response**:
```json
{
  "sync_id": "84d567fe-9e0c-4ab4-b771-477080e722a4",
  "status": "SUCCEEDED",
  "records_fetched": 25,
  "records_created": 20,
  "records_updated": 5,
  "from": "2026-08-01T00:00:00+00:00",
  "to": "2026-08-22T23:59:59+00:00",
  "started_at": "2026-08-22T03:40:00+00:00",
  "completed_at": "2026-08-22T03:40:03+00:00",
  "error_message": null
}
```

### Get Ingestion Data Quality Metrics
```http
GET /admin/razorpay/sync/data-quality
```
**Response**:
```json
{
  "total_payments": 150,
  "successful_payments": 110,
  "failed_payments": 40,
  "unknown_status_payments": 0,
  "total_amount": 185000.0,
  "failed_amount": 42000.0,
  "captured_amount": 143000.0,
  "last_sync_time": "2026-08-22T03:40:03+00:00",
  "last_webhook_time": "2026-08-22T03:30:15+00:00"
}
```
