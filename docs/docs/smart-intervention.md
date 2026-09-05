# Smart Payment Recovery Intervention Engine

This document details the architecture, state machine, policy enforcement, idempotency, stopping rules, webhook reconciliation, and attribution models implemented in **Step 9 (Smart Payment Recovery Intervention)**.

---

## 1. System Architecture & End-to-End Flow

```
[Failed Payment Event] ──► [RecoveryCase (OPEN)]
                                  │
                                  ▼
                        [PredictionService]
                                  │ (Probability & Expected Recovered Value)
                                  ▼
                         [DecisionEngine]
                                  │ (Selects Best Action e.g. SEND_PAYMENT_LINK)
                                  ▼
                           [PolicyEngine]
                                  │
                   ┌──────────────┴──────────────┐
                   ▼ (APPROVED)                  ▼ (BLOCKED)
       [InterventionService]           [Intervention(BLOCKED)]
       ├── Concurrency / Idempotency    └── AuditLog(POLICY_BLOCKED)
       ├── [RazorpayPaymentLinkClient]
       │   └── Creates / Reuses RecoveryPaymentLink
       ├── [NotificationService]
       │   └── DevelopmentNotificationProvider (Masked copy)
       └── Updates RecoveryCase (IN_PROGRESS)
                   │
                   ▼
          [Customer Pays Link]
                   │
                   ▼
     [Razorpay Webhook: payment.captured]
                   │
                   ▼
       [Stopping Rule Engine]
       ├── Mark RecoveryCase (RECOVERED)
       ├── Mark RecoveryPaymentLink (PAID)
       ├── Mark Intervention (SUCCEEDED)
       ├── Cancel any pending outreach
       ├── Create RecoveryOutcome (Attribution: DIRECT / LIKELY)
       ├── Finalize LearningExample (label = 1)
       └── AuditLog (PAYMENT_CAPTURED, INTERVENTION_COMPLETED, CASE_RECOVERED)
```

---

## 2. Key Components

### 2.1 InterventionService (`app/services/intervention_service.py`)
- **Orchestration Facade**: Glues prediction scoring, decision recommendation, policy compliance, payment link generation, and notification delivery.
- **Idempotency & Concurrency**: Checks if an active payment link (`CREATED` or `SENT`) or active intervention (`EXECUTING` or `SENT`) already exists for the case. Reuses existing resources to avoid duplicate external charges or spamming customers.
- **Policy Enforcement**: Calls `PolicyEngine` before taking action. If blocked (e.g. quiet hours, retry cap exceeded, active Promise-to-Pay), records an `Intervention` in `BLOCKED` status and prevents payment link creation.

### 2.2 RazorpayPaymentLinkClient (`app/integrations/razorpay/payment_link_client.py`)
- Dedicated HTTP transport wrapper for official Razorpay Payment Links API (`POST /v1/payment_links`, `GET /v1/payment_links/{id}`, `POST /v1/payment_links/{id}/cancel`).
- Automatically manages Basic Authentication using `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`.
- Disables Razorpay's built-in SMS/email notifications (`notify: {sms: false, email: false}`) to guarantee our system retains complete control over customer communications and delivery timing.
- Enforces bounded exponential backoff retries on 5xx server errors and network timeouts, while failing fast on 4xx client errors.

### 2.3 NotificationService (`app/services/notification_service.py`)
- Abstract interface with `DevelopmentNotificationProvider` implementation.
- Generates clean, customer-facing copy without exposing internal AI models, risk scores, failure codes, or raw IDs:
  ```text
  Your recent payment of ₹5,000.00 could not be completed.
  You can securely complete the payment here:
  https://rzp.io/i/plink_12345
  ```
- Protects customer privacy by masking phone numbers and email addresses in all application logs (e.g. `+9198*****3210`, `u***r@example.com`).
- Persists full communication history in `communication_logs` table.

---

## 3. Stopping Rule & Webhook Reconciliation

When a verified `payment.captured` webhook arrives (or payment is ingested via API sync):
1. **Case State**: `RecoveryCase.status` is set to `RECOVERED` and `recovered_amount` is recorded.
2. **Intervention State**: Active `Intervention` records are transitioned to `SUCCEEDED` with `completed_at` timestamp.
3. **Payment Link State**: Active `RecoveryPaymentLink` records are set to `PAID`, stamped with `paid_at` and `razorpay_payment_id`.
4. **Outreach Cancellation**: All pending, planned, or recommended actions are immediately cancelled (`status = 'CANCELLED'`) to prevent pestering the customer.
5. **Attribution & Learning**: `RecoveryOutcome` is created with attribution classified as `DIRECT` (if within 24h) or `LIKELY` (if within 72h), and the pending `LearningExample` is finalized with binary `label = 1`.

---

## 4. API Endpoints

| Endpoint | Method | Purpose |
| :--- | :--- | :--- |
| `/recovery-cases/{id}/interventions` | `POST` | Execute intervention (`SEND_PAYMENT_LINK`) with full policy and idempotency guards. |
| `/recovery-cases/{id}/intervention-preview` | `GET` | Preview recommended action, ML probability, expected value, and policy status without side effects. |
| `/admin/interventions/dashboard` | `GET` | Aggregated metrics: revenue at risk, recovered amount, recovery rate, active cases, and predicted vs actual value. |

---

## 5. Execution Modes

- **`INTERVENTION_MODE=DRY_RUN`**: Simulates payment link creation (`https://rzp.io/i/plink_sim_...`) and dispatches development notifications without calling external Razorpay APIs.
- **`INTERVENTION_MODE=RAZORPAY_TEST`**: Uses verified Razorpay Test Mode keys (`rzp_test_...`) to create real test payment links on Razorpay's sandbox. Production keys are strictly blocked in test mode.
