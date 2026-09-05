"""Unit and Integration tests for Baseline Recovery Probability Model, Pipeline Preprocessing, and Inference."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import uuid
import pytest
import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.ml.features.recovery_features import (
    RecoveryFeatureSchema,
    RecoveryFeatureVector,
    validate_pre_intervention_features,
)
from app.ml.dataset_builder import RecoveryMLDatasetBuilder
from app.ml.recovery_probability_model import (
    RecoveryProbabilityModelService,
    ModelEvaluationReport,
    DEFAULT_MODEL_VERSION,
)


def _generate_synthetic_temporal_dataset(sample_count: int = 100) -> pd.DataFrame:
    """Deterministic synthetic temporal dataset for unit tests."""
    rng = np.random.RandomState(42)
    base_time = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

    rows = []
    for i in range(sample_count):
        t = base_time + timedelta(hours=i * 2)
        amount = float(rng.uniform(1000.0, 20000.0))
        cat = rng.choice(["INSUFFICIENT_FUNDS", "AUTHENTICATION_FAILURE", "TECHNICAL_ERROR", "NETWORK_ERROR"])
        itype = rng.choice(["EMAIL", "VOICE", "PAYMENT_RETRY", "WHATSAPP"])
        ptype = rng.choice(["card", "upi", "netbanking"])
        prev_success = int(rng.randint(0, 10))
        prev_failed = int(rng.randint(0, 4))
        days_since = float(rng.uniform(0.1, 10.0))

        # Ground truth recovery formula (higher success with card & retry/email for tech error)
        score = 0.30 + (0.05 * prev_success) - (0.05 * prev_failed) + (0.15 if itype in ["EMAIL", "VOICE"] else 0.0)
        prob = 1.0 / (1.0 + np.exp(-score))
        recovered = 1 if rng.uniform(0.0, 1.0) < prob else 0

        row = {
            "case_id": f"case_{i}",
            "intervention_id": f"step_{i}",
            "intervention_type": itype,
            "prediction_timestamp": t.isoformat(),
            "amount_at_risk": amount,
            "currency": "INR",
            "days_overdue": days_since,
            "failure_code": "ERR_TEST",
            "failure_category": cat,
            "payment_type": ptype,
            "is_subscription_or_invoice": 1,
            "customer_age_days": 100.0,
            "previous_successful_payments": prev_success,
            "previous_failed_payments": prev_failed,
            "previous_recoveries": 0,
            "previous_promises_to_pay": 0,
            "previous_ptp_fulfillment_rate": 0.0,
            "previous_voice_attempts": 0,
            "previous_email_attempts": 1 if itype == "EMAIL" else 0,
            "previous_whatsapp_attempts": 0,
            "hour_of_day": t.hour,
            "day_of_week": t.weekday(),
            "days_since_failure": days_since,
            "previous_intervention_outcome": "NONE",
            "number_of_previous_recovery_attempts": 0,
            "previous_recovery_time_seconds": 0.0,
            "recovered": recovered,
            "amount_recovered": amount if recovered else 0.0,
            "time_to_recovery_seconds": 3600.0 if recovered else None,
        }
        rows.append(row)

    return pd.DataFrame(rows)


# 1. Feature Preprocessing Test
def test_feature_preprocessing_pipeline():
    df = _generate_synthetic_temporal_dataset(sample_count=20)
    numerical_cols, categorical_cols = RecoveryProbabilityModelService.get_feature_columns()
    pipeline = RecoveryProbabilityModelService.build_sklearn_pipeline(numerical_cols, categorical_cols)

    feature_cols = numerical_cols + categorical_cols
    X = df[feature_cols]
    y = df["recovered"].values

    pipeline.fit(X, y)
    probs = pipeline.predict_proba(X)
    assert probs.shape == (20, 2)


# 2. Model Training & Temporal Split Test
def test_model_training_temporal_split():
    with tempfile.TemporaryDirectory() as tmp_dir:
        df = _generate_synthetic_temporal_dataset(sample_count=80)
        report = RecoveryProbabilityModelService.train_and_evaluate_temporal(
            df=df,
            model_version="test_temp_model_v1",
            output_dir=tmp_dir,
            test_ratio=0.25,
        )

        assert report.is_sufficient_data is True
        assert report.train_samples == 60
        assert report.test_samples == 20
        assert report.metrics.brier_score >= 0.0
        assert 0.0 <= report.metrics.roc_auc <= 1.0
        assert report.train_date_range["min"] < report.test_date_range["min"]


# 3. Probability Output & Bounds Test
def test_probability_output_bounds():
    with tempfile.TemporaryDirectory() as tmp_dir:
        df = _generate_synthetic_temporal_dataset(sample_count=60)
        version = f"bounds_test_{uuid.uuid4().hex[:6]}"
        report = RecoveryProbabilityModelService.train_and_evaluate_temporal(
            df=df,
            model_version=version,
            output_dir=tmp_dir,
        )

        # Load and predict
        model_path = Path(tmp_dir) / f"{version}.joblib"
        RecoveryProbabilityModelService.load_model(str(model_path))

        sample_features = {
            "amount_at_risk": 7500.0,
            "currency": "INR",
            "days_overdue": 2.0,
            "failure_category": "AUTHENTICATION_FAILURE",
            "payment_type": "card",
            "previous_successful_payments": 5,
            "hour_of_day": 14,
            "day_of_week": 2,
        }
        res = RecoveryProbabilityModelService.predict_probability(
            features=sample_features,
            intervention_type="EMAIL",
            model_version=version,
        )

        assert res["is_model_prediction"] is True
        assert 0.0 <= res["probability"] <= 1.0
        assert res["expected_recovery_value"] == round(res["probability"] * 7500.0, 2)


# 4. Missing Feature Imputation Test
def test_missing_feature_handling():
    with tempfile.TemporaryDirectory() as tmp_dir:
        df = _generate_synthetic_temporal_dataset(sample_count=60)
        version = f"missing_test_{uuid.uuid4().hex[:6]}"
        RecoveryProbabilityModelService.train_and_evaluate_temporal(
            df=df,
            model_version=version,
            output_dir=tmp_dir,
        )
        model_path = Path(tmp_dir) / f"{version}.joblib"
        RecoveryProbabilityModelService.load_model(str(model_path))

        # Pass sparse dictionary with missing numerical and categorical features
        sparse_features = {
            "amount_at_risk": 3000.0,
        }
        res = RecoveryProbabilityModelService.predict_probability(
            features=sparse_features,
            intervention_type="VOICE",
            model_version=version,
        )
        assert res["is_model_prediction"] is True
        assert 0.0 <= res["probability"] <= 1.0


# 5. Categorical Handling Test (Unseen Categories)
def test_unseen_categorical_handling():
    with tempfile.TemporaryDirectory() as tmp_dir:
        df = _generate_synthetic_temporal_dataset(sample_count=60)
        version = f"cat_test_{uuid.uuid4().hex[:6]}"
        RecoveryProbabilityModelService.train_and_evaluate_temporal(
            df=df,
            model_version=version,
            output_dir=tmp_dir,
        )
        model_path = Path(tmp_dir) / f"{version}.joblib"
        RecoveryProbabilityModelService.load_model(str(model_path))

        # Pass completely unseen category values
        weird_features = {
            "amount_at_risk": 5000.0,
            "currency": "BITCOIN_UNKNOWN",
            "failure_category": "QUANTUM_TELEPORT_ERROR",
            "payment_type": "CRYPTO_TOKEN",
        }
        res = RecoveryProbabilityModelService.predict_probability(
            features=weird_features,
            intervention_type="MAGIC_CHANNEL",
            model_version=version,
        )
        assert res["is_model_prediction"] is True
        assert 0.0 <= res["probability"] <= 1.0


# 6. Model Serialization and Reload Consistency Test
def test_model_serialization_consistency():
    with tempfile.TemporaryDirectory() as tmp_dir:
        df = _generate_synthetic_temporal_dataset(sample_count=60)
        version = f"reload_test_{uuid.uuid4().hex[:6]}"
        report = RecoveryProbabilityModelService.train_and_evaluate_temporal(
            df=df,
            model_version=version,
            output_dir=tmp_dir,
        )

        model_path = Path(tmp_dir) / f"{version}.joblib"
        pipe1 = RecoveryProbabilityModelService.load_model(str(model_path))

        test_feat = {
            "amount_at_risk": 12000.0,
            "currency": "INR",
            "failure_category": "INSUFFICIENT_FUNDS",
            "payment_type": "upi",
            "previous_successful_payments": 2,
        }
        pred1 = RecoveryProbabilityModelService.predict_probability(test_feat, "PAYMENT_RETRY", version)

        # Clear cache and reload
        RecoveryProbabilityModelService._cached_model_pipeline = None
        pipe2 = RecoveryProbabilityModelService.load_model(str(model_path))
        pred2 = RecoveryProbabilityModelService.predict_probability(test_feat, "PAYMENT_RETRY", version)

        assert pred1["probability"] == pred2["probability"]
        assert pred1["expected_recovery_value"] == pred2["expected_recovery_value"]


# 7. Cold-Start / Insufficient-Data Handling Test
def test_cold_start_insufficient_data():
    small_df = _generate_synthetic_temporal_dataset(sample_count=10)
    report = RecoveryProbabilityModelService.train_and_evaluate_temporal(
        df=small_df,
        model_version="cold_start_test",
        min_samples=50,
    )

    assert report.is_sufficient_data is False
    assert "Insufficient historical data" in str(report.insufficient_reason)


# 8. Baseline Comparison Test
def test_baseline_comparison():
    with tempfile.TemporaryDirectory() as tmp_dir:
        df = _generate_synthetic_temporal_dataset(sample_count=80)
        report = RecoveryProbabilityModelService.train_and_evaluate_temporal(
            df=df,
            model_version="baseline_comp_test",
            output_dir=tmp_dir,
        )

        assert report.metrics.baseline_brier_score >= 0.0
        assert report.metrics.baseline_recovery_rate >= 0.0
        assert isinstance(report.metrics.brier_skill_score, float)


# 9. Feature Explainability (Logistic Regression Coefficients) Test
def test_feature_explainability_extraction():
    with tempfile.TemporaryDirectory() as tmp_dir:
        df = _generate_synthetic_temporal_dataset(sample_count=80)
        version = f"explain_test_{uuid.uuid4().hex[:6]}"
        report = RecoveryProbabilityModelService.train_and_evaluate_temporal(
            df=df,
            model_version=version,
            output_dir=tmp_dir,
        )

        assert len(report.feature_coefficients) > 0
        first_feat = report.feature_coefficients[0]
        assert "feature" in first_feat
        assert "coefficient" in first_feat
        assert first_feat["direction"] in ["POSITIVE_RECOVERY", "NEGATIVE_RECOVERY"]
        assert "odds_ratio" in first_feat


# 10. Database Cold-Start Checker Test
def test_check_and_train_from_db_insufficient():
    class MockStats:
        total_rows = 5
        recovered_rows = 0

    class MockBuildResult:
        statistics = MockStats()
        dataframe = pd.DataFrame()

    from unittest.mock import patch

    with patch("app.ml.recovery_probability_model.RecoveryMLDatasetBuilder.build_training_dataset", return_value=MockBuildResult()):
        res = RecoveryProbabilityModelService.check_and_train_from_db(db=None, min_samples=50)
        assert res["status"] == "INSUFFICIENT_DATA"
        assert res["minimum_samples"] == 50
        assert res["available_samples"] == 5
        assert "insufficient historical interventions" in res["message"]


# 11. No Future-Data Leakage Test
def test_no_future_leakage_enforcement():
    leaky_input = {
        "amount_at_risk": 5000.0,
        "amount_recovered": 5000.0,  # LEAKAGE
    }
    with pytest.raises(ValueError, match="anti-leakage violation"):
        RecoveryProbabilityModelService.predict_probability(leaky_input)
