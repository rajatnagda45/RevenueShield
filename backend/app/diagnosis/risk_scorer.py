"""Explainable Revenue Risk Scorer.

Computes a normalized revenue risk score (0 to 100) reflecting financial exposure,
failure severity, and customer churn propensity.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Optional, TYPE_CHECKING
from app.diagnosis.base import RiskScorer
from app.diagnosis.rules import RootCauseCategory

if TYPE_CHECKING:
    from app.services.customer_intelligence import CustomerHistoricalFeatures


class NormalizedRiskScorer(RiskScorer):
    """Computes a multi-factor risk score between 0 and 100."""

    # Base severity weight per root cause category (0 to 40 points)
    CATEGORY_SEVERITY_WEIGHTS = {
        RootCauseCategory.POSSIBLE_FRAUD_OR_SECURITY: 40.0,
        RootCauseCategory.PAYMENT_METHOD_FAILURE: 30.0,
        RootCauseCategory.MERCHANT_CONFIGURATION: 30.0,
        RootCauseCategory.BANK_DECLINE: 25.0,
        RootCauseCategory.USER_FRICTION: 20.0,
        RootCauseCategory.INSUFFICIENT_FUNDS: 15.0,
        RootCauseCategory.AUTHENTICATION_FAILURE: 15.0,
        RootCauseCategory.BANK_TECHNICAL_FAILURE: 10.0,
        RootCauseCategory.RECEIVABLE_OVERDUE: 22.0,
        RootCauseCategory.MANDATE_REVOKED_OR_EXPIRED: 30.0,
        RootCauseCategory.SYSTEMIC_ISSUER_DEGRADATION: 8.0,
        RootCauseCategory.UNKNOWN: 25.0,
    }

    def calculate_risk_score(
        self,
        amount: Decimal,
        category: str,
        confidence: float,
        customer_features: Optional[CustomerHistoricalFeatures],
    ) -> float:
        """Calculate revenue risk score (0.0 to 100.0).

        Factors:
        1. Amount Magnitude Component (0 to 30 pts): Log-scaled so ₹100 vs ₹10,000 scale gracefully.
        2. Category Severity (0 to 40 pts): Risk level of the failure type.
        3. Repeat Failures & History (0 to 30 pts): Consecutive failures and historical customer churn signals.
        """
        # Factor 1: Log-scaled Amount Component (0 to 30 pts)
        amt_float = max(0.0, float(amount))
        if amt_float <= 0:
            amount_score = 0.0
        else:
            # log10(100)=2 -> 10pts, log10(1,000)=3 -> 15pts, log10(100,000)=5 -> 25pts, log10(1M)=6 -> 30pts
            amount_score = min(30.0, math.log10(amt_float + 10) * 5.0)

        # Factor 2: Category Severity Component (0 to 40 pts)
        cat_weight = self.CATEGORY_SEVERITY_WEIGHTS.get(category, 25.0)

        # Factor 3: Customer History & Repeat Failures Component (0 to 30 pts)
        history_score = 15.0  # neutral default for new customers
        if customer_features and customer_features.total_attempts > 0:
            # High failure rate increases risk
            failure_rate = 1.0 - customer_features.success_rate
            consecutive = min(5, customer_features.consecutive_failures)
            history_score = (failure_rate * 15.0) + (consecutive * 3.0)
            history_score = min(30.0, max(0.0, history_score))

        total_score = amount_score + cat_weight + history_score
        return round(min(100.0, max(0.0, total_score)), 2)
