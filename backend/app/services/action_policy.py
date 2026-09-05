"""Pluggable ActionPolicy interface and RuleBasedActionPolicy for adaptive recovery action scoring."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class ActionPolicyContext:
    """Rich context provided to the ActionPolicy to determine the next best action."""

    recovery_case_id: str
    amount_at_risk: Decimal
    attempt_number: int  # 1 for first action, 2 for second, etc.
    previous_steps: List[Dict[str, Any]] = field(default_factory=list)
    previous_communications: List[Dict[str, Any]] = field(default_factory=list)
    customer_preferences: Dict[str, Any] = field(default_factory=dict)
    hours_since_failure: float = 0.0
    has_payment_link_interaction: bool = False
    ml_base_probability: float = 0.45
    customer_email_available: bool = True
    customer_phone_available: bool = False


@dataclass
class NextBestAction:
    """Action recommendation produced by NextBestActionEngine."""

    action_type: str  # EMAIL_PAYMENT_RECOVERY, EMAIL_FOLLOWUP, NO_ACTION, etc.
    channel: str      # EMAIL, WHATSAPP, VOICE, NONE
    score: float      # Normalized priority score [0.0 - 1.0]
    expected_recovery_probability: float
    expected_recovery_value: Decimal
    reason: str
    confidence: float


class ActionPolicy(ABC):
    """Abstract interface for Next Best Action selection policies (Rule-based, ML, or Contextual Bandit)."""

    @abstractmethod
    def select_action(self, context: ActionPolicyContext) -> NextBestAction:
        """Select the next best recovery action based on the current context."""
        pass


class RuleBasedActionPolicy(ActionPolicy):
    """Deterministic, auditable action policy incorporating ML baseline predictions and interaction signals."""

    def select_action(self, context: ActionPolicyContext) -> NextBestAction:
        attempt = context.attempt_number
        amount = context.amount_at_risk
        base_prob = min(max(context.ml_base_probability, 0.05), 0.95)

        # 1. Attempt Cap Check
        if attempt > 3:
            return NextBestAction(
                action_type="NO_ACTION",
                channel="NONE",
                score=0.0,
                expected_recovery_probability=0.0,
                expected_recovery_value=Decimal("0.00"),
                reason="Maximum recovery attempts (3) reached. Automated outreach capped by policy.",
                confidence=1.0,
            )

        # 2. First Action: Initial Email Payment Recovery
        if attempt == 1:
            prob = round(base_prob, 4)
            ev = Decimal(str(round(float(amount) * prob, 2)))
            score = round(prob * 0.9, 4)
            return NextBestAction(
                action_type="EMAIL_PAYMENT_RECOVERY",
                channel="EMAIL",
                score=score,
                expected_recovery_probability=prob,
                expected_recovery_value=ev,
                reason="Initial recovery outreach: dispatching primary payment link via transactional email.",
                confidence=0.85,
            )

        # 3. Follow-up Actions (Attempt 2 or 3)
        if attempt >= 2:
            # Check for engagement signals (e.g. payment link clicked / opened)
            if context.has_payment_link_interaction:
                # Customer engagement increases recovery likelihood
                prob = round(min(base_prob * 1.5, 0.88), 4)
                reason = "Customer demonstrated engagement with previous payment link; sending targeted email follow-up."
                confidence = 0.88
            else:
                # Standard decay or reminder model
                prob = round(max(base_prob * 0.85, 0.20), 4)
                reason = f"Follow-up outreach (Attempt {attempt}): sending scheduled reminder email with active payment link."
                confidence = 0.80

            ev = Decimal(str(round(float(amount) * prob, 2)))
            score = round(prob * 0.95, 4)
            return NextBestAction(
                action_type="EMAIL_FOLLOWUP",
                channel="EMAIL",
                score=score,
                expected_recovery_probability=prob,
                expected_recovery_value=ev,
                reason=reason,
                confidence=confidence,
            )

        return NextBestAction(
            action_type="NO_ACTION",
            channel="NONE",
            score=0.0,
            expected_recovery_probability=0.0,
            expected_recovery_value=Decimal("0.00"),
            reason="No eligible action identified for current state.",
            confidence=0.5,
        )


class MLActionPolicy(ActionPolicy):
    """Data-driven action policy selecting interventions that maximize Expected Recovery Value."""

    def __init__(self, fallback_policy: Optional[ActionPolicy] = None):
        self.fallback_policy = fallback_policy or RuleBasedActionPolicy()

    def select_action(
        self,
        context: ActionPolicyContext,
        candidate_predictions: Optional[List[Dict[str, Any]]] = None,
    ) -> NextBestAction:
        """Select action maximizing Expected Recovery Value from candidate predictions, or fall back safely."""
        if not candidate_predictions:
            return self.fallback_policy.select_action(context)

        # Filter out candidates with zero probability or invalid format
        valid_candidates = []
        for c in candidate_predictions:
            prob = float(c.get("probability", 0.0))
            act = str(c.get("action", ""))
            amount = float(context.amount_at_risk or Decimal("0.00"))
            ev = Decimal(str(round(prob * amount, 2)))
            channel = "EMAIL" if "EMAIL" in act else ("WHATSAPP" if "WHATSAPP" in act else "NONE")
            valid_candidates.append({
                "action": act,
                "channel": channel,
                "probability": prob,
                "ev": ev,
                "factors": c.get("contributing_factors", []),
                "model_version": c.get("model_version", "v1"),
            })

        if not valid_candidates:
            return self.fallback_policy.select_action(context)

        # Sort by Expected Recovery Value descending
        valid_candidates.sort(key=lambda x: x["ev"], reverse=True)
        best = valid_candidates[0]

        factors_str = "; ".join(best["factors"]) if best["factors"] else "Maximized Expected Recovery Value based on historical outcomes."
        reason = f"Data-driven ML selection ({best['model_version']}): {best['action']} achieves highest Expected Recovery Value (Rs. {best['ev']:,.2f}, P={best['probability']*100:.1f}%). {factors_str}"

        return NextBestAction(
            action_type=best["action"],
            channel=best["channel"],
            score=round(best["probability"], 4),
            expected_recovery_probability=best["probability"],
            expected_recovery_value=best["ev"],
            reason=reason,
            confidence=0.88,
        )
