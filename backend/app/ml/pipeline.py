"""Machine Learning Training Pipeline & Model Serialization for Predictive Recovery."""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.learning import LearningExample
from app.ml.features import FeatureSchemaV1, validate_point_in_time_features
from app.ml.synthetic import generate_synthetic_demo_dataset

logger = logging.getLogger(__name__)

# Default directory for persistent model artifacts
ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DataSufficiencyResult:
    """Represents the dataset sufficiency gate evaluation."""

    is_sufficient: bool
    total_examples: int
    positive_examples: int
    negative_examples: int
    positive_ratio: float
    reason: Optional[str] = None


@dataclass
class ModelPackage:
    """Serialisable inference artifact bundle."""

    model_id: str
    model_name: str
    model_version: str
    model_type: str
    feature_schema_version: str
    dataset_type: str
    trained_at: str
    pipeline: Pipeline
    metrics: Dict[str, Any]
    feature_names: List[str]

    @property
    def version(self) -> str:
        return self.model_version


class TrainingPipeline:
    """End-to-end ML Training Pipeline for Action-Level Recovery Prediction."""

    MIN_TRAINING_EXAMPLES = 50
    MIN_POSITIVE_EXAMPLES = 10
    MIN_NEGATIVE_EXAMPLES = 10

    @classmethod
    def load_dataset(
        cls,
        db: Optional[Session] = None,
        dataset_type: str = "REAL",
    ) -> pd.DataFrame:
        """Load and tabularize learning examples for model training."""
        if dataset_type == "SYNTHETIC_DEMO":
            records = generate_synthetic_demo_dataset(n_samples=120, seed=42)
            rows = []
            for r in records:
                row = dict(r["features"])
                row["label"] = r["label"]
                row["created_at"] = r["created_at"]
                rows.append(row)
            return pd.DataFrame(rows)

        if not db:
            raise ValueError("Database session is required to load REAL dataset.")

        # Load REAL finalized learning examples from Step 7
        examples = db.scalars(
            select(LearningExample).order_by(LearningExample.created_at.asc())
        ).all()

        rows = []
        for ex in examples:
            snapshot = ex.feature_snapshot or {}
            # Anti-leakage validation
            try:
                validate_point_in_time_features(snapshot)
            except ValueError as err:
                logger.warning(f"Skipping corrupted/leaked example {ex.id}: {err}")
                continue

            row = {k: snapshot.get(k) for k in FeatureSchemaV1.ALL_FEATURES}
            row["label"] = int(ex.label)
            row["created_at"] = ex.created_at
            rows.append(row)

        return pd.DataFrame(rows)

    @classmethod
    def check_sufficiency(
        cls,
        df: pd.DataFrame,
        min_total: int = MIN_TRAINING_EXAMPLES,
        min_positive: int = MIN_POSITIVE_EXAMPLES,
        min_negative: int = MIN_NEGATIVE_EXAMPLES,
    ) -> DataSufficiencyResult:
        """Evaluate dataset sufficiency against minimum thresholds and class balance."""
        total = len(df)
        if total == 0:
            return DataSufficiencyResult(
                is_sufficient=False,
                total_examples=0,
                positive_examples=0,
                negative_examples=0,
                positive_ratio=0.0,
                reason="Dataset is completely empty (0 examples).",
            )

        positives = int((df["label"] == 1).sum()) if "label" in df.columns else 0
        negatives = total - positives
        pos_ratio = round(positives / total, 3)

        if total < min_total:
            return DataSufficiencyResult(
                is_sufficient=False,
                total_examples=total,
                positive_examples=positives,
                negative_examples=negatives,
                positive_ratio=pos_ratio,
                reason=f"Insufficient total examples: {total} < required {min_total}.",
            )

        if positives < min_positive:
            return DataSufficiencyResult(
                is_sufficient=False,
                total_examples=total,
                positive_examples=positives,
                negative_examples=negatives,
                positive_ratio=pos_ratio,
                reason=f"Insufficient positive recovery examples: {positives} < required {min_positive}.",
            )

        if negatives < min_negative:
            return DataSufficiencyResult(
                is_sufficient=False,
                total_examples=total,
                positive_examples=positives,
                negative_examples=negatives,
                positive_ratio=pos_ratio,
                reason=f"Insufficient negative unrecovered examples: {negatives} < required {min_negative}.",
            )

        return DataSufficiencyResult(
            is_sufficient=True,
            total_examples=total,
            positive_examples=positives,
            negative_examples=negatives,
            positive_ratio=pos_ratio,
        )

    @classmethod
    def build_preprocessor(cls) -> ColumnTransformer:
        """Construct scikit-learn ColumnTransformer for numerical and categorical features."""
        num_transformer = StandardScaler()
        cat_transformer = OneHotEncoder(handle_unknown="ignore", sparse_output=False)

        preprocessor = ColumnTransformer(
            transformers=[
                ("num", num_transformer, FeatureSchemaV1.NUMERICAL_FEATURES),
                ("cat", cat_transformer, FeatureSchemaV1.CATEGORICAL_FEATURES),
            ],
            remainder="drop",
        )
        return preprocessor

    @classmethod
    def split_dataset(
        cls, df: pd.DataFrame, test_size: float = 0.25
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Perform time-aware temporal train/test split to prevent temporal leakage."""
        df_sorted = df.sort_values(by="created_at").reset_index(drop=True)
        split_idx = int(len(df_sorted) * (1.0 - test_size))
        train_df = df_sorted.iloc[:split_idx].copy()
        test_df = df_sorted.iloc[split_idx:].copy()
        return train_df, test_df

    @classmethod
    def train_and_evaluate(
        cls,
        df: pd.DataFrame,
        dataset_type: str = "REAL",
        model_name: str = "recovery_value_predictor",
        version: str = "v1.0.0",
    ) -> Tuple[ModelPackage, Dict[str, Any]]:
        """Train baseline Logistic Regression & tree-based models, evaluate, and select champion."""
        train_df, val_df = cls.split_dataset(df)

        X_train = train_df[FeatureSchemaV1.ALL_FEATURES]
        y_train = train_df["label"].values
        X_val = val_df[FeatureSchemaV1.ALL_FEATURES]
        y_val = val_df["label"].values

        # 1. Candidate 1: Logistic Regression Pipeline
        lr_pipeline = Pipeline([
            ("preprocessor", cls.build_preprocessor()),
            ("classifier", LogisticRegression(max_iter=1000, random_state=42)),
        ])
        lr_pipeline.fit(X_train, y_train)
        lr_metrics = cls._evaluate_model(lr_pipeline, X_val, y_val, val_df)

        # 2. Candidate 2: HistGradientBoosting Pipeline
        hgb_pipeline = Pipeline([
            ("preprocessor", cls.build_preprocessor()),
            ("classifier", HistGradientBoostingClassifier(random_state=42)),
        ])
        hgb_pipeline.fit(X_train, y_train)
        hgb_metrics = cls._evaluate_model(hgb_pipeline, X_val, y_val, val_df)

        # 3. Model Selection: Pick higher ROC-AUC / Expected Value Lift
        if hgb_metrics.get("roc_auc", 0.0) > lr_metrics.get("roc_auc", 0.0):
            champion_pipeline = hgb_pipeline
            champion_type = "HIST_GRADIENT_BOOSTING"
            champion_metrics = hgb_metrics
        else:
            champion_pipeline = lr_pipeline
            champion_type = "LOGISTIC_REGRESSION"
            champion_metrics = lr_metrics

        model_id = str(uuid.uuid4())
        package = ModelPackage(
            model_id=model_id,
            model_name=model_name,
            model_version=version,
            model_type=champion_type,
            feature_schema_version=FeatureSchemaV1.SCHEMA_VERSION,
            dataset_type=dataset_type,
            trained_at=datetime.now(timezone.utc).isoformat(),
            pipeline=champion_pipeline,
            metrics=champion_metrics,
            feature_names=FeatureSchemaV1.ALL_FEATURES,
        )

        all_candidate_metrics = {
            "logistic_regression": lr_metrics,
            "hist_gradient_boosting": hgb_metrics,
            "selected_model": champion_type,
        }

        return package, all_candidate_metrics

    @classmethod
    def _evaluate_model(
        cls,
        pipeline: Pipeline,
        X_val: pd.DataFrame,
        y_val: np.ndarray,
        val_df: pd.DataFrame,
    ) -> Dict[str, Any]:
        """Compute statistical and business value metrics."""
        probs = pipeline.predict_proba(X_val)[:, 1]
        preds = (probs >= 0.50).astype(int)

        # Statistical Metrics
        try:
            auc = float(roc_auc_score(y_val, probs))
        except Exception:
            auc = 0.50

        try:
            pr_auc = float(average_precision_score(y_val, probs))
        except Exception:
            pr_auc = float(np.mean(y_val)) if len(y_val) > 0 else 0.50

        brier = float(brier_score_loss(y_val, probs))
        prec = float(precision_score(y_val, preds, zero_division=0))
        rec = float(recall_score(y_val, preds, zero_division=0))
        f1 = float(f1_score(y_val, preds, zero_division=0))

        # Business Value Metrics
        amounts = val_df["amount_at_risk"].values
        total_at_risk = float(np.sum(amounts))
        actual_recovered = float(np.sum(amounts * y_val))
        predicted_expected_recovery = float(np.sum(amounts * probs))

        heuristic_probs = val_df["heuristic_recovery_probability"].values
        heuristic_expected_recovery = float(np.sum(amounts * heuristic_probs))

        lift_percentage = 0.0
        if heuristic_expected_recovery > 0:
            lift_percentage = round(
                ((predicted_expected_recovery - heuristic_expected_recovery) / heuristic_expected_recovery) * 100.0,
                2
            )

        return {
            "roc_auc": round(auc, 4),
            "pr_auc": round(pr_auc, 4),
            "brier_score": round(brier, 4),
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
            "total_amount_at_risk": round(total_at_risk, 2),
            "actual_recovered_amount": round(actual_recovered, 2),
            "predicted_recovery_value": round(predicted_expected_recovery, 2),
            "heuristic_recovery_value": round(heuristic_expected_recovery, 2),
            "expected_value_lift_pct": lift_percentage,
            "sample_size": len(y_val),
        }

    @classmethod
    def save_artifact(cls, package: ModelPackage, filename: Optional[str] = None) -> str:
        """Serialize model package to persistent storage."""
        if not filename:
            filename = f"model_{package.model_id}.joblib"
        target_path = ARTIFACTS_DIR / filename
        joblib.dump(package, target_path)
        logger.info(f"[ML_ARTIFACT_SAVED] Saved model package to {target_path}")
        return str(target_path)

    @classmethod
    def load_artifact(cls, artifact_path: str) -> ModelPackage:
        """Load serialized model package from file path."""
        path = Path(artifact_path)
        if not path.exists():
            raise FileNotFoundError(f"Model artifact file not found: {artifact_path}")
        package: ModelPackage = joblib.load(path)
        return package
