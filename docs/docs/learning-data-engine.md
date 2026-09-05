# Recovery Outcome & Learning Data Engine Architecture

This document outlines the architecture, data models, causal attribution rules, point-in-time feature snapshotting, and future machine learning formulation implemented in **Step 7**.

---

## 1. Outcome & Learning Pipeline

```
[payment.failed] ➔ [Diagnosis] ➔ [Decision] ➔ [Feature Snapshot & Pending LearningExample]
                                       │
                                       ▼
                             [Execution (Dry-Run / Live)]
                                       │
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
        [payment.captured Webhook]           [Observation Window Expired (72h)]
                    │                                     │
                    ▼                                     ▼
          [OutcomeEngine]                       [OutcomeEngine]
          ├── Calculate recovery %              ├── outcome_type = NOT_RECOVERED
          ├── Attribution (DIRECT/ORGANIC)      ├── amount_recovered = 0.0
          ├── Time to recovery (seconds)        └── label = 0
          ├── Create RecoveryOutcome
          ├── Finalize LearningExample (label=1/0)
          └── Cancel Pending Actions
                    │
                    ▼
          [Immutable Audit Logs]
          ("OUTCOME_CREATED", "RECOVERY_CASE_RECOVERED", "LEARNING_EXAMPLE_FINALIZED")
```

---

## 2. Point-in-Time Correctness & Anti-Leakage Guarantee

A common failure mode in machine learning systems is **target leakage** (using information from the future as inputs to predict the future).

### Invariant Rules:
1. **Pre-Decision Snapshot**: When a decision/recommendation is generated, `LearningDataService.create_feature_snapshot()` takes an immutable JSON snapshot containing only features known at decision time (amount at risk, customer historical success rate, diagnosis category, risk score, payment method).
2. **Strict Exclusion of Target Variables**: Variables such as `amount_recovered`, `time_to_recovery_seconds`, `payment_capture_time`, and `outcome_type` are strictly excluded from the input feature set.
3. **Data Quality Validation**: `LearningDataService.validate_data_quality()` automatically scans every learning example and flags any anomalies, impossible values, or target leakage.

---

## 3. Conservative Attribution Mechanics

Revenue recovery attribution determines whether captured revenue was caused by our intervention or occurred organically.

| Attribution Type | Condition |
| :--- | :--- |
| **`DIRECT`** | Payment captured within 24 hours of executing a recovery action (e.g. Payment Link). |
| **`LIKELY`** | Payment captured after 24 hours but within the action's observation window (e.g., 72 hours for Payment Links). |
| **`ORGANIC`** | Payment captured without any prior executed action or after the observation window expired. |
| **`UNCERTAIN`** | Anomaly detected (e.g. capture timestamp precedes execution timestamp). |

---

## 4. Observation Windows

Configured in [`app/outcomes/base.py`](file:///c:/Users/Ankit%20Kumar/OneDrive/Desktop/RevenueShield/revenue-recovery/backend/app/outcomes/base.py):
- `SEND_PAYMENT_LINK`: 72 hours
- `SEND_WHATSAPP_REMINDER`: 72 hours
- `VOICE_OUTREACH`: 7 days (168 hours)
- `RETRY_PAYMENT`: 24 hours

---

## 5. Binary Training Label Formulation

For initial predictive modeling, each finalized `LearningExample` is labeled:
- **`label = 1`**: Successful recovery attributable to the intervention (`outcome_type in [RECOVERED, PARTIALLY_RECOVERED]` AND `attribution in [DIRECT, LIKELY]`).
- **`label = 0`**: Non-recovery, failed intervention, expired observation window, or organic recovery.

---

## 6. Future Machine Learning & Value-Based Optimization Formulation

Future ML models will not simply predict binary success; they will estimate conditional recovery probabilities across all candidate actions:

$$P(\text{recovery} \mid \mathbf{x}, a) \quad \text{for } a \in \mathcal{A}$$

Where:
- $\mathbf{x}$ is the point-in-time feature snapshot.
- $a$ is the candidate recovery action (e.g. `RETRY_PAYMENT`, `SEND_PAYMENT_LINK`, `VOICE_OUTREACH`).

### Expected Recovery Value Optimization:
Rather than choosing the action that maximizes raw probability $\arg\max_a P(\text{recovery} \mid \mathbf{x}, a)$, the decision engine will maximize **Expected Recovered Value** minus **Intervention Cost**:

$$a^* = \arg\max_{a \in \mathcal{A}} \Big( P(\text{recovery} \mid \mathbf{x}, a) \times \text{amount\_at\_risk} - \text{cost}(a) \Big)$$

*Example*:
- ₹500 at 95% probability yields ₹475 expected value.
- ₹20,000 at 70% probability yields ₹14,000 expected value.
- The engine rationally prioritizes high-impact interventions on high-value balances.

---

## 7. API Endpoints

### Get Case Outcome
```http
GET /recovery-cases/{case_id}/outcome
```
```json
{
  "case_id": "3b294c7b-7df7-4569-b5d1-55c32faeb26c",
  "outcome_type": "RECOVERED",
  "amount_at_risk": 3000.0,
  "amount_recovered": 3000.0,
  "recovery_percentage": 100.0,
  "attribution": "DIRECT",
  "time_to_recovery_seconds": 34.2,
  "occurred_at": "2026-08-22T03:15:00Z"
}
```

### Get Learning Example
```http
GET /learning/examples/{case_id}
```
```json
{
  "example_id": "993f7734-7a1a-4d43-98fe-89dc563b717b",
  "case_id": "3b294c7b-7df7-4569-b5d1-55c32faeb26c",
  "diagnosis_category": "AUTHENTICATION_FAILURE",
  "action_type": "SEND_PAYMENT_LINK",
  "decision_score": 0.9,
  "decision_confidence": 0.82,
  "policy_allowed": true,
  "amount_at_risk": 3000.0,
  "is_finalized": true,
  "label": 1,
  "outcome_type": "RECOVERED",
  "amount_recovered": 3000.0,
  "recovery_percentage": 100.0,
  "attribution": "DIRECT",
  "time_to_recovery_seconds": 34.2,
  "feature_snapshot": {
    "amount_at_risk": 3000.0,
    "diagnosis_category": "AUTHENTICATION_FAILURE",
    "customer_success_rate": 0.8,
    "payment_method": "CARD"
  }
}
```
