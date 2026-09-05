"""Action-Aware Predictive Recovery Service & Expected Value Optimizer."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Dict, List, Optional, Tuple
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.prediction import Prediction
from app.services.customer_intelligence import CustomerIntelligenceService, _to_utc_aware
from app.ml.features import (
    FeatureSchemaV1,
    build_feature_dict_for_inference,
)
from app.ml.pipeline import ModelPackage, TrainingPipeline
from app.ml.registry import ModelRegistryService

logger = logging.getLogger(__name__)


@dataclass
class ActionPrediction:
    """Action-specific prediction outcome with expected value and explainability factors."""

    action: str
    probability: float
    expected_recovered_value: float
    contributing_factors: List[str]
    prediction_id: Optional[str] = None

    @property
    def predicted_probability(self) -> float:
        return self.probability


@dataclass
class CasePredictionResult:
    """Consolidated case prediction response."""

    case_id: str
    strategy: str  # "ML" or "HEURISTIC"
    model_status: str  # "ACTIVE", "INSUFFICIENT_DATA", "FALLBACK"
    model_version: str
    feature_schema_version: str
    amount_at_risk: float
    predictions: List[ActionPrediction]

    @property
    def top_prediction(self) -> Optional[ActionPrediction]:
        return self.predictions[0] if self.predictions else None


class HeuristicPredictor:
    """Baseline heuristic recovery probability estimator for fallback and benchmarking."""

    ACTION_BASE_PROBABILITIES: Dict[str, float] = {
        "RETRY_PAYMENT": 0.65,
        "SEND_PAYMENT_LINK": 0.80,
        "SEND_WHATSAPP_REMINDER": 0.75,
        "SEND_EMAIL_NOTIFICATION": 0.55,
        "OFFER_DISCOUNT": 0.70,
        "CALL_CUSTOMER": 0.60,
    }

    CATEGORY_AFFINITY: Dict[str, Dict[str, float]] = {
        "INSUFFICIENT_FUNDS": {"SEND_WHATSAPP_REMINDER": 0.15, "SEND_PAYMENT_LINK": 0.10, "RETRY_PAYMENT": -0.20},
        "AUTHENTICATION_FAILED": {"SEND_PAYMENT_LINK": 0.20, "SEND_WHATSAPP_REMINDER": 0.10, "RETRY_PAYMENT": -0.30},
        "BANK_TECHNICAL_FAILURE": {"RETRY_PAYMENT": 0.25, "SEND_PAYMENT_LINK": 0.05},
        "PAYMENT_METHOD_INVALID": {"SEND_PAYMENT_LINK": 0.20, "SEND_WHATSAPP_REMINDER": 0.15, "RETRY_PAYMENT": -0.40},
        "USER_FRICTION": {"SEND_PAYMENT_LINK": 0.20, "OFFER_DISCOUNT": 0.15},
    }

    @classmethod
    def predict_action(
        cls,
        action: str,
        diagnosis_category: str,
        customer_success_rate: float,
        previous_attempts: int,
        risk_score: float,
    ) -> Tuple[float, List[str]]:
        """Estimate recovery probability and generate explainability factors under heuristic baseline."""
        base_p = cls.ACTION_BASE_PROBABILITIES.get(action, 0.50)
        affinity = cls.CATEGORY_AFFINITY.get(diagnosis_category, {}).get(action, 0.0)

        p = base_p + affinity

        # Adjust for customer history
        if customer_success_rate > 0.75:
            p += 0.10
        elif customer_success_rate < 0.25 and customer_success_rate > 0:
            p -= 0.10

        # Penalty for repeated attempts
        if previous_attempts > 0:
            p -= 0.08 * previous_attempts

        # Penalty for high risk score
        if risk_score > 70.0:
            p -= 0.10

        p = round(min(0.98, max(0.05, p)), 2)

        # Contributing explainability factors
        factors = []
        if affinity > 0:
            factors.append(f"Historical category affinity: {diagnosis_category} is responsive to {action}.")
        elif affinity < 0:
            factors.append(f"Historical friction: {diagnosis_category} has lower success with direct {action}.")

        if customer_success_rate > 0.75:
            factors.append(f"Customer has high historical payment success rate ({int(customer_success_rate*100)}%).")
        elif previous_attempts > 0:
            factors.append(f"Adjusted downward due to {previous_attempts} prior attempt(s).")

        factors.append(f"Baseline heuristic confidence for {action}.")
        return p, factors


class PredictionService:
    """Production Prediction Service evaluating candidate actions and computing Expected Recovered Value."""

    ELIGIBLE_RECOVERY_ACTIONS = [
        "SEND_PAYMENT_LINK",
        "RETRY_PAYMENT",
        "SEND_WHATSAPP_REMINDER",
        "SEND_EMAIL_NOTIFICATION",
        "OFFER_DISCOUNT",
    ]

    _loaded_package: Optional[ModelPackage] = None
    _loaded_model_id: Optional[str] = None

    @classmethod
    def get_or_load_active_model(cls, db: Session) -> Optional[ModelPackage]:
        """Fetch and cache the active model package artifact."""
        active_model_record = ModelRegistryService.get_active_model(db)
        if not active_model_record:
            cls._loaded_package = None
            cls._loaded_model_id = None
            return None

        if cls._loaded_package and cls._loaded_model_id == str(active_model_record.id):
            return cls._loaded_package

        try:
            package = TrainingPipeline.load_artifact(active_model_record.artifact_path)
            cls._loaded_package = package
            cls._loaded_model_id = str(active_model_record.id)
            return package
        except Exception as exc:
            logger.error(f"Failed to load active model artifact {active_model_record.id}: {exc}")
            return None

    @classmethod
    def predict_for_case(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        eligible_actions: Optional[List[str]] = None,
        save_predictions: bool = True,
    ) -> CasePredictionResult:
        """Generate action-level recovery probabilities and Expected Recovered Values.

        Args:
            db: Database session.
            recovery_case: RecoveryCase instance.
            eligible_actions: Optional list of candidate actions to score (defaults to all ELIGIBLE_RECOVERY_ACTIONS).
            save_predictions: Whether to persist Prediction records in PostgreSQL.

        Returns:
            CasePredictionResult containing ranked action predictions.
        """
        actions = eligible_actions or cls.ELIGIBLE_RECOVERY_ACTIONS
        amount = float(recovery_case.amount_at_risk or 0.0)

        # 1. Fetch case diagnosis & customer intelligence
        diagnosis = db.scalar(
            select(Diagnosis)
            .where(Diagnosis.recovery_case_id == recovery_case.id)
            .order_by(Diagnosis.created_at.desc())
            .limit(1)
        )
        diag_category = diagnosis.category if diagnosis else "UNKNOWN"
        diag_confidence = diagnosis.confidence if diagnosis else 0.50
        risk_score = float(recovery_case.risk_score or 50.0)
        heuristic_prob = float(recovery_case.recovery_probability or 0.50)

        # Calculate case age
        now = datetime.now(timezone.utc)
        case_created_at = _to_utc_aware(recovery_case.created_at) or now
        case_age_hours = max(0.0, (now - case_created_at).total_seconds() / 3600.0)

        # Customer Intelligence
        customer = recovery_case.customer
        cust_profile = CustomerIntelligenceService.get_customer_features(db, customer.id) if customer else None
        succ_rate = float(getattr(cust_profile, "success_rate", 0.0) if cust_profile else 0.0)
        succ_count = int(getattr(cust_profile, "successful_count", 0) if cust_profile else 0)
        fail_count = int(getattr(cust_profile, "failed_count", 0) if cust_profile else 0)
        prev_attempts = int(recovery_case.retry_count or 0)

        # Payment details
        payment = recovery_case.payment
        payment_method = payment.payment_method if payment else "CARD"
        bank = payment.bank if payment else None
        error_code = payment.failure_code if payment else None
        error_source = payment.error_source if payment else None
        error_step = payment.error_step if payment else None
        error_reason = payment.error_reason if payment else None

        # 2. Check for Active ML Model Package
        active_model_record = ModelRegistryService.get_active_model(db)
        model_package = cls.get_or_load_active_model(db)

        predictions_list: List[ActionPrediction] = []

        if model_package and active_model_record:
            strategy = "ML"
            model_status = active_model_record.status
            model_version = active_model_record.version

            # Construct inference DataFrame
            feature_rows = []
            for act in actions:
                f_dict = build_feature_dict_for_inference(
                    amount_at_risk=amount,
                    case_age_at_decision_hours=case_age_hours,
                    diagnosis_category=diag_category,
                    diagnosis_confidence=diag_confidence,
                    risk_score=risk_score,
                    heuristic_recovery_probability=heuristic_prob,
                    customer_success_rate=succ_rate,
                    customer_success_count=succ_count,
                    customer_failure_count=fail_count,
                    previous_recovery_attempts=prev_attempts,
                    payment_method=payment_method,
                    bank=bank,
                    error_code=error_code,
                    error_source=error_source,
                    error_step=error_step,
                    error_reason=error_reason,
                    action_type=act,
                )
                feature_rows.append(f_dict)

            infer_df = pd.DataFrame(feature_rows)

            try:
                # Predict probabilities
                probs = model_package.pipeline.predict_proba(infer_df)[:, 1]

                for act, p in zip(actions, probs):
                    p_val = round(float(p), 2)
                    ev = round(p_val * amount, 2)
                    # Explainability factors
                    factors = [
                        f"Associated with {diag_category} diagnosis pattern (confidence {int(diag_confidence*100)}%).",
                        f"Customer historical success rate of {int(succ_rate*100)}% ({succ_count} captured, {fail_count} failed).",
                        f"Action {act} predicted recovery probability {int(p_val*100)}%.",
                    ]
                    predictions_list.append(
                        ActionPrediction(
                            action=act,
                            probability=p_val,
                            expected_recovered_value=ev,
                            contributing_factors=factors,
                        )
                    )
            except Exception as exc:
                logger.error(f"Inference error with active model: {exc}. Falling back to heuristic.")
                strategy = "HEURISTIC"
                model_status = "FALLBACK"
                model_version = "heuristic_v1"
                predictions_list = cls._heuristic_predictions(
                    actions, amount, diag_category, succ_rate, prev_attempts, risk_score
                )
        else:
            # 3. Heuristic Fallback Strategy
            strategy = "HEURISTIC"
            model_status = "INSUFFICIENT_DATA"
            model_version = "heuristic_v1"
            predictions_list = cls._heuristic_predictions(
                actions, amount, diag_category, succ_rate, prev_attempts, risk_score
            )

        # Sort predictions descending by Expected Recovered Value
        predictions_list.sort(key=lambda x: x.expected_recovered_value, reverse=True)

        # 4. Store Prediction Records for Auditing & Drift Monitoring
        if save_predictions:
            for pred in predictions_list:
                db_pred = Prediction(
                    recovery_case_id=recovery_case.id,
                    model_version_id=active_model_record.id if (strategy == "ML" and active_model_record) else None,
                    model_version=model_version,
                    feature_schema_version=FeatureSchemaV1.SCHEMA_VERSION,
                    action_type=pred.action,
                    predicted_probability=pred.probability,
                    expected_recovered_value=Decimal(str(pred.expected_recovered_value)),
                    strategy=strategy,
                    contributing_factors={"factors": pred.contributing_factors},
                )
                db.add(db_pred)
                db.flush()
                pred.prediction_id = str(db_pred.id)
            db.flush()

        return CasePredictionResult(
            case_id=str(recovery_case.id),
            strategy=strategy,
            model_status=model_status,
            model_version=model_version,
            feature_schema_version=FeatureSchemaV1.SCHEMA_VERSION,
            amount_at_risk=amount,
            predictions=predictions_list,
        )

    @classmethod
    def _heuristic_predictions(
        cls,
        actions: List[str],
        amount: float,
        diagnosis_category: str,
        customer_success_rate: float,
        previous_attempts: int,
        risk_score: float,
    ) -> List[ActionPrediction]:
        """Generate heuristic predictions and expected values."""
        results = []
        for act in actions:
            prob, factors = HeuristicPredictor.predict_action(
                action=act,
                diagnosis_category=diagnosis_category,
                customer_success_rate=customer_success_rate,
                previous_attempts=previous_attempts,
                risk_score=risk_score,
            )
            ev = round(prob * amount, 2)
            results.append(
                ActionPrediction(
                    action=act,
                    probability=prob,
                    expected_recovered_value=ev,
                    contributing_factors=factors,
                )
            )
        return results
