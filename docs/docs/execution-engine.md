# Bounded Recovery Execution Engine Architecture

This document describes the design, execution states, pre-flight safety guard, dual-mode execution (dry-run & Razorpay test mode), idempotency invariants, and money tracking rules implemented in **Step 6**.

---

## 1. Core Execution Principle

No recovery action may directly trigger external communication or payment gateway calls without passing through the complete bounded execution pipeline:

```
[Recommendation (Step 5)]
            │
            ▼
   [Policy Validation]
            │
            ▼
   [Execution Request]
            │
            ▼
[Execution Guard (2nd Safety Layer)]
    ├── 1. Verify RecoveryCase is OPEN
    ├── 2. Verify payment NOT already captured
    ├── 3. Verify action is APPROVED
    ├── 4. Re-verify Policy Engine in real-time
    ├── 5. Verify Promise-to-Pay is NOT active
    ├── 6. Verify NO parallel execution in-flight
    └── 7. Verify Idempotency Key not already succeeded
            │
            ▼
   [RecoveryExecutor]
    ├── Dry-Run Mode ➔ Simulated Provider Response (SIMULATED)
    └── Test Mode ➔ Razorpay Payment Link API (RAZORPAY)
            │
            ▼
[Persist RecoveryExecution Record]
    ├── status = SUCCEEDED / FAILED / BLOCKED
    ├── provider_reference = "plink_..." (or "sim_plink_...")
    └── provider_url = "https://rzp.io/i/..."
            │
            ▼
[Record Immutable AuditLog] ("EXECUTION_STARTED", "EXECUTION_SUCCEEDED")
```

---

## 2. Execution State Machine

```
              ┌──────────────┐
              │   PENDING    │
              └──────┬───────┘
                     │ (Pre-Flight Guard)
        ┌────────────┴────────────┐
        ▼                         ▼
┌──────────────┐          ┌──────────────┐
│  EXECUTING   │          │   BLOCKED    │
└───────┬──────┘          └──────────────┘
        │
   ┌────┴────┐
   ▼         ▼
┌─────────┐ ┌────────┐
│SUCCEEDED│ │ FAILED │
└─────────┘ └────────┘
```

- `PENDING`: Initial state upon execution request receipt.
- `EXECUTING`: Dispatched to executor; lock held via idempotency key.
- `SUCCEEDED`: Provider successfully generated link / payment instruction.
- `FAILED`: Provider error, network timeout, or invalid payload.
- `BLOCKED`: Guard rejected execution due to inactive case, active PTP, or policy change.
- `CANCELLED`: Pending execution invalidated because payment was captured.

---

## 3. Money Tracking Invariant: At-Risk vs Recovered

> [!IMPORTANT]
> **Creating a Payment Link does NOT mean revenue is recovered.**
>
> When `SEND_PAYMENT_LINK` executes successfully:
> - `RecoveryCase.status` remains `OPEN`.
> - `RecoveryCase.amount_at_risk` remains at risk.
> - `RecoveryCase.recovered_amount` remains `None` (or 0).
>
> Revenue is **ONLY** marked recovered when an authentic `payment.captured` webhook is received and verified.

---

## 4. Dual-Mode Operation

### Mode 1: Dry-Run Mode (`EXECUTION_MODE=dry_run`)
- Default mode for development and testing.
- Executes full validation, pre-flight guard checks, and database persistence.
- Generates simulated provider reference (`sim_plink_...`) and URL (`https://simulated.pay/i/...`).
- Zero external HTTP calls made to Razorpay.

### Mode 2: Razorpay Test Mode (`EXECUTION_MODE=razorpay_test`)
- Enabled by setting `EXECUTION_MODE=razorpay_test` in `.env`.
- Uses official Razorpay Test Key (`rzp_test_...`).
- Strictly rejects live keys (`rzp_live_...`) to prevent accidental production impact.
- Converts Decimal amounts to paise integer (`int(amount * 100)`).
- Calls official `POST /v1/payment_links` endpoint with silent notification flags (`notify.email=false`, `notify.sms=false`).

---

## 5. API Endpoint

```http
POST /recovery-cases/{case_id}/execute
```

Response:
```json
{
  "execution_id": "750fe684-e462-4676-bd64-618cb2ba8c5a",
  "case_id": "b4ddcb43-bd98-4fcf-9397-59ae88e7cf19",
  "action_id": "c63b54f9-5bf6-45d5-bda1-0c8c3c00a5fe",
  "action_type": "SEND_PAYMENT_LINK",
  "status": "SUCCEEDED",
  "provider": "SIMULATED",
  "provider_reference": "sim_plink_56f93e714c76",
  "provider_url": "https://simulated.pay/i/sim_plink_56f93e714c76",
  "amount": 650.0,
  "currency": "INR",
  "idempotency_key": "b4ddcb43-bd98-4fcf-9397-59ae88e7cf19_c63b54f9-5bf6-45d5-bda1-0c8c3c00a5fe_SEND_PAYMENT_LINK",
  "error_code": null,
  "error_message": null,
  "created_at": "2026-08-22T02:52:25.258054+00:00"
}
```
