"""Rule-based implementation of the Diagnosis Engine (v1)."""
import logging
from app.diagnosis.base import (
    DiagnosisEngine,
    DiagnosisInput,
    DiagnosisResult,
    RiskScorer,
    RecoveryPredictor,
)
from app.diagnosis.rules import RuleEngine
from app.diagnosis.risk_scorer import NormalizedRiskScorer
from app.diagnosis.recovery_predictor import HeuristicRecoveryPredictor

logger = logging.getLogger(__name__)


class RuleBasedDiagnosisEngine(DiagnosisEngine):
    """Deterministic, explainable Rule-Based Diagnosis Engine (v1)."""

    version: str = "diagnosis_engine_v1"

    def __init__(
        self,
        risk_scorer: RiskScorer = None,  # type: ignore[assignment]
        recovery_predictor: RecoveryPredictor = None,  # type: ignore[assignment]
    ):
        self.risk_scorer = risk_scorer or NormalizedRiskScorer()
        self.recovery_predictor = recovery_predictor or HeuristicRecoveryPredictor()

    def diagnose(self, input_data: DiagnosisInput) -> DiagnosisResult:
        """Perform deterministic diagnosis on incoming payment failure signals."""
        logger.info(
            f"[DIAGNOSIS_STARTED] Evaluating failure for amount={input_data.amount} "
            f"currency={input_data.currency} method={input_data.payment_method}"
        )

        # 1. Evaluate root cause rules
        rule_match = RuleEngine.evaluate(
            error_source=input_data.error_source,
            error_step=input_data.error_step,
            error_reason=input_data.error_reason,
            error_code=input_data.failure_code,
            failure_description=input_data.failure_description,
            payment_method=input_data.payment_method,
        )

        # 2. Compile structured evidence (masking sensitive tokens)
        evidence = {
            "error_source": input_data.error_source,
            "error_step": input_data.error_step,
            "error_reason": input_data.error_reason,
            "error_code": input_data.failure_code,
            "failure_description": input_data.failure_description,
            "payment_method": input_data.payment_method,
            "bank": input_data.bank,
            "matched_rule": rule_match.matched_rule,
            "customer_history": (
                input_data.customer_features.to_dict()
                if input_data.customer_features
                else None
            ),
        }

        # 3. Compute revenue risk score (0 to 100)
        risk_score = self.risk_scorer.calculate_risk_score(
            amount=input_data.amount,
            category=rule_match.category,
            confidence=rule_match.base_confidence,
            customer_features=input_data.customer_features,
        )

        # 4. Predict baseline recovery probability (0.0 to 1.0)
        recovery_prob = self.recovery_predictor.predict_probability(
            category=rule_match.category,
            amount=input_data.amount,
            customer_features=input_data.customer_features,
            evidence=evidence,
        )

        logger.info(
            f"[DIAGNOSIS_COMPLETED] Category={rule_match.category} "
            f"Confidence={rule_match.base_confidence} RiskScore={risk_score} "
            f"RecoveryProb={recovery_prob} EngineVersion={self.version}"
        )

        return DiagnosisResult(
            category=rule_match.category,
            failure_code=input_data.failure_code or input_data.error_reason,
            explanation=rule_match.explanation,
            confidence=rule_match.base_confidence,
            risk_score=risk_score,
            recovery_probability=recovery_prob,
            engine_version=self.version,
            evidence=evidence,
        )
