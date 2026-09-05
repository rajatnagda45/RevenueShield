"""Tests for Model Registry, Versioning, and Activation Gates."""
from datetime import datetime, timezone
import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.model_version import ModelVersion
from app.ml.pipeline import TrainingPipeline
from app.ml.registry import ModelRegistryService


def test_model_registration_and_activation_lifecycle(db_session: Session):
    """Verify that models are properly registered, gated, and promoted to ACTIVE."""
    df = TrainingPipeline.load_dataset(dataset_type="SYNTHETIC_DEMO")
    package, _ = TrainingPipeline.train_and_evaluate(
        df=df,
        dataset_type="SYNTHETIC_DEMO",
        model_name="test_registry_model",
        version="v1.0.0",
    )
    artifact_path = TrainingPipeline.save_artifact(package)
    now = datetime.now(timezone.utc)

    # 1. Register model
    model_record = ModelRegistryService.register_model(
        db=db_session,
        package=package,
        artifact_path=artifact_path,
        training_started_at=now,
        training_completed_at=now,
    )
    assert model_record.status == "DEVELOPMENT_ONLY"

    # 2. Activate model
    activated = ModelRegistryService.activate_model(db=db_session, model_id=model_record.id)
    assert activated.status == "ACTIVE"
    assert activated.deployed_at is not None

    # Verify active query
    active_in_db = ModelRegistryService.get_active_model(db_session, model_name="test_registry_model")
    assert active_in_db is not None
    assert active_in_db.id == model_record.id

    # 3. Register second model and activate it -> first model should become RETIRED
    package2, _ = TrainingPipeline.train_and_evaluate(
        df=df,
        dataset_type="SYNTHETIC_DEMO",
        model_name="test_registry_model",
        version="v1.1.0",
    )
    artifact_path2 = TrainingPipeline.save_artifact(package2)
    model2 = ModelRegistryService.register_model(
        db=db_session,
        package=package2,
        artifact_path=artifact_path2,
        training_started_at=now,
        training_completed_at=now,
    )
    ModelRegistryService.activate_model(db=db_session, model_id=model2.id)

    db_session.refresh(model_record)
    assert model_record.status == "RETIRED"
    assert model2.status == "ACTIVE"


def test_activate_model_fails_on_missing_artifact(db_session: Session):
    """Verify that activation gate blocks activation if artifact file does not exist on disk."""
    fake_model = ModelVersion(
        id=uuid.uuid4(),
        model_name="ghost_model",
        version="v0.0.1",
        algorithm="LOGISTIC_REGRESSION",
        model_type="LOGISTIC_REGRESSION",
        dataset_type="REAL",
        feature_schema_version="v1",
        metrics={"roc_auc": 0.85},
        status="VALIDATED",
        artifact_path="/non/existent/path/ghost.joblib",
    )
    db_session.add(fake_model)
    db_session.commit()

    with pytest.raises(FileNotFoundError, match="Artifact file .* does not exist"):
        ModelRegistryService.activate_model(db=db_session, model_id=fake_model.id)
