"""NextBestActionEngine coordinating context gathering, policy evaluation, and action scoring."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.diagnosis import Diagnosis
from app.models.prediction import Prediction
from app.models.recovery_case import RecoveryCase
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.services.action_policy import ActionPolicy, ActionPolicyContext, MLActionPolicy, NextBestAction, RuleBasedActionPolicy
from app.ml.action_prediction_service import RecoveryActionPredictionService
from app.ml.registry import ModelRegistryService

logger = logging.getLogger(__name__)


class NextBestActionEngine:
    """Engine calculating the optimal next action and expected recovery value for an active recovery plan."""

    @classmethod
    def compute_next_best_action(
        cls,
        db: Session,
        case: RecoveryCase,
        reference_time: Optional[datetime] = None,
        action_policy: Optional[ActionPolicy] = None,
        force_engagement_signal: Optional[bool] = None,
    ) -> NextBestAction:
        """Evaluate case telemetry and compute next best action with Expected Recovery Value."""
        now = reference_time or datetime.now(timezone.utc)

        # 1. Fetch Baseline ML Probability or Diagnosis
        base_prob = 0.45
        latest_pred = db.scalar(
            select(Prediction)
            .where(Prediction.recovery_case_id == case.id)
            .order_by(Prediction.created_at.desc())
        )
        if latest_pred and latest_pred.predicted_probability is not None:
            base_prob = float(latest_pred.predicted_probability)
        else:
            latest_diag = db.scalar(
                select(Diagnosis)
                .where(Diagnosis.recovery_case_id == case.id)
                .order_by(Diagnosis.created_at.desc())
            )
            if latest_diag and latest_diag.recovery_probability is not None:
                base_prob = float(latest_diag.recovery_probability)

        # 2. Customer Context & Channel Availability
        customer = case.customer or (db.scalar(select(Customer).where(Customer.id == case.customer_id)) if case.customer_id else None)
        email_available = bool(customer and customer.email and "@" in customer.email)
        phone_available = bool(customer and customer.phone)

        # 3. Determine Attempt Number & Previous Steps
        previous_steps: List[Dict[str, Any]] = []
        if case.recovery_plan and case.recovery_plan.steps:
            for step in case.recovery_plan.steps:
                previous_steps.append({
                    "step_number": step.step_number,
                    "action_type": step.action_type,
                    "channel": step.channel,
                    "status": step.status,
                    "completed_at": step.completed_at.isoformat() if step.completed_at else None,
                })
        attempt_number = len(previous_steps) + 1

        # 4. Check for Payment Link Telemetry / Engagement Signals
        has_interaction = False
        if force_engagement_signal is not None:
            has_interaction = force_engagement_signal
        else:
            active_links = db.scalars(
                select(RecoveryPaymentLink).where(RecoveryPaymentLink.recovery_case_id == case.id)
            ).all()
            for link in active_links:
                if link.status in ["OPENED", "CLICKED", "PAID"]:
                    has_interaction = True
                    break

        # Calculate time elapsed since case creation
        created_at_dt = case.created_at
        if created_at_dt.tzinfo is None:
            created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
        hours_elapsed = max((now - created_at_dt).total_seconds() / 3600.0, 0.0)

        # 5. Build ActionPolicyContext
        context = ActionPolicyContext(
            recovery_case_id=str(case.id),
            amount_at_risk=case.amount_at_risk or Decimal("0.00"),
            attempt_number=attempt_number,
            previous_steps=previous_steps,
            previous_communications=[],
            customer_preferences={
                "transactional_allowed": customer.transactional_allowed if customer else True,
                "marketing_opt_out": customer.marketing_opt_out if customer else False,
            },
            hours_since_failure=hours_elapsed,
            has_payment_link_interaction=has_interaction,
            ml_base_probability=base_prob,
            customer_email_available=email_available,
            customer_phone_available=phone_available,
        )

        # 6. Check Active ML Model in Registry
        active_model = ModelRegistryService.get_active_model(db, model_name="action_recovery_model")
        
        if action_policy is not None:
            policy_evaluator = action_policy
        elif active_model is not None:
            # Active ML model exists -> Score candidate actions
            candidates = cls.evaluate_candidate_actions(db=db, case=case)
            policy_evaluator = MLActionPolicy()
            nba = policy_evaluator.select_action(context, candidate_predictions=candidates)
            logger.info(
                f"[NEXT_BEST_ACTION_ML] Case={case.id} Model={active_model.version} Action={nba.action_type} EV={nba.expected_recovery_value} Prob={nba.expected_recovery_probability}"
            )
            return nba
        else:
            policy_evaluator = RuleBasedActionPolicy()

        # 7. Fallback Evaluation
        nba = policy_evaluator.select_action(context)
        logger.info(
            f"[NEXT_BEST_ACTION_RULE] Case={case.id} Attempt={attempt_number} Action={nba.action_type} EV={nba.expected_recovery_value} Prob={nba.expected_recovery_probability}"
        )
        return nba

    @classmethod
    def evaluate_candidate_actions(
        cls,
        db: Session,
        case: RecoveryCase,
    ) -> List[Dict[str, Any]]:
        """Compute ML predictions and expected recovery values for all candidate actions."""
        candidate_action_types = [
            "EMAIL_PAYMENT_RECOVERY",
            "EMAIL_FOLLOWUP",
            "WHATSAPP_PAYMENT_RECOVERY",
            "SEND_PAYMENT_LINK",
        ]

        results = []
        for act in candidate_action_types:
            outcome = RecoveryActionPredictionService.predict_action(
                db=db,
                case=case,
                action_type=act,
                persist=False,
            )
            amount = float(case.amount_at_risk or 0.0)
            ev = round(outcome.probability * amount, 2)
            results.append({
                "action": act,
                "probability": outcome.probability,
                "expected_recovery_value": ev,
                "contributing_factors": outcome.contributing_factors,
                "model_version": outcome.model_version,
                "model_status": outcome.model_status,
            })

        # Sort descending by Expected Recovery Value
        results.sort(key=lambda x: x["expected_recovery_value"], reverse=True)
        return results
