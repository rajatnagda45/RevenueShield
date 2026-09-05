"""Deterministic Rule-Based Recovery Decision Engine (v1)."""
import logging
from typing import List
from app.decision.base import (
    ActionCandidate,
    ActionType,
    ChannelType,
    DecisionContext,
    RecommendationResult,
    RecoveryDecisionEngine,
)
from app.diagnosis.rules import RootCauseCategory

logger = logging.getLogger(__name__)


class RuleBasedRecoveryDecisionEngine(RecoveryDecisionEngine):
    """Deterministic, explainable next-best-action decision engine (v1)."""

    version: str = "decision_engine_v1"

    def recommend(self, context: DecisionContext) -> RecommendationResult:
        """Evaluate situation context and produce scored candidate action recommendations."""
        logger.info(
            f"[DECISION_STARTED] Evaluating Next Best Action for Case {context.case_id} "
            f"(Category={context.diagnosis_category}, RiskScore={context.risk_score}, "
            f"RecoveryProb={context.recovery_probability}, Amount={context.amount_at_risk})"
        )

        candidates: List[ActionCandidate] = []

        # 1. Score Candidate: RETRY_PAYMENT
        score_retry, conf_retry, reason_retry, factors_retry = self._score_retry(context)
        candidates.append(
            ActionCandidate(
                action_type=ActionType.RETRY_PAYMENT,
                channel=ChannelType.GATEWAY,
                score=score_retry,
                confidence=conf_retry,
                reason=reason_retry,
                supporting_factors=factors_retry,
            )
        )

        # 2. Score Candidate: SEND_PAYMENT_LINK
        score_link, conf_link, reason_link, factors_link = self._score_payment_link(context)
        candidates.append(
            ActionCandidate(
                action_type=ActionType.SEND_PAYMENT_LINK,
                channel=ChannelType.EMAIL if context.customer_email_available else ChannelType.SMS,
                score=score_link,
                confidence=conf_link,
                reason=reason_link,
                supporting_factors=factors_link,
            )
        )

        # 3. Score Candidate: SEND_WHATSAPP_REMINDER
        score_wa, conf_wa, reason_wa, factors_wa = self._score_whatsapp(context)
        candidates.append(
            ActionCandidate(
                action_type=ActionType.SEND_WHATSAPP_REMINDER,
                channel=ChannelType.WHATSAPP,
                score=score_wa,
                confidence=conf_wa,
                reason=reason_wa,
                supporting_factors=factors_wa,
            )
        )

        # 4. Score Candidate: VOICE_OUTREACH
        score_voice, conf_voice, reason_voice, factors_voice = self._score_voice(context)
        candidates.append(
            ActionCandidate(
                action_type=ActionType.VOICE_OUTREACH,
                channel=ChannelType.VOICE,
                score=score_voice,
                confidence=conf_voice,
                reason=reason_voice,
                supporting_factors=factors_voice,
            )
        )

        # 5. Score Candidate: WAIT
        score_wait, conf_wait, reason_wait, factors_wait = self._score_wait(context)
        candidates.append(
            ActionCandidate(
                action_type=ActionType.WAIT,
                channel=ChannelType.SYSTEM,
                score=score_wait,
                confidence=conf_wait,
                reason=reason_wait,
                supporting_factors=factors_wait,
            )
        )

        # 6. Score Candidate: ESCALATE
        score_esc, conf_esc, reason_esc, factors_esc = self._score_escalate(context)
        candidates.append(
            ActionCandidate(
                action_type=ActionType.ESCALATE,
                channel=ChannelType.MANUAL,
                score=score_esc,
                confidence=conf_esc,
                reason=reason_esc,
                supporting_factors=factors_esc,
            )
        )

        # 7. Score Candidate: NO_ACTION
        score_none, conf_none, reason_none, factors_none = self._score_no_action(context)
        candidates.append(
            ActionCandidate(
                action_type=ActionType.NO_ACTION,
                channel=ChannelType.SYSTEM,
                score=score_none,
                confidence=conf_none,
                reason=reason_none,
                supporting_factors=factors_none,
            )
        )

        # Sort candidates descending by decision score
        candidates.sort(key=lambda c: c.score, reverse=True)

        winner = candidates[0]
        alternatives = [c.to_dict() for c in candidates[1:4]]

        logger.info(
            f"[DECISION_COMPLETED] Case {context.case_id} Recommended Action: {winner.action_type} "
            f"(Score={winner.score}, Confidence={winner.confidence})"
        )

        return RecommendationResult(
            recommended_action=winner.action_type,
            channel=winner.channel,
            score=winner.score,
            confidence=winner.confidence,
            reason=winner.reason,
            supporting_factors=winner.supporting_factors,
            alternatives=alternatives,
            decision_engine_version=self.version,
        )

    # ---------------- Scorer Implementations ----------------

    def _score_retry(self, ctx: DecisionContext) -> tuple[float, float, str, List[str]]:
        factors = [
            f"recovery_probability={ctx.recovery_probability}",
            f"diagnosis_category={ctx.diagnosis_category}",
        ]
        surface = (ctx.metadata or {}).get("leak_surface")
        # Surfaces with no failed instrument (abandoned checkout, overdue invoice) cannot be "retried".
        if surface in ("CHECKOUT_ABANDONMENT", "RECEIVABLE_OVERDUE") or ctx.diagnosis_category in [
            RootCauseCategory.RECEIVABLE_OVERDUE, RootCauseCategory.MANDATE_REVOKED_OR_EXPIRED,
        ]:
            factors.append(f"leak_surface={surface}")
            return 0.02, 0.90, "No payment instrument to retry on this surface; customer must act.", factors

        # Systemic issuer degradation: the right move is a deferred retry once the issuer recovers.
        if ctx.diagnosis_category == RootCauseCategory.SYSTEMIC_ISSUER_DEGRADATION:
            factors.append("systemic_incident=true")
            return 0.90, 0.88, "Issuer/PSP degradation detected; schedule a deferred gateway retry, do not contact the customer.", factors

        # RETRY is prime for transient technical bank failures or temporary balance issues
        if ctx.diagnosis_category in [RootCauseCategory.BANK_TECHNICAL_FAILURE, RootCauseCategory.INSUFFICIENT_FUNDS]:
            base = 0.88 if ctx.diagnosis_category == RootCauseCategory.BANK_TECHNICAL_FAILURE else 0.76
            conf = 0.85
            reason = "Transient bank or balance failure suitable for automated gateway retry."
        else:
            base = 0.20
            conf = 0.50
            reason = "Failure is unlikely to resolve via simple payment retry."

        if ctx.customer_features and ctx.customer_features.success_rate >= 0.80:
            base += 0.05
            factors.append(f"customer_success_rate={ctx.customer_features.success_rate}")

        retry_count = ctx.previous_action_types.count(ActionType.RETRY_PAYMENT)
        if retry_count > 0:
            base -= (retry_count * 0.25)
            factors.append(f"previous_retries={retry_count}")

        return round(min(1.0, max(0.0, base)), 4), round(conf, 4), reason, factors

    def _score_payment_link(self, ctx: DecisionContext) -> tuple[float, float, str, List[str]]:
        factors = [
            f"diagnosis_category={ctx.diagnosis_category}",
            f"has_email={ctx.customer_email_available}",
            f"has_phone={ctx.customer_phone_available}",
        ]
        # Payment links excel for user-actionable failures (OTP, expired card, user friction, bank decline)
        actionable_categories = [
            RootCauseCategory.AUTHENTICATION_FAILURE,
            RootCauseCategory.PAYMENT_METHOD_FAILURE,
            RootCauseCategory.USER_FRICTION,
            RootCauseCategory.BANK_DECLINE,
            RootCauseCategory.RECEIVABLE_OVERDUE,
            RootCauseCategory.MANDATE_REVOKED_OR_EXPIRED,
        ]
        if ctx.diagnosis_category == RootCauseCategory.SYSTEMIC_ISSUER_DEGRADATION:
            factors.append("systemic_incident=true")
            return 0.05, 0.90, "Customer is not at fault during an issuer outage; no payment link is sent.", factors
        if ctx.diagnosis_category in actionable_categories:
            base = 0.85
            conf = 0.82
            reason = "Customer-side payment issue resolvable by prompting customer with hosted payment link."
        elif ctx.diagnosis_category == RootCauseCategory.INSUFFICIENT_FUNDS:
            base = 0.72
            conf = 0.78
            reason = "Payment link allows customer to complete payment from alternate account or payment method."
        else:
            base = 0.35
            conf = 0.60
            reason = "Payment link is a secondary recovery option."

        if ctx.customer_email_available or ctx.customer_phone_available:
            base += 0.05
        else:
            base -= 0.30
            factors.append("missing_contact_info=true")

        # High-ticket transactions (> 50k) are better served by dedicated voice/account outreach
        if float(ctx.amount_at_risk) >= 50000.0:
            base -= 0.15
            factors.append("high_ticket_size=true")

        return round(min(1.0, max(0.0, base)), 4), round(conf, 4), reason, factors

    def _score_whatsapp(self, ctx: DecisionContext) -> tuple[float, float, str, List[str]]:
        factors = [
            f"diagnosis_category={ctx.diagnosis_category}",
            f"has_phone={ctx.customer_phone_available}",
        ]
        # WhatsApp reminder is strong for retail / user friction & OTP failure when phone available
        if ctx.customer_phone_available and ctx.diagnosis_category in [
            RootCauseCategory.USER_FRICTION,
            RootCauseCategory.AUTHENTICATION_FAILURE,
            RootCauseCategory.INSUFFICIENT_FUNDS,
        ]:
            base = 0.80
            conf = 0.80
            reason = "Interactive WhatsApp reminder is optimal for quick customer re-engagement."
        else:
            base = 0.30 if ctx.customer_phone_available else 0.10
            conf = 0.65
            reason = "WhatsApp reminder has lower affinity for this failure category or lacks phone number."

        return round(min(1.0, max(0.0, base)), 4), round(conf, 4), reason, factors

    def _score_voice(self, ctx: DecisionContext) -> tuple[float, float, str, List[str]]:
        factors = [
            f"amount_at_risk={float(ctx.amount_at_risk)}",
            f"risk_score={ctx.risk_score}",
        ]
        amt = float(ctx.amount_at_risk)
        surface = (ctx.metadata or {}).get("leak_surface")
        if surface == "CHECKOUT_ABANDONMENT" or ctx.diagnosis_category == RootCauseCategory.SYSTEMIC_ISSUER_DEGRADATION:
            factors.append(f"leak_surface={surface}")
            return 0.02, 0.90, "Voice outreach is disproportionate for an abandoned checkout or an issuer outage.", factors
        # Voice outreach is prioritized for high-value / enterprise cases (> ₹50,000) or high risk
        if amt >= 50000.0 and ctx.customer_phone_available:
            base = 0.94
            conf = 0.90
            reason = f"High-value revenue case ({ctx.currency} {amt:,.2f}) warrants direct voice outreach."
            factors.append("high_ticket_account=true")
        elif amt >= 10000.0 and ctx.customer_phone_available and ctx.risk_score >= 60.0:
            base = 0.70
            conf = 0.75
            reason = "Elevated risk case warrants personalized voice follow-up."
        else:
            base = 0.20
            conf = 0.50
            reason = "Low/standard ticket size does not warrant immediate voice outreach."

        return round(min(1.0, max(0.0, base)), 4), round(conf, 4), reason, factors

    def _score_wait(self, ctx: DecisionContext) -> tuple[float, float, str, List[str]]:
        factors = [f"diagnosis_confidence={ctx.diagnosis_confidence}"]
        # WAIT is preferred if diagnosis confidence is low or recent payment attempt is very fresh
        if ctx.diagnosis_confidence < 0.50:
            base = 0.75
            conf = 0.70
            reason = "Low diagnostic confidence; waiting for additional telemetry or settlement signals."
        elif ctx.case_age_hours < 1.0 and ctx.diagnosis_category == RootCauseCategory.BANK_TECHNICAL_FAILURE:
            base = 0.65
            conf = 0.70
            reason = "Holding briefly to allow bank gateway switches to recover before initiating contact."
        else:
            base = 0.25
            conf = 0.60
            reason = "Immediate recovery intervention is preferred over waiting."

        return round(min(1.0, max(0.0, base)), 4), round(conf, 4), reason, factors

    def _score_escalate(self, ctx: DecisionContext) -> tuple[float, float, str, List[str]]:
        factors = [
            f"risk_score={ctx.risk_score}",
            f"diagnosis_category={ctx.diagnosis_category}",
        ]
        # ESCALATE is prime for fraud/security, merchant config issues, or extreme risk
        if ctx.diagnosis_category in [
            RootCauseCategory.POSSIBLE_FRAUD_OR_SECURITY,
            RootCauseCategory.MERCHANT_CONFIGURATION,
        ]:
            base = 0.92
            conf = 0.90
            reason = f"Critical risk or operational error ({ctx.diagnosis_category}) requires human specialist review."
        elif ctx.risk_score >= 80.0:
            base = 0.78
            conf = 0.80
            reason = f"High revenue risk score ({ctx.risk_score}) requires account manager escalation."
        elif (
            ctx.diagnosis_category == RootCauseCategory.RECEIVABLE_OVERDUE
            and float(ctx.amount_at_risk) >= 100000.0
            and len(ctx.previous_action_types) >= 2
        ):
            base = 0.80
            conf = 0.80
            reason = "Large overdue receivable with prior reminders exhausted; escalate to merchant finance / collections."
            factors.append("b2b_collections_escalation=true")
        elif ctx.previous_action_outcomes.count("FAILED") >= 2:
            base = 0.75
            conf = 0.75
            reason = "Multiple automated recovery attempts failed; escalating to manual review."
            factors.append("multiple_failed_interventions=true")
        else:
            base = 0.15
            conf = 0.50
            reason = "Case is safely addressable via automated workflows."

        return round(min(1.0, max(0.0, base)), 4), round(conf, 4), reason, factors

    def _score_no_action(self, ctx: DecisionContext) -> tuple[float, float, str, List[str]]:
        factors = [f"recovery_probability={ctx.recovery_probability}"]
        if ctx.recovery_probability < 0.10:
            base = 0.80
            conf = 0.85
            reason = "Recovery probability is negligible; suppressing automated outreach."
        else:
            base = 0.05
            conf = 0.90
            reason = "Case has viable recovery paths; suppressing action is not indicated."

        return round(min(1.0, max(0.0, base)), 4), round(conf, 4), reason, factors
