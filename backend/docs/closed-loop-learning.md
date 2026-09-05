# Closed-Loop Learning Workflow

```mermaid
graph TD
    A[Payment Failure: Rs. 10,000 at Risk] --> B[Feature Snapshot Extracted at T0]
    B --> C[Prediction Engine: P_rec = 42% for Email, 65% for Followup]
    C --> D[Policy Check & Next-Best-Action Selection]
    D --> E[Execute Action: Dispatch Recovery Email]
    E --> F[Observe Customer Behavior & Telemetry]
    F --> G[Razorpay Webhook: payment.captured at T + 4h]
    G --> H[RecoveryOutcomeResolver: Resolve Status RECOVERED]
    H --> I[Attribution Engine: Credit Email within 24h Window with Weight 1.0]
    I --> J[Record LearningExample: Target=1, Error=1 - 0.42 = +0.58]
    J --> K[Dataset Builder: Filter training_eligible == True]
    K --> L[Batch Retraining: Fit Calibrated Logistic/Forest Model]
    L --> M[Model Evaluation: Measure ROC-AUC, Log Loss, Brier Score]
    M --> N{Promotion Gate Passed?}
    N -->|YES| O[Activate Model Version: Update Production Registry]
    N -->|NO| P[Retain Active Model: Log Rejection Audit Event]
    O --> Q[Next Prediction Leverages Improved Calibrated Weights]
```

---

## The Closed Feedback Guarantee

1. **Prediction Traceability**: Every decision stores `prediction_id`, `model_version`, and feature values.
2. **Outcome Attribution**: Real webhook events link payments back to the triggering intervention.
3. **Continuous Auditing**: Prediction error ($y - \hat{p}$) and calibration curves are continuously monitored for distribution drift.
