"""Tests for ML Training Pipeline, Model Fitting, Evaluation, and Artifact Serialization."""
from pathlib import Path
from app.ml.pipeline import TrainingPipeline, ModelPackage


def test_train_and_evaluate_synthetic_demo_pipeline():
    """Verify that training pipeline fits models on synthetic demo data and calculates valid metrics."""
    df = TrainingPipeline.load_dataset(dataset_type="SYNTHETIC_DEMO")
    assert len(df) >= 100

    sufficiency = TrainingPipeline.check_sufficiency(df)
    assert sufficiency.is_sufficient

    package, candidate_metrics = TrainingPipeline.train_and_evaluate(
        df=df,
        dataset_type="SYNTHETIC_DEMO",
        model_name="test_recovery_predictor",
        version="v1.0.0-test",
    )

    assert isinstance(package, ModelPackage)
    assert package.model_type in ("LOGISTIC_REGRESSION", "HIST_GRADIENT_BOOSTING")
    assert package.feature_schema_version == "v1"

    metrics = package.metrics
    assert 0.0 <= metrics["roc_auc"] <= 1.0
    assert 0.0 <= metrics["brier_score"] <= 1.0
    assert metrics["total_amount_at_risk"] > 0
    assert metrics["predicted_recovery_value"] >= 0


def test_save_and_load_model_artifact(tmp_path):
    """Verify that model package can be saved to disk and reloaded without loss of functionality."""
    df = TrainingPipeline.load_dataset(dataset_type="SYNTHETIC_DEMO")
    package, _ = TrainingPipeline.train_and_evaluate(
        df=df,
        dataset_type="SYNTHETIC_DEMO",
        model_name="artifact_test_model",
        version="v1.0.0",
    )

    artifact_file = tmp_path / "test_model.joblib"
    saved_path = TrainingPipeline.save_artifact(package, filename=str(artifact_file))
    assert Path(saved_path).exists()

    reloaded = TrainingPipeline.load_artifact(saved_path)
    assert reloaded.model_id == package.model_id
    assert reloaded.model_type == package.model_type
    assert reloaded.version == "v1.0.0"

    # Test inference with reloaded pipeline
    sample_df = df.iloc[:5][package.feature_names]
    probs = reloaded.pipeline.predict_proba(sample_df)[:, 1]
    assert len(probs) == 5
    for p in probs:
        assert 0.0 <= p <= 1.0
