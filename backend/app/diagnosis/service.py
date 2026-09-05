"""Diagnosis lifecycle orchestration service."""
import logging
from typing import Optional
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.audit_log import AuditLog
from app.schemas.event import NormalizedEvent
from app.diagnosis.base import DiagnosisInput, DiagnosisResult, DiagnosisEngine
from app.diagnosis.engine import RuleBasedDiagnosisEngine

logger = logging.getLogger(__name__)


class DiagnosisService:
    """Orchestrates feature extraction, diagnosis evaluation, persistence, and audit logging."""

    _engine: DiagnosisEngine = RuleBasedDiagnosisEngine()

    @classmethod
    def set_engine(cls, engine: DiagnosisEngine) -> None:
        """Swap the active diagnosis engine (e.g. for ML models or testing)."""
        cls._engine = engine

    @classmethod
    def diagnose_case(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        event: Optional[NormalizedEvent] = None,
    ) -> Diagnosis:
        """Run diagnosis on a recovery case, persist diagnosis record, and update case metrics.

        Args:
            db: Database session.
            recovery_case: The open RecoveryCase instance.
            event: Normalized incoming event if available during ingestion.

        Returns:
            The persisted Diagnosis entity.
        """
        from app.services.customer_intelligence import CustomerIntelligenceService
        logger.info(f"[DIAGNOSIS_STARTED] Running diagnosis for RecoveryCase id={recovery_case.id}")

        # 1. Extract historical customer features
        customer_features = CustomerIntelligenceService.get_customer_features(
            db=db,
            customer_id=recovery_case.customer_id,
            reference_time=recovery_case.created_at,
        )

        # 2. Build diagnosis input
        if event:
            error_source = event.failure_source
            error_step = event.failure_step
            error_reason = event.failure_reason
            failure_code = event.failure_code
            failure_description = event.failure_description
            payment_method = event.payment_method
            bank = event.metadata.get("bank") if event.metadata else None
            amount = event.amount
            currency = event.currency
            event_type = event.event_type
        else:
            # Fallback to recovery case & payment attributes
            payment = recovery_case.payment
            error_source = None
            error_step = None
            error_reason = payment.failure_code if payment else None
            failure_code = payment.failure_code if payment else None
            failure_description = payment.failure_description if payment else None
            payment_method = payment.payment_method if payment else None
            bank = None
            amount = recovery_case.amount_at_risk
            currency = recovery_case.currency
            event_type = "payment.failed"

        diagnosis_input = DiagnosisInput(
            event_type=event_type,
            amount=amount,
            currency=currency,
            payment_method=payment_method,
            bank=bank,
            failure_code=failure_code,
            failure_description=failure_description,
            error_source=error_source,
            error_step=error_step,
            error_reason=error_reason,
            customer_features=customer_features,
        )

        # 3. Execute diagnosis engine
        result: DiagnosisResult = cls._engine.diagnose(diagnosis_input)

        # 4. Create and persist Diagnosis record
        diagnosis = Diagnosis(
            recovery_case_id=recovery_case.id,
            category=result.category,
            failure_code=result.failure_code,
            explanation=result.explanation,
            confidence=result.confidence,
            evidence=result.evidence,
            risk_score=result.risk_score,
            recovery_probability=result.recovery_probability,
            engine_version=result.engine_version,
        )
        db.add(diagnosis)

        # 5. Update RecoveryCase risk & recovery probability
        recovery_case.risk_score = result.risk_score
        recovery_case.recovery_probability = result.recovery_probability
        db.flush()

        # 6. Record immutable AuditLog
        audit = AuditLog(
            recovery_case_id=recovery_case.id,
            actor_type="SYSTEM",
            actor_id=result.engine_version,
            action="DIAGNOSIS_CREATED",
            entity_type="RecoveryCase",
            entity_id=str(recovery_case.id),
            audit_metadata={
                "category": result.category,
                "confidence": result.confidence,
                "risk_score": result.risk_score,
                "recovery_probability": result.recovery_probability,
                "engine_version": result.engine_version,
            },
        )
        db.add(audit)
        db.flush()

        logger.info(
            f"[DIAGNOSIS_COMPLETED] Case {recovery_case.id} diagnosed as {result.category} "
            f"with confidence={result.confidence}, risk_score={result.risk_score}, "
            f"recovery_prob={result.recovery_probability}"
        )

        return diagnosis
