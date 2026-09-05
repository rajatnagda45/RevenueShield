"""Independent Policy & Compliance Validation Engine.

Enforces hard safety boundaries, quiet hours, attempt caps, and regulatory rules.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from app.decision.base import ActionType, DecisionContext


@dataclass
class PolicyEvaluationResult:
    """Outcome of policy compliance validation."""

    allowed: bool
    reason: str
    blocking_rule: Optional[str] = None
    evaluated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PolicyEngine:
    """Evaluates proposed recovery actions against safety, compliance, and stopping policies."""

    version: str = "policy_engine_v1"

    @classmethod
    def evaluate(
        cls,
        action_type: str,
        context: DecisionContext,
        case_status: str,
        active_interventions_count: int = 0,
    ) -> PolicyEvaluationResult:
        """Evaluate if proposed action is permitted under current recovery policies.

        Args:
            action_type: Proposed ActionType (e.g. RETRY_PAYMENT, VOICE_OUTREACH).
            context: The DecisionContext for the case.
            case_status: Current status of RecoveryCase (e.g. OPEN, IN_PROGRESS, RECOVERED, CLOSED).
            active_interventions_count: Count of existing active interventions.

        Returns:
            PolicyEvaluationResult (allowed=True/False, blocking_rule, reason).
        """
        eval_time_str = datetime.now(timezone.utc).isoformat()

        # RULE 1 & 5: Case is already Recovered or Closed
        if case_status in ["RECOVERED", "CLOSED", "RESOLVED"]:
            return PolicyEvaluationResult(
                allowed=False,
                reason=f"Recovery case is already {case_status}. No further recovery actions permitted.",
                blocking_rule="CASE_ALREADY_RECOVERED_OR_CLOSED",
                evaluated_at=eval_time_str,
            )

        # RULE 0 (measurement): HOLDOUT arm cases are observed, never contacted or retried.
        experiment_arm = (context.metadata or {}).get("experiment_arm")
        if experiment_arm == "HOLDOUT" and action_type not in [ActionType.WAIT, ActionType.NO_ACTION]:
            return PolicyEvaluationResult(
                allowed=False,
                reason=(
                    "Case is in the experiment HOLDOUT arm. It is observed to measure incremental recovery "
                    "and receives no outreach, retries, or payment links."
                ),
                blocking_rule="HOLDOUT_ARM_OBSERVE_ONLY",
                evaluated_at=eval_time_str,
            )

        # RULE 0b (surfaces): each leak surface has a bounded recovery profile.
        surface_name = (context.metadata or {}).get("leak_surface")
        if surface_name:
            from app.domain.surfaces import profile_for
            profile = profile_for(surface_name)
            outreach_types = [
                ActionType.RETRY_PAYMENT, ActionType.SEND_PAYMENT_LINK,
                ActionType.SEND_WHATSAPP_REMINDER, ActionType.VOICE_OUTREACH,
            ]
            if action_type == ActionType.RETRY_PAYMENT and not profile.retry_allowed:
                return PolicyEvaluationResult(
                    allowed=False,
                    reason=f"{profile.surface.value}: there is no failed instrument to retry; the customer must act.",
                    blocking_rule="SURFACE_RETRY_NOT_APPLICABLE",
                    evaluated_at=eval_time_str,
                )
            if action_type == ActionType.VOICE_OUTREACH and not profile.voice_allowed:
                return PolicyEvaluationResult(
                    allowed=False,
                    reason=f"{profile.surface.value}: voice outreach is disproportionate for this surface.",
                    blocking_rule="SURFACE_VOICE_NOT_ALLOWED",
                    evaluated_at=eval_time_str,
                )
            if action_type == ActionType.ESCALATE and not profile.escalation_allowed:
                return PolicyEvaluationResult(
                    allowed=False,
                    reason=f"{profile.surface.value}: human escalation is not part of this surface's playbook.",
                    blocking_rule="SURFACE_ESCALATION_NOT_ALLOWED",
                    evaluated_at=eval_time_str,
                )
            if action_type in outreach_types:
                touches = sum(1 for a in context.previous_action_types if a in [t.value for t in outreach_types])
                if touches >= profile.max_touches:
                    return PolicyEvaluationResult(
                        allowed=False,
                        reason=f"{profile.surface.value}: contact cap of {profile.max_touches} touches reached ({touches} made).",
                        blocking_rule="SURFACE_TOUCH_CAP_REACHED",
                        evaluated_at=eval_time_str,
                    )

        # RULE 0c (portfolio): systemic issuer degradation holds retries and outreach for affected cases.
        if (context.metadata or {}).get("systemic_incident_active") and action_type in [
            ActionType.RETRY_PAYMENT, ActionType.SEND_PAYMENT_LINK,
            ActionType.SEND_WHATSAPP_REMINDER, ActionType.VOICE_OUTREACH,
        ]:
            return PolicyEvaluationResult(
                allowed=False,
                reason="An issuer/PSP degradation incident is active for this bank and method; retries and outreach are held until it clears.",
                blocking_rule="ISSUER_DEGRADED_HOLD",
                evaluated_at=eval_time_str,
            )

        # RULE 4: Active Promise-to-Pay pauses routine outreach
        if context.promise_to_pay_active and action_type in [
            ActionType.RETRY_PAYMENT,
            ActionType.SEND_PAYMENT_LINK,
            ActionType.SEND_WHATSAPP_REMINDER,
            ActionType.VOICE_OUTREACH,
        ]:
            return PolicyEvaluationResult(
                allowed=False,
                reason="Customer has an active Promise-to-Pay agreement. Routine automated outreach is paused.",
                blocking_rule="PROMISE_TO_PAY_ACTIVE",
                evaluated_at=eval_time_str,
            )

        # RULE 2 & 7: Maximum recovery attempts capped at 3
        if action_type in [
            ActionType.RETRY_PAYMENT,
            ActionType.SEND_PAYMENT_LINK,
            ActionType.SEND_WHATSAPP_REMINDER,
            ActionType.VOICE_OUTREACH,
        ]:
            retry_attempts = context.previous_action_types.count(action_type) + context.retry_count
            if retry_attempts >= 3:
                return PolicyEvaluationResult(
                    allowed=False,
                    reason=f"Maximum recovery attempt limit (3) exceeded. Total attempts: {retry_attempts}.",
                    blocking_rule="MAX_RETRIES_EXCEEDED",
                    evaluated_at=eval_time_str,
                )

        # RULE 3: Recovery-call window (RBI Fair Practices Code: 08:00-19:00 customer-local)
        if action_type == ActionType.VOICE_OUTREACH:
            from app.core.config import settings as _settings
            call_start = _settings.CONTACT_VOICE_START_HOUR
            call_end = _settings.CONTACT_VOICE_END_HOUR
            tz_name = (context.metadata or {}).get("timezone")
            if tz_name:
                try:
                    from zoneinfo import ZoneInfo
                    eval_hour = context.current_time.astimezone(ZoneInfo(tz_name)).hour
                except Exception:
                    eval_hour = context.current_time.hour
            else:
                eval_hour = context.current_time.hour

            if eval_hour >= call_end or eval_hour < call_start:
                return PolicyEvaluationResult(
                    allowed=False,
                    reason=(
                        f"Voice outreach is prohibited outside the recovery-call window "
                        f"({call_start:02d}:00 - {call_end:02d}:00, RBI Fair Practices Code). Current evaluation hour: {eval_hour:02d}:00."
                    ),
                    blocking_rule="QUIET_HOURS_VOICE_PROHIBITED",
                    evaluated_at=eval_time_str,
                )

        # RULE 6: Block conflicting parallel active interventions
        if active_interventions_count > 0 and action_type not in [ActionType.WAIT, ActionType.NO_ACTION]:
            return PolicyEvaluationResult(
                allowed=False,
                reason="Another recovery action is currently active for this case. Parallel intervention is blocked.",
                blocking_rule="ACTIVE_INTERVENTION_EXISTS",
                evaluated_at=eval_time_str,
            )

        # All policy checks passed
        return PolicyEvaluationResult(
            allowed=True,
            reason="Action satisfies all current recovery policies and compliance constraints.",
            blocking_rule=None,
            evaluated_at=eval_time_str,
        )
