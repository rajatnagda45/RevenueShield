# Diagnosis & Root Cause Engine Architecture

This document describes the architectural design, root cause categories, evidence capture, risk scoring, and recovery prediction baseline implemented in **Step 4** of the Revenue Recovery AI platform.

---

## High-Level Architecture

```
[Incoming payment.failed Webhook]
                │
                ▼
      [Event Processing Engine]
                │
                ▼
      [RecoveryCase (OPEN)]
                │
                ▼
  [Customer Intelligence Service] ──────────► [Extracts Historical Features]
                │                              (Attempts, Success Rate, Recent Failures, LTV)
                ▼
    [Rule-Based Diagnosis Engine]
    (diagnosis_engine_v1)
      ├── Root Cause Categorization (9 Controlled Categories)
      ├── Heuristic Confidence (0.0 to 1.0)
      ├── Deterministic Reproducible Explanation
      ├── Structured Evidence Compilation
      ├── Revenue Risk Scorer (0 to 100)
      └── Baseline Recovery Predictor (0.0 to 1.0)
                │
                ▼
    [Persist Diagnosis & Update RecoveryCase]
                │
                ▼
      [Immutable AuditLog] ("DIAGNOSIS_CREATED")
```

---

## 1. Why Diagnosis is Decoupled from Ingestion

1. **Separation of Concerns**: Webhook ingestion is strictly responsible for transport security (HMAC verification) and idempotency. The Diagnosis Engine is a core analytical domain service.
2. **Pluggable & Extensible Architecture**: Diagnosis and recovery prediction implement abstract protocols (`DiagnosisEngine`, `RecoveryPredictor`). The initial implementation (`RuleBasedDiagnosisEngine`, `HeuristicRecoveryPredictor`) can later be upgraded to ML/LLM engines without rewriting the ingestion or state machine.
3. **Re-evaluable**: As new diagnostic signals or customer actions arrive (e.g. partial payments, updated cards), cases can be re-diagnosed without reprocessing the raw webhook.

---

## 2. Root Cause Categories (Controlled Vocabulary)

| Category | Typical Gateway Signals | Description |
| :--- | :--- | :--- |
| `INSUFFICIENT_FUNDS` | `insufficient_funds`, `insufficient balance` | Customer account deficit; ideal for scheduled smart retries. |
| `BANK_TECHNICAL_FAILURE` | `gateway_timeout`, `bank_offline`, `switch_down` | Temporary banking switch downtime; high recovery propensity. |
| `BANK_DECLINE` | `do_not_honor`, `generic_decline`, `not_permitted` | Bank card controls or cardholder limit decline. |
| `AUTHENTICATION_FAILURE` | `incorrect_otp`, `3d_secure`, `auth_failed` | Customer failed 2FA / OTP; resolved with direct link reminder. |
| `PAYMENT_METHOD_FAILURE` | `expired_card`, `card_inactive`, `invalid_vpa` | Payment instrument permanently invalid; requires update. |
| `USER_FRICTION` | `user_cancelled`, `session_expired` | Customer abandoned or dropped off from payment window. |
| `MERCHANT_CONFIGURATION` | `currency_not_supported`, `invalid_api_key` | Merchant account or gateway configuration issue. |
| `POSSIBLE_FRAUD_OR_SECURITY` | `risk_threshold`, `stolen_card`, `security_violation` | Flagged by fraud filters or restricted cards. |
| `UNKNOWN` | Missing or unmapped error codes | Incomplete gateway diagnostic details. |

---

## 3. Customer Intelligence & Feature Store

The `CustomerIntelligenceService` extracts historical behavior metrics prior to diagnosis:
- `total_attempts`, `successful_count`, `failed_count`
- `success_rate` ($0.0 \dots 1.0$)
- `recent_success_count`, `recent_failure_count` (last 30 days)
- `avg_transaction_amount`
- `consecutive_failures`
- `previous_recovery_cases_count`, `previous_recovered_amount`

---

## 4. Revenue Risk Scoring (0 to 100)

The `NormalizedRiskScorer` combines:
1. **Amount Magnitude Component (0–30 pts)**: Log-scaled ($\min(30, \log_{10}(\text{amount} + 10) \times 5)$) to gracefully normalize values from ₹100 to ₹1,000,000.
2. **Category Severity (0–40 pts)**: Intrinsic severity of failure category (e.g. Fraud = 40, Technical Failure = 10).
3. **Customer History (0–30 pts)**: Churn risk based on failure rate and consecutive failures.

---

## 5. Baseline Recovery Probability (0.05 to 0.98)

The `HeuristicRecoveryPredictor` evaluates:
- Base recovery rate per category (e.g., Technical = 0.85, Insufficient Funds = 0.75, Method Failure = 0.40, Fraud = 0.15).
- Customer relationship modifier ($+0.10$ for high success rate $\ge 80\%$, $+0.05$ for prior successful recovery, $-0.10$ for $\ge 3$ consecutive failures).
- High transaction friction modifier ($-0.05$ for ticket size $> 50,000$).

---

## 6. Engine Versioning & Observability

- Every diagnosis stores `engine_version` (e.g. `diagnosis_engine_v1`).
- Every diagnosis records an `AuditLog` entry (`action="DIAGNOSIS_CREATED"`) for explainability and compliance.
- High-level logs capture `DIAGNOSIS_STARTED` and `DIAGNOSIS_COMPLETED` with masked credentials.

---

## 7. API Access

Retrieve diagnosis details via:
```http
GET /recovery-cases/{case_id}/diagnosis
```
Response:
```json
{
  "case_id": "c5ab3001-5c48-4a6c-b4c1-1a0478172ced",
  "category": "INSUFFICIENT_FUNDS",
  "failure_code": "BAD_REQUEST_ERROR",
  "explanation": "Payment failed due to insufficient customer account balance...",
  "confidence": 0.92,
  "risk_score": 39.6,
  "recovery_probability": 0.85,
  "engine_version": "diagnosis_engine_v1",
  "evidence": {
    "error_reason": "insufficient_funds",
    "matched_rule": "rule_insufficient_funds",
    "customer_history": { ... }
  },
  "created_at": "2026-08-21T19:20:35.946189+00:00"
}
```
