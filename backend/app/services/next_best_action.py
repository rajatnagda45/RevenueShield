"""Next-Best-Action (NBA) and Expected Recovered Value Scoring Service."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Union
import uuid
from sqlalchemy.orm import Session

from app.core.config import settings
from app.decision.base import ActionType, DecisionContext
from app.decision.policy import PolicyEngine
from app.models.recovery_case import RecoveryCase
from app.models.customer import Customer
from app.models.promise_to_pay import PromiseToPay
from app.models.audit_log import AuditLog
from app.ml.dataset_builder import RecoveryMLDatasetBuilder
from app.ml.expected_recovery import calculate_expected_recovered_value
from app.ml.recovery_probability_model import (
    RecoveryProbabilityModelService,
    DEFAULT_MODEL_VERSION,
)
from app.services.action_candidate_service import ActionCandidateService

logger = logging.getLogger(__name__)

# Map candidate action strings to PolicyEngine ActionType enums
ACTION_TYPE_POLICY_MAP: Dict[str, ActionType] = {
    "PAYMENT_RETRY": ActionType.RETRY_PAYMENT,
    "EMAIL": ActionType.SEND_PAYMENT_LINK,
    "VOICE": ActionType.VOICE_OUTREACH,
    "WHATSAPP": ActionType.SEND_WHATSAPP_REMINDER,
    "NO_ACTION": ActionType.NO_ACTION,
}


class NextBestActionService:
    """Evaluates multiple candidate actions, predicts recovery probability, scores ERV, and applies PolicyEngine gates."""

    @classmethod
    def _build_decision_context(
        cls,
        case: RecoveryCase,
        customer: Optional[Customer],
        active_ptp: Optional[PromiseToPay],
    ) -> DecisionContext:
        """Construct DecisionContext for PolicyEngine compliance evaluation."""
        now = datetime.now(timezone.utc)
        case_created = case.created_at
        if case_created and case_created.tzinfo is None:
            case_created = case_created.replace(tzinfo=timezone.utc)
        case_age_hours = (now - case_created).total_seconds() / 3600.0 if case_created else 0.0

        diag = case.diagnoses[0] if getattr(case, "diagnoses", None) else None
        plan = getattr(case, "plan", None) or (
            getattr(case, "plans", [None])[0] if getattr(case, "plans", None) else None
        )
        steps = plan.steps if plan and getattr(plan, "steps", None) else []
        prev_actions = [s.action_type for s in steps]

        phone_available = bool(customer and getattr(customer, "phone", None))
        email_available = bool(customer and getattr(customer, "email", None))

        return DecisionContext(
            case_id=str(case.id),
            case_type=str(getattr(case, "case_type", "INVOICE") or "INVOICE"),
            amount_at_risk=Decimal(str(case.amount_at_risk or "0.00")),
            currency=str(case.currency or "INR"),
            case_age_hours=case_age_hours,
            retry_count=int(case.retry_count or 0),
            diagnosis_category=str(getattr(diag, "category", "UNKNOWN") or "UNKNOWN"),
            diagnosis_confidence=float(getattr(diag, "confidence", 0.0) or 0.0),
            risk_score=50.0,
            recovery_probability=0.50,
            customer_phone_available=phone_available,
            customer_email_available=email_available,
            promise_to_pay_active=bool(active_ptp),
            current_time=now,
            previous_action_types=prev_actions,
            metadata={
                "experiment_arm": getattr(case, "experiment_arm", None),
                "leak_surface": getattr(case, "leak_surface", None),
                "systemic_incident_active": bool((getattr(case, "case_metadata", None) or {}).get("systemic_hold")),
                "plan_step_count": len(steps),
                "timezone": getattr(customer, "timezone", None) or settings.DEFAULT_TIMEZONE or "Asia/Kolkata",
            },
        )

    @classmethod
    def recommend_next_best_action(
        cls,
        case_id: Union[uuid.UUID, str],
        db: Session,
        model_version: Optional[str] = None,
        model_pipeline: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Recommend Next-Best-Action for a recovery case.

        Flow:
            1. Load case & customer
            2. Build pre-intervention base feature vector (invariant across actions)
            3. Generate eligible candidate actions
            4. Predict recovery probability per action
            5. Calculate Expected Recovered Value (ERV = P * amount)
            6. Evaluate PolicyEngine for each action
            7. Rank permitted actions by ERV descending
            8. Select highest permitted action (or NO_ACTION)
            9. Record AuditLog entry: NEXT_BEST_ACTION_RECOMMENDED
            10. Return decision response (recommends ONLY, does NOT execute)
        """
        cid = uuid.UUID(str(case_id)) if isinstance(case_id, str) else case_id
        case = db.query(RecoveryCase).filter(RecoveryCase.id == cid).first()
        if not case:
            raise ValueError(f"Recovery case with ID {case_id} not found.")

        customer: Optional[Customer] = getattr(case, "customer", None)
        active_ptp = (
            db.query(PromiseToPay)
            .filter(
                PromiseToPay.recovery_case_id == case.id,
                PromiseToPay.status.in_(["ACTIVE", "PENDING"]),
            )
            .first()
        )

        amount_at_risk = float(case.amount_at_risk or 0.0)
        status = str(case.status or "OPEN").upper()

        # 1. Generate Candidate Actions
        candidate_actions = ActionCandidateService.get_candidate_actions(case, db=db)

        # 2. Build Invariant Pre-Intervention Feature Vector
        now_utc = datetime.now(timezone.utc)
        plan_obj = getattr(case, "plan", None) or (
            getattr(case, "plans", [None])[0] if getattr(case, "plans", None) else None
        )
        step_num = (len(plan_obj.steps) + 1) if (plan_obj and getattr(plan_obj, "steps", None)) else 1

        base_features = RecoveryMLDatasetBuilder.extract_pre_intervention_features(
            db=db,
            case=case,
            customer=customer,
            prediction_timestamp=now_utc,
            current_step_number=step_num,
        )

        # 3. Check ML model availability
        active_version = model_version or DEFAULT_MODEL_VERSION
        pipeline = model_pipeline or RecoveryProbabilityModelService.load_model(active_version)

        decision_mode = "ML_NBA" if pipeline is not None else "RULE_BASED_COLD_START"

        # 4. Construct Decision Context for PolicyEngine
        context = cls._build_decision_context(case, customer, active_ptp)

        # 5. Score Each Candidate Action
        ranking: List[Dict[str, Any]] = []

        for action in candidate_actions:
            # Predict probability
            if action == "NO_ACTION":
                prob = 0.0
                model_ver_used = active_version if pipeline else "rule_heuristic"
            elif pipeline is not None:
                pred_res = RecoveryProbabilityModelService.predict_probability(
                    features=base_features,
                    intervention_type=action,
                    model_version=active_version,
                )
                prob = float(pred_res.get("probability", 0.5))
                model_ver_used = str(pred_res.get("model_version", active_version))
            else:
                # Rule-based cold start heuristic prior
                prob = 0.40
                model_ver_used = "RULE_BASED_COLD_START"

            # Calculate Expected Recovered Value
            erv_res = calculate_expected_recovered_value(prob, amount_at_risk)
            erv = float(erv_res["expected_recovered_value"])

            # Evaluate PolicyEngine
            policy_action_type = ACTION_TYPE_POLICY_MAP.get(action, ActionType.NO_ACTION)
            if action == "NO_ACTION":
                policy_allowed = True
                policy_reason = "NO_ACTION is always permitted by policy."
            else:
                eval_res = PolicyEngine.evaluate(
                    action_type=policy_action_type,
                    context=context,
                    case_status=status,
                    active_interventions_count=len(context.previous_action_types),
                )
                policy_allowed = eval_res.allowed
                policy_reason = eval_res.reason

            ranking.append(
                {
                    "action": action,
                    "predicted_probability": round(prob, 4),
                    "amount_at_risk": amount_at_risk,
                    "expected_recovered_value": erv,
                    "policy_allowed": policy_allowed,
                    "policy_reason": policy_reason,
                }
            )

        # 6. Rank actions: Policy allowed first, then descending by expected_recovered_value
        ranking.sort(key=lambda x: (x["policy_allowed"], x["expected_recovered_value"]), reverse=True)

        # 7. Select Recommended Action (Highest permitted action)
        permitted_actions = [a for a in ranking if a["policy_allowed"]]
        if not permitted_actions or status in ["RECOVERED", "CLOSED", "RESOLVED"] or active_ptp:
            recommended = next((a for a in ranking if a["action"] == "NO_ACTION"), ranking[0])
            reason = "Case is recovered/paused or all outreach actions are blocked by policy; NO_ACTION selected."
        else:
            # Highest expected value among permitted
            recommended = max(permitted_actions, key=lambda x: x["expected_recovered_value"])
            if recommended["action"] == "NO_ACTION" and len(permitted_actions) > 1:
                # If a positive action is permitted with positive value, select it
                positive_permitted = [a for a in permitted_actions if a["action"] != "NO_ACTION" and a["expected_recovered_value"] > 0]
                if positive_permitted:
                    recommended = max(positive_permitted, key=lambda x: x["expected_recovered_value"])
            reason = f"{recommended['action']} has the highest expected recovered value (₹{recommended['expected_recovered_value']:,.2f}) among policy-permitted actions."

        recommended_action = recommended["action"]
        best_prob = recommended["predicted_probability"]
        best_erv = recommended["expected_recovered_value"]

        # 8. Record Audit Log: NEXT_BEST_ACTION_RECOMMENDED
        try:
            audit_entry = AuditLog(
                recovery_case_id=case.id,
                actor_type="ML_NBA_ENGINE",
                actor_id=model_ver_used,
                action="NEXT_BEST_ACTION_RECOMMENDED",
                entity_type="RECOVERY_CASE",
                entity_id=str(case.id),
                audit_metadata={
                    "case_id": str(case.id),
                    "recommended_action": recommended_action,
                    "decision_mode": decision_mode,
                    "model_version": model_ver_used,
                    "predicted_probability": best_prob,
                    "expected_recovered_value": best_erv,
                    "candidate_actions": candidate_actions,
                    "ranking": ranking,
                    "timestamp": now_utc.isoformat(),
                },
            )
            db.add(audit_entry)
            db.commit()
        except Exception as e:
            logger.warning(f"[AUDIT_LOG_ERROR] Failed to record NBA audit log: {e}")
            db.rollback()

        return {
            "case_id": str(case.id),
            "decision_mode": decision_mode,
            "recommended_action": recommended_action,
            "amount_at_risk": amount_at_risk,
            "model_version": model_ver_used,
            "predicted_probability": best_prob,
            "expected_recovered_value": best_erv,
            "ranking": ranking,
            "reason": reason,
        }
