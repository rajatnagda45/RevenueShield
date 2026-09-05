# Self-Learning Feedback Loop Architecture

## 1. What Self-Learning Means

In RevenueShield, **Self-Learning** does **NOT** mean unconstrained online weight updates after every single transaction. Such continuous updates risk runaway catastrophic forgetting and policy instability.

Instead, **Self-Learning** is implemented as a **Controlled, Audited Batch-Learning Closed Loop**:

$$\text{PREDICTION} \to \text{ACTION} \to \text{OBSERVATION} \to \text{OUTCOME} \to \text{ATTRIBUTION} \to \text{DATASET} \to \text{RETRAINING} \to \text{EVALUATION} \to \text{PROMOTION}$$

---

## 2. Outcome Realization & Attribution Windows

When a customer settles a failed payment via Razorpay (`payment.captured`), the system credits the recovery through a deterministic **Attribution Engine**:

- **Attribution Window**: Configurable (Default: `24 hours`).
- **Attribution Rule**: The last eligible recovery intervention executed within 24 hours prior to payment capture receives **PRIMARY** attribution ($w = 1.0$).
- **Outside Window**: If payment occurs $> 24\text{h}$ without intermediate outreach, the recovery is labeled **UNCERTAIN**.

---

## 3. Training Eligibility & Anti-Poisoning Gates

Every resolved recovery intervention produces a `LearningExample`. However, only high-integrity records enter model retraining datasets:

| Gate Criterion | Condition | Ineligible Exclusion Reason |
| :--- | :--- | :--- |
| Outcome Finality | Outcome must be known (`RECOVERED`, `NOT_RECOVERED`, etc.) | `OUTCOME_UNKNOWN` |
| Decision Features | Point-in-time features must exist without leakage | `MISSING_FEATURES` / `LEAKAGE_DETECTED` |
| Operator Overrides | Interventions manually forced by human operators are excluded | `MANUAL_OPERATOR_OVERRIDE` |
| Sandbox Separation | Examples are tagged by `environment_type` (`TEST`, `LIVE`, `SIMULATED`) | `ENVIRONMENT_MISMATCH` |

---

## 4. Retraining, Promotion Quality Gates, & Rollback

1. **Retraining Trigger**: Batch retraining is triggered automatically when $\ge 100$ new eligible learning examples accumulate (`RETRAINING_SCHEDULE_THRESHOLD = 100`) or on-demand via `POST /ml/retrain`.
2. **Promotion Gate**: A candidate model is only promoted to `ACTIVE` if its out-of-time **Log Loss** and **Brier Score** improve upon the active champion model.
3. **Rollback Safeguard**: If an active model exhibits unexpected performance regression in production, `ModelRollbackService` allows 1-click reversion to the previous validated model version without data loss.
