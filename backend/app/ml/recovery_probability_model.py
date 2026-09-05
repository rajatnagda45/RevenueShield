"""Baseline Logistic Regression Recovery Probability Model with Temporal Split, Anti-Leakage, & Explainability."""
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
    average_precision_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sqlalchemy.orm import Session

from app.ml.features.recovery_features import (
    RecoveryFeatureSchema,
    validate_pre_intervention_features,
)
from app.ml.dataset_builder import (
    RecoveryMLDatasetBuilder,
    DEFAULT_ATTRIBUTION_WINDOW_HOURS,
    MIN_TRAINING_SAMPLES_THRESHOLD,
)

logger = logging.getLogger(__name__)

MODELS_DIR = Path("artifacts/models")
DEFAULT_MODEL_VERSION = "recovery_probability_v1"


@dataclass
class ModelMetrics:
    """Detailed validation metrics comparing model against naive baseline."""

    roc_auc: float
    pr_auc: float
    accuracy: float
    precision: float
    recall: float
    f1: float
    log_loss: float
    brier_score: float
    baseline_brier_score: float
    baseline_log_loss: float
    baseline_recovery_rate: float
    brier_skill_score: float  # 1 - (brier / baseline_brier)
    calibration_error: float
    intervention_breakdown: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FeatureExplanation:
    """Interpretable feature coefficient metadata for Logistic Regression."""

    feature: str
    coefficient: float
    direction: str  # POSITIVE_RECOVERY or NEGATIVE_RECOVERY
    odds_ratio: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelEvaluationReport:
    """Complete evaluation report for the trained model."""

    model_name: str
    model_version: str
    training_timestamp: str
    dataset_hash: str
    total_samples: int
    train_samples: int
    test_samples: int
    train_date_range: Dict[str, Optional[str]]
    test_date_range: Dict[str, Optional[str]]
    train_recovery_rate: float
    test_recovery_rate: float
    attribution_window_hours: int
    target_definition: str
    intervention_types: List[str]
    metrics: ModelMetrics
    feature_schema_version: str
    feature_coefficients: List[Dict[str, Any]]
    model_artifact_path: str
    metadata_artifact_path: str
    is_sufficient_data: bool = True
    insufficient_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RecoveryProbabilityModelService:
    """Manages training, temporal evaluation, explainability, serialization, and point-in-time inference."""

    _cached_model_pipeline: Optional[Pipeline] = None
    _cached_model_version: Optional[str] = None

    @classmethod
    def get_feature_columns(cls) -> Tuple[List[str], List[str]]:
        """Return numerical and categorical column names including intervention_type."""
        numerical_cols = list(RecoveryFeatureSchema.NUMERICAL_FEATURES)
        categorical_cols = list(RecoveryFeatureSchema.CATEGORICAL_FEATURES)
        if "intervention_type" not in categorical_cols:
            categorical_cols.append("intervention_type")
        return numerical_cols, categorical_cols

    @classmethod
    def build_sklearn_pipeline(
        cls,
        numerical_cols: List[str],
        categorical_cols: List[str],
        class_weight: str = "balanced",
        c_param: float = 1.0,
        random_state: int = 42,
    ) -> Pipeline:
        """Create a reproducible scikit-learn Pipeline with preprocessor and Logistic Regression."""
        num_transformer = Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
            ]
        )

        cat_transformer = Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="constant", fill_value="UNKNOWN")),
                ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
            ]
        )

        preprocessor = ColumnTransformer(
            transformers=[
                ("num", num_transformer, numerical_cols),
                ("cat", cat_transformer, categorical_cols),
            ],
            remainder="drop",
        )

        classifier = LogisticRegression(
            C=c_param,
            class_weight=class_weight,
            max_iter=1000,
            random_state=random_state,
            solver="lbfgs",
        )

        return Pipeline(
            steps=[
                ("preprocessor", preprocessor),
                ("classifier", classifier),
            ]
        )

    @classmethod
    def extract_feature_explanations(
        cls,
        pipeline: Pipeline,
        numerical_cols: List[str],
        categorical_cols: List[str],
    ) -> List[Dict[str, Any]]:
        """Extract interpretable feature coefficients from fitted Logistic Regression pipeline."""
        try:
            preprocessor = pipeline.named_steps["preprocessor"]
            classifier = pipeline.named_steps["classifier"]

            cat_encoder = preprocessor.named_transformers_["cat"].named_steps["onehot"]
            cat_feature_names = list(cat_encoder.get_feature_names_out(categorical_cols))
            all_feature_names = numerical_cols + cat_feature_names

            coefs = classifier.coef_[0]
            explanations = []
            for name, coef in zip(all_feature_names, coefs):
                coef_val = float(round(coef, 4))
                direction = "POSITIVE_RECOVERY" if coef_val >= 0 else "NEGATIVE_RECOVERY"
                odds_ratio = float(round(np.exp(coef_val), 4))
                explanations.append(
                    {
                        "feature": str(name),
                        "coefficient": coef_val,
                        "direction": direction,
                        "odds_ratio": odds_ratio,
                    }
                )

            # Sort by absolute magnitude
            explanations.sort(key=lambda x: abs(x["coefficient"]), reverse=True)
            return explanations
        except Exception as e:
            logger.warning(f"[FEATURE_EXPLANATION_ERROR] Could not extract coefficients: {e}")
            return []

    @classmethod
    def train_and_evaluate_temporal(
        cls,
        df: pd.DataFrame,
        model_version: str = DEFAULT_MODEL_VERSION,
        output_dir: Union[str, Path] = MODELS_DIR,
        test_ratio: float = 0.20,
        min_samples: int = MIN_TRAINING_SAMPLES_THRESHOLD,
    ) -> ModelEvaluationReport:
        """Train Logistic Regression on chronologically older rows and evaluate on newer test rows."""
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        model_path = out_dir / f"{model_version}.joblib"
        metadata_path = out_dir / f"{model_version}_metadata.json"

        # 1. Cold-Start Check
        if df.empty or len(df) < min_samples:
            reason = f"Insufficient historical data: {len(df)} samples available (minimum required: {min_samples})."
            logger.warning(f"[COLD_START_TRIGGERED] {reason}")
            return ModelEvaluationReport(
                model_name="RecoveryProbabilityLogisticRegression",
                model_version=model_version,
                training_timestamp=datetime.now(timezone.utc).isoformat(),
                dataset_hash="NONE",
                total_samples=len(df),
                train_samples=0,
                test_samples=0,
                train_date_range={"min": None, "max": None},
                test_date_range={"min": None, "max": None},
                train_recovery_rate=0.0,
                test_recovery_rate=0.0,
                attribution_window_hours=DEFAULT_ATTRIBUTION_WINDOW_HOURS,
                target_definition="recovered within 72 hours (1 or 0)",
                intervention_types=[],
                metrics=ModelMetrics(
                    roc_auc=0.5,
                    pr_auc=0.0,
                    accuracy=0.0,
                    precision=0.0,
                    recall=0.0,
                    f1=0.0,
                    log_loss=0.0,
                    brier_score=0.0,
                    baseline_brier_score=0.0,
                    baseline_log_loss=0.0,
                    baseline_recovery_rate=0.0,
                    brier_skill_score=0.0,
                    calibration_error=0.0,
                ),
                feature_schema_version=RecoveryFeatureSchema.SCHEMA_VERSION,
                feature_coefficients=[],
                model_artifact_path=str(model_path),
                metadata_artifact_path=str(metadata_path),
                is_sufficient_data=False,
                insufficient_reason=reason,
            )

        # 2. Sort by prediction_timestamp for temporal split
        df_sorted = df.copy()
        if "prediction_timestamp" in df_sorted.columns:
            df_sorted["dt_temp"] = pd.to_datetime(df_sorted["prediction_timestamp"], errors="coerce")
            df_sorted = df_sorted.sort_values("dt_temp").drop(columns=["dt_temp"])
        elif "created_at" in df_sorted.columns:
            df_sorted["dt_temp"] = pd.to_datetime(df_sorted["created_at"], errors="coerce")
            df_sorted = df_sorted.sort_values("dt_temp").drop(columns=["dt_temp"])

        split_idx = int(len(df_sorted) * (1.0 - test_ratio))
        split_idx = max(10, min(split_idx, len(df_sorted) - 5))

        train_df = df_sorted.iloc[:split_idx].copy()
        test_df = df_sorted.iloc[split_idx:].copy()

        # Compute dataset hash
        ds_bytes = df_sorted.to_csv(index=False).encode("utf-8")
        dataset_hash = hashlib.sha256(ds_bytes).hexdigest()[:16]

        # Extract features and targets
        numerical_cols, categorical_cols = cls.get_feature_columns()
        feature_cols = numerical_cols + categorical_cols

        for col in feature_cols:
            if col not in train_df.columns:
                train_df[col] = 0.0 if col in numerical_cols else "UNKNOWN"
            if col not in test_df.columns:
                test_df[col] = 0.0 if col in numerical_cols else "UNKNOWN"

        target_col = "recovered" if "recovered" in train_df.columns else "label"
        y_train = train_df[target_col].astype(int).values
        y_test = test_df[target_col].astype(int).values

        X_train = train_df[feature_cols]
        X_test = test_df[feature_cols]

        # 3. Fit unified sklearn Pipeline
        pipeline = cls.build_sklearn_pipeline(numerical_cols, categorical_cols)
        pipeline.fit(X_train, y_train)

        # 4. Predict probabilities on test set
        y_pred_proba = pipeline.predict_proba(X_test)[:, 1]
        y_pred = (y_pred_proba >= 0.5).astype(int)

        # Baseline predictions: constant probability = train_recovery_rate
        p_baseline = float(np.mean(y_train))
        y_baseline_proba = np.full_like(y_pred_proba, p_baseline)

        # 5. Compute Comprehensive Metrics
        try:
            roc_auc = float(roc_auc_score(y_test, y_pred_proba)) if len(np.unique(y_test)) > 1 else 0.5
        except Exception:
            roc_auc = 0.5

        try:
            pr_auc = float(average_precision_score(y_test, y_pred_proba))
        except Exception:
            pr_auc = p_baseline

        acc = float(accuracy_score(y_test, y_pred))
        prec = float(precision_score(y_test, y_pred, zero_division=0))
        rec = float(recall_score(y_test, y_pred, zero_division=0))
        f1 = float(f1_score(y_test, y_pred, zero_division=0))

        # Log Loss & Brier Score
        eps = 1e-15
        p_clipped = np.clip(y_pred_proba, eps, 1 - eps)
        p_base_clipped = np.clip(y_baseline_proba, eps, 1 - eps)

        try:
            ll = float(log_loss(y_test, p_clipped))
            ll_base = float(log_loss(y_test, p_base_clipped))
        except Exception:
            ll, ll_base = 0.0, 0.0

        brier = float(brier_score_loss(y_test, y_pred_proba))
        brier_base = float(brier_score_loss(y_test, y_baseline_proba))
        brier_skill = 1.0 - (brier / brier_base) if brier_base > 0 else 0.0

        calib_error = float(np.abs(np.mean(y_pred_proba) - np.mean(y_test)))

        # 6. Breakdown by Intervention Type
        breakdown = {}
        itypes = [str(x) for x in df_sorted["intervention_type"].dropna().unique()] if "intervention_type" in df_sorted else []
        if "intervention_type" in test_df.columns:
            for itype in test_df["intervention_type"].unique():
                mask = (test_df["intervention_type"] == itype).values
                if np.sum(mask) > 0:
                    sub_y_test = y_test[mask]
                    sub_y_pred = y_pred_proba[mask]
                    breakdown[str(itype)] = {
                        "samples": int(np.sum(mask)),
                        "recovery_rate": float(np.mean(sub_y_test)),
                        "mean_predicted_prob": float(np.mean(sub_y_pred)),
                        "brier_score": float(brier_score_loss(sub_y_test, sub_y_pred)) if len(sub_y_test) > 0 else 0.0,
                    }

        # 7. Extract Feature Coefficients
        explanations = cls.extract_feature_explanations(pipeline, numerical_cols, categorical_cols)

        metrics = ModelMetrics(
            roc_auc=roc_auc,
            pr_auc=pr_auc,
            accuracy=acc,
            precision=prec,
            recall=rec,
            f1=f1,
            log_loss=ll,
            brier_score=brier,
            baseline_brier_score=brier_base,
            baseline_log_loss=ll_base,
            baseline_recovery_rate=p_baseline,
            brier_skill_score=brier_skill,
            calibration_error=calib_error,
            intervention_breakdown=breakdown,
        )

        t_min_train = str(train_df["prediction_timestamp"].min()) if "prediction_timestamp" in train_df else None
        t_max_train = str(train_df["prediction_timestamp"].max()) if "prediction_timestamp" in train_df else None
        t_min_test = str(test_df["prediction_timestamp"].min()) if "prediction_timestamp" in test_df else None
        t_max_test = str(test_df["prediction_timestamp"].max()) if "prediction_timestamp" in test_df else None

        report = ModelEvaluationReport(
            model_name="RecoveryProbabilityLogisticRegression",
            model_version=model_version,
            training_timestamp=datetime.now(timezone.utc).isoformat(),
            dataset_hash=dataset_hash,
            total_samples=len(df_sorted),
            train_samples=len(train_df),
            test_samples=len(test_df),
            train_date_range={"min": t_min_train, "max": t_max_train},
            test_date_range={"min": t_min_test, "max": t_max_test},
            train_recovery_rate=p_baseline,
            test_recovery_rate=float(np.mean(y_test)),
            attribution_window_hours=DEFAULT_ATTRIBUTION_WINDOW_HOURS,
            target_definition="recovered within 72 hours (1 or 0)",
            intervention_types=itypes,
            metrics=metrics,
            feature_schema_version=RecoveryFeatureSchema.SCHEMA_VERSION,
            feature_coefficients=explanations,
            model_artifact_path=str(model_path),
            metadata_artifact_path=str(metadata_path),
            is_sufficient_data=True,
        )

        # 8. Serialize Artifacts (Model + Metadata)
        joblib.dump(pipeline, model_path)
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2)

        logger.info(
            f"[MODEL_TRAINED_AND_SAVED] Saved {model_version} to {model_path} (Brier={brier:.4f}, AUC={roc_auc:.4f})"
        )
        return report

    @classmethod
    def check_and_train_from_db(
        cls,
        db: Session,
        model_version: str = DEFAULT_MODEL_VERSION,
        min_samples: int = MIN_TRAINING_SAMPLES_THRESHOLD,
    ) -> Dict[str, Any]:
        """Check real database historical records; if insufficient (< 50), report cold start without training."""
        build_result = RecoveryMLDatasetBuilder.build_training_dataset(db=db)
        count = build_result.statistics.total_rows

        if count < min_samples:
            return {
                "status": "INSUFFICIENT_DATA",
                "minimum_samples": min_samples,
                "available_samples": count,
                "message": f"Model not trained because the real dataset contains insufficient historical interventions ({count} available, minimum required is {min_samples}).",
            }

        report = cls.train_and_evaluate_temporal(
            df=build_result.dataframe,
            model_version=model_version,
            min_samples=min_samples,
        )
        return {
            "status": "TRAINED",
            "report": report.to_dict(),
        }

    @classmethod
    def load_model(cls, model_version_or_path: Optional[str] = None) -> Optional[Pipeline]:
        """Load trained scikit-learn Pipeline from disk."""
        target_path: Optional[Path] = None

        if model_version_or_path:
            p = Path(model_version_or_path)
            if p.exists():
                target_path = p
            else:
                target_path = MODELS_DIR / f"{model_version_or_path}.joblib"
        else:
            target_path = MODELS_DIR / f"{DEFAULT_MODEL_VERSION}.joblib"

        if not target_path or not target_path.exists():
            logger.warning(f"[MODEL_NOT_FOUND] Model file does not exist at {target_path}")
            return None

        try:
            pipeline = joblib.load(target_path)
            cls._cached_model_pipeline = pipeline
            cls._cached_model_version = target_path.stem
            return pipeline
        except Exception as e:
            logger.error(f"[MODEL_LOAD_ERROR] Failed to load {target_path}: {e}")
            return None

    @classmethod
    def predict_probability(
        cls,
        features: Dict[str, Any],
        intervention_type: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute point-in-time recovery probability for a feature dictionary and candidate intervention."""
        # 1. Anti-leakage validation
        feat_clean = dict(features)
        validate_pre_intervention_features(feat_clean)

        if intervention_type:
            feat_clean["intervention_type"] = str(intervention_type).upper()
        elif "intervention_type" not in feat_clean:
            feat_clean["intervention_type"] = "EMAIL"

        # 2. Load model
        pipeline = cls._cached_model_pipeline or cls.load_model(model_version)
        version_name = cls._cached_model_version or model_version or DEFAULT_MODEL_VERSION

        if not pipeline:
            # Safe Fallback: cold start heuristic baseline
            logger.info("[COLD_START_FALLBACK] Model not loaded, returning baseline rule probability.")
            return {
                "probability": 0.50,
                "model_version": "fallback_rule_heuristic",
                "is_model_prediction": False,
                "status": "COLD_START",
            }

        # 3. Format into DataFrame for inference
        numerical_cols, categorical_cols = cls.get_feature_columns()
        feature_cols = numerical_cols + categorical_cols

        row_dict = {}
        for col in feature_cols:
            row_dict[col] = feat_clean.get(col, 0.0 if col in numerical_cols else "UNKNOWN")

        df_single = pd.DataFrame([row_dict])

        # 4. Predict
        try:
            proba = float(pipeline.predict_proba(df_single)[0, 1])
            proba = max(0.0, min(1.0, proba))
        except Exception as e:
            logger.error(f"[PREDICTION_ERROR] Pipeline inference failed: {e}")
            return {
                "probability": 0.50,
                "model_version": version_name,
                "is_model_prediction": False,
                "error": str(e),
            }

        prob_rounded = round(proba, 4)
        amount_at_risk = float(feat_clean.get("amount_at_risk", 0.0) or 0.0)
        expected_recovered_value = round(prob_rounded * amount_at_risk, 2)

        return {
            "probability": prob_rounded,
            "expected_recovery_value": expected_recovered_value,
            "model_version": version_name,
            "is_model_prediction": True,
            "status": "PREDICTED",
        }
