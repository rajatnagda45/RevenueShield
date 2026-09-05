"""Machine Learning, Model Registry & Predictive Recovery API Endpoints."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.recovery_case import RecoveryCase
from app.models.model_version import ModelVersion
from app.ml.features import FeatureSchemaV1
from app.ml.pipeline import TrainingPipeline, DataSufficiencyResult
from app.ml.registry import ModelRegistryService
from app.ml.prediction_service import PredictionService, CasePredictionResult

logger = logging.getLogger(__name__)

router = APIRouter()


class TrainModelRequest(BaseModel):
    """Request payload for triggering model training."""

    model_config = ConfigDict(populate_by_name=True)

    dataset_type: str = Field(
        default="REAL",
        description="Dataset source: 'REAL' (production LearningExamples) or 'SYNTHETIC_DEMO' (sandbox verification).",
    )
    model_name: str = Field(
        default="recovery_value_predictor",
        description="Logical name of the model family.",
    )
    version: Optional[str] = Field(
        default=None,
        description="Optional semantic version string (e.g. 'v1.0.0'). Auto-generated if omitted.",
    )


class TrainModelResponse(BaseModel):
    """Response returned upon training completion or insufficiency detection."""

    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(..., description="'VALIDATED', 'DEVELOPMENT_ONLY', or 'INSUFFICIENT_DATA'")
    model_id: Optional[str] = Field(None, description="UUID of the registered model version")
    version: Optional[str] = Field(None, description="Model version")
    model_type: Optional[str] = Field(None, description="Algorithm selected (e.g. LOGISTIC_REGRESSION)")
    dataset_type: str = Field(..., description="'REAL' or 'SYNTHETIC_DEMO'")
    metrics: Optional[Dict[str, Any]] = Field(None, description="Evaluation metrics")
    sufficiency: Dict[str, Any] = Field(..., description="Dataset sufficiency check report")
    message: str = Field(..., description="Summary message")


class ModelVersionResponse(BaseModel):
    """Summary of a model registered in the ModelRegistry."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    model_name: str
    version: str
    model_type: str
    dataset_type: str
    feature_schema_version: str
    status: str
    metrics: Dict[str, Any]
    created_at: str
    deployed_at: Optional[str] = None


class ActionPredictionItem(BaseModel):
    """Single action prediction result."""

    action: str
    probability: float
    expected_recovered_value: float
    contributing_factors: List[str]


class CasePredictionsResponse(BaseModel):
    """Action-level predictions and Expected Recovered Values for a recovery case."""

    model_config = ConfigDict(populate_by_name=True)

    case_id: str
    strategy: str
    model_status: str
    model_version: str
    feature_schema_version: str
    amount_at_risk: float
    predictions: List[ActionPredictionItem]


# -----------------------------------------------------------------------------
# Admin ML Endpoints
# -----------------------------------------------------------------------------

@router.post(
    "/admin/ml/train",
    response_model=TrainModelResponse,
    status_code=status.HTTP_200_OK,
    summary="Train candidate recovery models",
    tags=["Machine Learning"],
)
def train_recovery_model(
    request: TrainModelRequest,
    db: Session = Depends(get_db),
) -> TrainModelResponse:
    """Train candidate models on real learning examples (or synthetic demo dataset) and register the champion."""
    started_at = datetime.now(timezone.utc)
    dataset_type = request.dataset_type.upper()
    if dataset_type not in ("REAL", "SYNTHETIC_DEMO"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="dataset_type must be either 'REAL' or 'SYNTHETIC_DEMO'",
        )

    # 1. Load dataset
    try:
        df = TrainingPipeline.load_dataset(db=db, dataset_type=dataset_type)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load dataset: {exc}",
        )

    # 2. Check Data Sufficiency Gate
    sufficiency: DataSufficiencyResult = TrainingPipeline.check_sufficiency(df)
    sufficiency_dict = {
        "is_sufficient": sufficiency.is_sufficient,
        "total_examples": sufficiency.total_examples,
        "positive_examples": sufficiency.positive_examples,
        "negative_examples": sufficiency.negative_examples,
        "positive_ratio": sufficiency.positive_ratio,
        "reason": sufficiency.reason,
    }

    if not sufficiency.is_sufficient:
        logger.warning(f"[ML_TRAIN_BLOCKED] Dataset insufficiency: {sufficiency.reason}")
        return TrainModelResponse(
            status="INSUFFICIENT_DATA",
            model_id=None,
            version=None,
            model_type=None,
            dataset_type=dataset_type,
            metrics=None,
            sufficiency=sufficiency_dict,
            message=f"Training halted: {sufficiency.reason} System will continue using HEURISTIC strategy.",
        )

    # 3. Train, Evaluate & Select Champion Model
    version_str = request.version or f"v{int(started_at.timestamp())}"
    try:
        package, all_metrics = TrainingPipeline.train_and_evaluate(
            df=df,
            dataset_type=dataset_type,
            model_name=request.model_name,
            version=version_str,
        )
    except Exception as exc:
        logger.error(f"Model training failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Model training failed during fitting/evaluation: {exc}",
        )

    # 4. Save Artifact
    artifact_path = TrainingPipeline.save_artifact(package)
    completed_at = datetime.now(timezone.utc)

    # 5. Register in ModelRegistry
    registered_record = ModelRegistryService.register_model(
        db=db,
        package=package,
        artifact_path=artifact_path,
        training_started_at=started_at,
        training_completed_at=completed_at,
    )

    return TrainModelResponse(
        status=registered_record.status,
        model_id=str(registered_record.id),
        version=registered_record.version,
        model_type=registered_record.model_type,
        dataset_type=dataset_type,
        metrics=registered_record.metrics,
        sufficiency=sufficiency_dict,
        message=f"Model {registered_record.version} successfully trained and registered as {registered_record.status}.",
    )


@router.post(
    "/admin/ml/models/{model_id}/activate",
    response_model=ModelVersionResponse,
    status_code=status.HTTP_200_OK,
    summary="Activate a validated ML model",
    tags=["Machine Learning"],
)
def activate_recovery_model(
    model_id: uuid.UUID = PathParam(..., description="UUID of the model version to activate"),
    db: Session = Depends(get_db),
) -> ModelVersionResponse:
    """Promote a registered model to ACTIVE status and retire previously active model."""
    try:
        activated = ModelRegistryService.activate_model(db=db, model_id=model_id)
        return ModelVersionResponse(
            id=str(activated.id),
            model_name=activated.model_name,
            version=activated.version,
            model_type=activated.model_type,
            dataset_type=activated.dataset_type,
            feature_schema_version=activated.feature_schema_version,
            status=activated.status,
            metrics=activated.metrics,
            created_at=activated.created_at.isoformat(),
            deployed_at=activated.deployed_at.isoformat() if activated.deployed_at else None,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.get(
    "/admin/ml/models",
    response_model=List[ModelVersionResponse],
    status_code=status.HTTP_200_OK,
    summary="List all registered models",
    tags=["Machine Learning"],
)
def list_recovery_models(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> List[ModelVersionResponse]:
    """Retrieve all models from the Model Registry."""
    models = ModelRegistryService.list_models(db=db, limit=limit)
    return [
        ModelVersionResponse(
            id=str(m.id),
            model_name=m.model_name,
            version=m.version,
            model_type=m.model_type,
            dataset_type=m.dataset_type,
            feature_schema_version=m.feature_schema_version,
            status=m.status,
            metrics=m.metrics,
            created_at=m.created_at.isoformat(),
            deployed_at=m.deployed_at.isoformat() if m.deployed_at else None,
        )
        for m in models
    ]


@router.get(
    "/admin/ml/feature-drift",
    status_code=status.HTTP_200_OK,
    summary="Compute feature drift and distribution statistics",
    tags=["Machine Learning"],
)
def get_feature_drift_statistics(
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Compute distribution statistics across numerical and categorical features."""
    df = TrainingPipeline.load_dataset(db=db, dataset_type="REAL")
    if len(df) == 0:
        return {
            "status": "NO_DATA",
            "total_examples": 0,
            "feature_statistics": {},
        }

    stats = {}
    for num_col in FeatureSchemaV1.NUMERICAL_FEATURES:
        if num_col in df.columns:
            s = df[num_col].dropna()
            stats[num_col] = {
                "type": "numerical",
                "mean": round(float(s.mean()), 2) if len(s) > 0 else 0.0,
                "median": round(float(s.median()), 2) if len(s) > 0 else 0.0,
                "min": round(float(s.min()), 2) if len(s) > 0 else 0.0,
                "max": round(float(s.max()), 2) if len(s) > 0 else 0.0,
                "missing_rate": round(float(df[num_col].isna().mean()), 3),
            }

    for cat_col in FeatureSchemaV1.CATEGORICAL_FEATURES:
        if cat_col in df.columns:
            dist = df[cat_col].value_counts(normalize=True).to_dict()
            stats[cat_col] = {
                "type": "categorical",
                "distribution": {str(k): round(float(v), 3) for k, v in dist.items()},
                "missing_rate": round(float(df[cat_col].isna().mean()), 3),
            }

    return {
        "status": "COMPUTED",
        "total_examples": len(df),
        "feature_statistics": stats,
    }


# -----------------------------------------------------------------------------
# Public / Operational Prediction Endpoint
# -----------------------------------------------------------------------------

@router.get(
    "/recovery-cases/{case_id}/predictions",
    response_model=CasePredictionsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get action-level recovery probabilities and Expected Recovered Values",
    tags=["Predictions"],
)
def get_case_predictions(
    case_id: uuid.UUID = PathParam(..., description="UUID of the RecoveryCase"),
    db: Session = Depends(get_db),
) -> CasePredictionsResponse:
    """Fetch predictive recovery scoring and Expected Recovered Value for all eligible actions."""
    recovery_case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not recovery_case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery case '{case_id}' not found",
        )

    res: CasePredictionResult = PredictionService.predict_for_case(
        db=db,
        recovery_case=recovery_case,
        save_predictions=True,
    )

    return CasePredictionsResponse(
        case_id=res.case_id,
        strategy=res.strategy,
        model_status=res.model_status,
        model_version=res.model_version,
        feature_schema_version=res.feature_schema_version,
        amount_at_risk=res.amount_at_risk,
        predictions=[
            ActionPredictionItem(
                action=p.action,
                probability=p.probability,
                expected_recovered_value=p.expected_recovered_value,
                contributing_factors=p.contributing_factors,
            )
            for p in res.predictions
        ],
    )


class ModelStatusResponse(BaseModel):
    """Status and performance summary of active ML model."""

    model_config = ConfigDict(populate_by_name=True)

    status: str
    active_model_version: Optional[str] = None
    algorithm: Optional[str] = None
    training_samples: int = 0
    metrics: Dict[str, Any] = {}
    drift_status: Dict[str, Any] = {}


@router.get(
    "/ml/model/status",
    response_model=ModelStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get active ML model status, metrics, and drift indicators",
    tags=["Machine Learning"],
)
def get_ml_model_status(
    db: Session = Depends(get_db),
):
    """Retrieve the currently active model version, metrics, sample volume, and drift indicators."""
    from app.ml.drift_detector import ModelDriftDetector
    active_model = ModelRegistryService.get_active_model(db, model_name="action_recovery_model")
    if not active_model:
        return ModelStatusResponse(
            status="INSUFFICIENT_DATA",
            active_model_version=None,
            algorithm=None,
            training_samples=0,
            metrics={},
            drift_status={"status": "STABLE", "psi": 0.0},
        )

    drift_report = ModelDriftDetector.assess_model_drift(db, model_version=active_model.version)

    return ModelStatusResponse(
        status=active_model.status,
        active_model_version=active_model.version,
        algorithm=active_model.algorithm,
        training_samples=active_model.metrics.get("training_samples", 0) if active_model.metrics else 0,
        metrics=active_model.metrics or {},
        drift_status=drift_report,
    )


# -----------------------------------------------------------------------------
# Step 13: Closed-Loop Self-Learning Endpoints
# -----------------------------------------------------------------------------

class LearningStatusResponse(BaseModel):
    """Dataset volume and batch retraining trigger thresholds."""

    model_config = ConfigDict(populate_by_name=True)

    training_examples: int
    eligible_examples: int
    pending_examples: int
    last_training_time: Optional[str] = None
    next_training_threshold: int


@router.get(
    "/ml/learning/status",
    response_model=LearningStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get self-learning dataset statistics and retraining eligibility",
    tags=["Self-Learning Feedback Loop"],
)
def get_learning_status(
    db: Session = Depends(get_db),
):
    """Retrieve learning dataset counts, eligible sample volume, and threshold trigger status."""
    from sqlalchemy import func
    from app.core.config import settings
    from app.models.learning import LearningExample
    from app.models.model_evaluation import ModelEvaluation

    total = db.scalar(select(func.count(LearningExample.id))) or 0
    eligible = db.scalar(
        select(func.count(LearningExample.id)).where(LearningExample.training_eligible == True)
    ) or 0
    pending = total - eligible

    latest_eval = db.scalar(
        select(ModelEvaluation).order_by(ModelEvaluation.created_at.desc()).limit(1)
    )

    return LearningStatusResponse(
        training_examples=total,
        eligible_examples=eligible,
        pending_examples=pending,
        last_training_time=latest_eval.created_at.isoformat() if latest_eval else None,
        next_training_threshold=settings.RETRAINING_SCHEDULE_THRESHOLD,
    )


class PerformanceDashboardResponse(BaseModel):
    """Aggregated performance metrics across business, actions, and models."""

    model_config = ConfigDict(populate_by_name=True)

    business_performance: Dict[str, Any]
    action_performance: List[Dict[str, Any]]
    action_imbalance: Dict[str, Any]


@router.get(
    "/ml/performance",
    response_model=PerformanceDashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Get comprehensive recovery performance across business KPIs and actions",
    tags=["Self-Learning Feedback Loop"],
)
def get_performance_metrics(
    db: Session = Depends(get_db),
):
    """Retrieve business KPIs, per-action recovery conversion rates, and action selection distribution."""
    from app.services.learning_metrics_service import LearningMetricsService

    biz = LearningMetricsService.compute_business_metrics(db)
    acts = LearningMetricsService.compute_action_performance(db)
    imbalance = LearningMetricsService.detect_action_imbalance(db)

    return PerformanceDashboardResponse(
        business_performance=biz,
        action_performance=acts,
        action_imbalance=imbalance,
    )


class RetrainRequest(BaseModel):
    """Retrain trigger payload."""

    model_config = ConfigDict(populate_by_name=True)
    force: bool = Field(default=True, description="Force training even if threshold is not reached")
    auto_promote: bool = Field(default=False, description="Automatically promote if candidate meets quality gates")


@router.post(
    "/ml/retrain",
    status_code=status.HTTP_200_OK,
    summary="Trigger candidate model batch retraining",
    tags=["Self-Learning Feedback Loop"],
)
def trigger_retraining(
    request: RetrainRequest = RetrainRequest(),
    db: Session = Depends(get_db),
):
    """Trigger candidate model retraining and validation evaluation without automatic activation."""
    from app.ml.retraining_service import RetrainingService

    res = RetrainingService.execute_retraining(
        db=db,
        force=request.force,
        auto_promote=request.auto_promote,
    )
    return res


@router.post(
    "/ml/models/{version}/promote",
    status_code=status.HTTP_200_OK,
    summary="Promote validated candidate model to active status",
    tags=["Self-Learning Feedback Loop"],
)
def promote_model_version(
    version: str = PathParam(..., description="Model version string to promote"),
    db: Session = Depends(get_db),
):
    """Promote a candidate model version to ACTIVE after quality gate validation."""
    cand = db.scalar(select(ModelVersion).where(ModelVersion.version == version))
    if not cand:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Model version '{version}' not found.")

    res = ModelRegistryService.evaluate_and_promote(db=db, candidate_model_id=cand.id)
    return res


@router.post(
    "/ml/models/{version}/rollback",
    status_code=status.HTTP_200_OK,
    summary="Rollback active model to target or previous validated version",
    tags=["Self-Learning Feedback Loop"],
)
def rollback_model_version(
    version: str = PathParam(..., description="Target version to restore, or 'latest'"),
    db: Session = Depends(get_db),
):
    """Roll back production model deployment to the specified or previous champion checkpoint."""
    from app.ml.model_rollback_service import ModelRollbackService

    try:
        res = ModelRollbackService.rollback_to_previous_model(
            db=db,
            target_version=version if version and version != "latest" else None,
        )
        return res
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
