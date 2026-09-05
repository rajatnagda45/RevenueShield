"""Explainable Heuristic Recovery Probability Predictor.

Computes a baseline recovery probability (0.0 to 1.0) using explainable domain factors.
Serves as the baseline predictor prior to ML training.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, TYPE_CHECKING
from app.diagnosis.base import RecoveryPredictor
from app.diagnosis.rules import RootCauseCategory

if TYPE_CHECKING:
    from app.services.customer_intelligence import CustomerHistoricalFeatures


class HeuristicRecoveryPredictor(RecoveryPredictor):
    """Calculates an explainable baseline recovery probability."""

    # Baseline recovery likelihood per failure category
    CATEGORY_BASE_PROBABILITIES = {
        RootCauseCategory.BANK_TECHNICAL_FAILURE: 0.85,
        RootCauseCategory.INSUFFICIENT_FUNDS: 0.75,
        RootCauseCategory.AUTHENTICATION_FAILURE: 0.70,
        RootCauseCategory.USER_FRICTION: 0.65,
        RootCauseCategory.BANK_DECLINE: 0.55,
        RootCauseCategory.PAYMENT_METHOD_FAILURE: 0.40,
        RootCauseCategory.MERCHANT_CONFIGURATION: 0.20,
        RootCauseCategory.POSSIBLE_FRAUD_OR_SECURITY: 0.15,
        RootCauseCategory.RECEIVABLE_OVERDUE: 0.60,
        RootCauseCategory.MANDATE_REVOKED_OR_EXPIRED: 0.45,
        RootCauseCategory.SYSTEMIC_ISSUER_DEGRADATION: 0.80,
        RootCauseCategory.UNKNOWN: 0.50,
    }

    def predict_probability(
        self,
        category: str,
        amount: Decimal,
        customer_features: Optional[CustomerHistoricalFeatures],
        evidence: Dict[str, Any],
    ) -> float:
        """Predict baseline recovery probability between 0.05 and 0.98."""
        base_prob = self.CATEGORY_BASE_PROBABILITIES.get(category, 0.50)

        # Factor 1: Customer historical relationship modifier (-0.20 to +0.20)
        history_modifier = 0.0
        if customer_features and customer_features.total_attempts > 0:
            # High success rate boosts recovery
            if customer_features.success_rate >= 0.80:
                history_modifier += 0.10
            elif customer_features.success_rate < 0.50:
                history_modifier -= 0.10

            # Recent success in past 30 days indicates active account
            if customer_features.recent_success_count > 0:
                history_modifier += 0.05

            # Prior successful recoveries prove customer willingness to pay
            if customer_features.previous_recovered_amount > 0:
                history_modifier += 0.05

            # Multiple consecutive failures reduce recovery propensity
            if customer_features.consecutive_failures >= 3:
                history_modifier -= 0.10

        # Factor 2: High transaction amount friction slight modifier (-0.05 to 0.0)
        amount_modifier = 0.0
        if float(amount) > 50000.0:  # High ticket transactions have higher dispute/dropoff risk
            amount_modifier = -0.05

        predicted = base_prob + history_modifier + amount_modifier
        # Clamp between 0.05 and 0.98 to avoid unrealistic absolute certainty
        return round(min(0.98, max(0.05, predicted)), 4)
