"""Tests for Data Sufficiency Gate in Training Pipeline."""
import pandas as pd
from app.ml.pipeline import TrainingPipeline, DataSufficiencyResult


def test_sufficiency_gate_rejects_empty_dataset():
    """Verify that an empty DataFrame is immediately marked insufficient."""
    df = pd.DataFrame()
    res = TrainingPipeline.check_sufficiency(df)
    assert not res.is_sufficient
    assert res.total_examples == 0
    assert "empty" in res.reason.lower()


def test_sufficiency_gate_rejects_undersized_dataset():
    """Verify that fewer than MIN_TRAINING_EXAMPLES is rejected."""
    df = pd.DataFrame({"label": [1] * 20 + [0] * 10})
    res = TrainingPipeline.check_sufficiency(df, min_total=50)
    assert not res.is_sufficient
    assert res.total_examples == 30
    assert "insufficient total examples" in res.reason.lower()


def test_sufficiency_gate_rejects_imbalanced_missing_positives():
    """Verify that having fewer than MIN_POSITIVE_EXAMPLES is rejected."""
    df = pd.DataFrame({"label": [1] * 3 + [0] * 60})
    res = TrainingPipeline.check_sufficiency(df, min_total=50, min_positive=10)
    assert not res.is_sufficient
    assert "insufficient positive recovery examples" in res.reason.lower()


def test_sufficiency_gate_accepts_sufficient_dataset():
    """Verify that a balanced dataset meeting all thresholds passes."""
    df = pd.DataFrame({"label": [1] * 35 + [0] * 25})
    res = TrainingPipeline.check_sufficiency(df, min_total=50, min_positive=10, min_negative=10)
    assert res.is_sufficient
    assert res.total_examples == 60
    assert res.positive_examples == 35
    assert res.negative_examples == 25
    assert res.reason is None
