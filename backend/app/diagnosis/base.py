"""Abstract protocols and data structures for Diagnosis and Recovery Prediction."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.customer_intelligence import CustomerHistoricalFeatures


@dataclass
class DiagnosisInput:
    """Consolidated input payload for diagnosis evaluation."""

    event_type: str
    amount: Decimal
    currency: str
    payment_method: Optional[str] = None
    bank: Optional[str] = None
    failure_code: Optional[str] = None
    failure_description: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    error_reason: Optional[str] = None
    customer_features: Optional[CustomerHistoricalFeatures] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DiagnosisResult:
    """Standardized output produced by any diagnosis engine."""

    category: str
    failure_code: Optional[str]
    explanation: str
    confidence: float
    risk_score: float  # 0 to 100
    recovery_probability: float  # 0.0 to 1.0
    engine_version: str
    evidence: Dict[str, Any]


class DiagnosisEngine(Protocol):
    """Protocol for diagnosis engines (Rule-based or ML/LLM in future)."""

    version: str

    def diagnose(self, input_data: DiagnosisInput) -> DiagnosisResult:
        """Evaluate input data and return a complete DiagnosisResult."""
        ...


class RecoveryPredictor(Protocol):
    """Protocol for recovery probability predictors."""

    def predict_probability(
        self,
        category: str,
        amount: Decimal,
        customer_features: Optional[CustomerHistoricalFeatures],
        evidence: Dict[str, Any],
    ) -> float:
        """Compute recovery probability between 0.0 and 1.0."""
        ...


class RiskScorer(Protocol):
    """Protocol for revenue risk scorers."""

    def calculate_risk_score(
        self,
        amount: Decimal,
        category: str,
        confidence: float,
        customer_features: Optional[CustomerHistoricalFeatures],
    ) -> float:
        """Compute revenue risk score between 0.0 and 100.0."""
        ...
