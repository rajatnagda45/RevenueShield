# Machine Learning Training, Evaluation & Activation Handbook

This document provides step-by-step procedures for dataset inspection, model training, evaluation, activation, and rollback.

---

## 1. Inspecting Dataset Health

### Check Feature Drift & Statistics
```http
GET /admin/ml/feature-drift
```
**Response**:
```json
{
  "status": "COMPUTED",
  "total_examples": 14,
  "feature_statistics": {
    "amount_at_risk": {
      "type": "numerical",
      "mean": 689.29,
      "median": 650.0,
      "min": 650.0,
      "max": 850.0,
      "missing_rate": 0.0
    }
  }
}
```

---

## 2. Triggering Model Training

### Production Real Data Training
```http
POST /admin/ml/train
Content-Type: application/json

{
  "dataset_type": "REAL",
  "model_name": "recovery_value_predictor",
  "version": "v1.0.0"
}
```

- If real examples $< 50$, the endpoint responds with `INSUFFICIENT_DATA`:
```json
{
  "status": "INSUFFICIENT_DATA",
  "model_id": null,
  "dataset_type": "REAL",
  "sufficiency": {
    "is_sufficient": false,
    "total_examples": 14,
    "positive_examples": 10,
    "negative_examples": 4,
    "reason": "Insufficient total examples: 14 < required 50."
  },
  "message": "Training halted: Insufficient total examples. System will continue using HEURISTIC strategy."
}
```

### Sandbox Synthetic Demo Training
```http
POST /admin/ml/train
Content-Type: application/json

{
  "dataset_type": "SYNTHETIC_DEMO",
  "model_name": "recovery_value_predictor",
  "version": "v1.0.0-demo"
}
```
**Response**:
```json
{
  "status": "DEVELOPMENT_ONLY",
  "model_id": "84d567fe-9e0c-4ab4-b771-477080e722a4",
  "version": "v1.0.0-demo",
  "model_type": "LOGISTIC_REGRESSION",
  "dataset_type": "SYNTHETIC_DEMO",
  "metrics": {
    "roc_auc": 0.8842,
    "pr_auc": 0.9125,
    "brier_score": 0.1184,
    "precision": 0.8571,
    "recall": 0.8824,
    "f1": 0.8696,
    "expected_value_lift_pct": 14.8
  },
  "message": "Model v1.0.0-demo successfully trained and registered as DEVELOPMENT_ONLY."
}
```

---

## 3. Activating a Model

To promote a model to active serving:
```http
POST /admin/ml/models/84d567fe-9e0c-4ab4-b771-477080e722a4/activate
```
**Response**:
```json
{
  "id": "84d567fe-9e0c-4ab4-b771-477080e722a4",
  "model_name": "recovery_value_predictor",
  "version": "v1.0.0-demo",
  "status": "ACTIVE",
  "deployed_at": "2026-08-22T04:10:00Z"
}
```

---

## 4. Rollback Procedure

If an active model degrades in production or exhibits distribution shift:
1. Identify the previous stable model ID via `GET /admin/ml/models`.
2. Activate the previous model via `POST /admin/ml/models/{previous_model_id}/activate`.
3. If no other model is suitable, deleting or deactivating active models will cause the system to seamlessly revert to the zero-risk `HEURISTIC` fallback strategy without interrupting recovery workflows.
