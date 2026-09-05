"""Deterministic rule matching and root cause categorization rules.

Maps combinations of gateway error fields to a controlled vocabulary of root causes.
"""
from dataclasses import dataclass
from typing import Optional


class RootCauseCategory:
    """Controlled vocabulary of failure root causes."""

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    BANK_DECLINE = "BANK_DECLINE"
    BANK_TECHNICAL_FAILURE = "BANK_TECHNICAL_FAILURE"
    AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
    PAYMENT_METHOD_FAILURE = "PAYMENT_METHOD_FAILURE"
    USER_FRICTION = "USER_FRICTION"
    MERCHANT_CONFIGURATION = "MERCHANT_CONFIGURATION"
    POSSIBLE_FRAUD_OR_SECURITY = "POSSIBLE_FRAUD_OR_SECURITY"
    # v2 surfaces / portfolio-level causes
    RECEIVABLE_OVERDUE = "RECEIVABLE_OVERDUE"                    # B2B invoice past due; no instrument failed
    MANDATE_REVOKED_OR_EXPIRED = "MANDATE_REVOKED_OR_EXPIRED"    # recurring mandate cancelled/expired at the bank
    SYSTEMIC_ISSUER_DEGRADATION = "SYSTEMIC_ISSUER_DEGRADATION"  # bank/PSP outage detected across the portfolio
    UNKNOWN = "UNKNOWN"

    ALL = {
        RECEIVABLE_OVERDUE,
        MANDATE_REVOKED_OR_EXPIRED,
        SYSTEMIC_ISSUER_DEGRADATION,
        INSUFFICIENT_FUNDS,
        BANK_DECLINE,
        BANK_TECHNICAL_FAILURE,
        AUTHENTICATION_FAILURE,
        PAYMENT_METHOD_FAILURE,
        USER_FRICTION,
        MERCHANT_CONFIGURATION,
        POSSIBLE_FRAUD_OR_SECURITY,
        UNKNOWN,
    }


@dataclass
class RuleMatchResult:
    category: str
    explanation: str
    base_confidence: float
    matched_rule: str


class RuleEngine:
    """Evaluates error signals against prioritized rule definitions."""

    @classmethod
    def evaluate(
        cls,
        error_source: Optional[str],
        error_step: Optional[str],
        error_reason: Optional[str],
        error_code: Optional[str],
        failure_description: Optional[str],
        payment_method: Optional[str],
    ) -> RuleMatchResult:
        """Evaluate failure signals and return matched category, explanation, and confidence."""
        # Normalize tokens for robust matching
        reason = (error_reason or "").lower().strip()
        code = (error_code or "").lower().strip()
        desc = (failure_description or "").lower().strip()
        source = (error_source or "").lower().strip()
        step = (error_step or "").lower().strip()
        method = (payment_method or "").upper().strip()

        combined_text = f"{reason} {code} {desc}".strip()

        # Rule 0a (v2 surface): Overdue receivable - nothing failed technically, the buyer has not paid.
        if "receivable_overdue" in combined_text or "invoice_overdue" in combined_text or "invoice_expired" in combined_text:
            return RuleMatchResult(
                category=RootCauseCategory.RECEIVABLE_OVERDUE,
                explanation=(
                    "Invoice is past its due date with no technical payment failure. Recovery is a collections "
                    "conversation: reminder, statement with payment link, promise-to-pay, then escalation."
                ),
                base_confidence=0.95,
                matched_rule="rule_receivable_overdue",
            )

        # Rule 0b (v2 surface): Mandate revoked / expired / paused at the bank or by the customer.
        if any(kw in combined_text for kw in ["mandate_revoked", "mandate revoked", "mandate_cancelled", "mandate_expired", "mandate expired", "mandate_paused", "token_expired", "autopay revoked", "emandate_cancelled"]):
            return RuleMatchResult(
                category=RootCauseCategory.MANDATE_REVOKED_OR_EXPIRED,
                explanation=(
                    "The recurring payment mandate is no longer valid (revoked, paused or expired). Automated retries "
                    "cannot succeed; the customer must re-authorise via a fresh authorisation link."
                ),
                base_confidence=0.93,
                matched_rule="rule_mandate_revoked_or_expired",
            )

        # Rule 0c (v2 portfolio): systemic issuer degradation tagged by the degradation monitor.
        if "systemic_issuer_degradation" in combined_text:
            return RuleMatchResult(
                category=RootCauseCategory.SYSTEMIC_ISSUER_DEGRADATION,
                explanation=(
                    "Failure coincides with an active issuer/PSP degradation incident affecting many customers. "
                    "Customer outreach is withheld; the payment is re-attempted once the issuer recovers."
                ),
                base_confidence=0.90,
                matched_rule="rule_systemic_issuer_degradation",
            )

        # Rule 1: Insufficient Funds
        if any(kw in combined_text for kw in ["insufficient_funds", "insufficient balance", "insufficient_balance", "low_balance", "balance"]):
            return RuleMatchResult(
                category=RootCauseCategory.INSUFFICIENT_FUNDS,
                explanation=(
                    "Payment failed due to insufficient customer account balance. "
                    "The transaction is eligible for timed smart retries or payment reminder."
                ),
                base_confidence=0.92 if reason == "insufficient_funds" else 0.85,
                matched_rule="rule_insufficient_funds",
            )

        # Rule 2: Authentication / OTP Failure
        if any(kw in combined_text for kw in ["otp", "3d_secure", "authentication_failed", "auth_failed", "incorrect_otp", "pin", "mpin", "wrong_pin"]):
            return RuleMatchResult(
                category=RootCauseCategory.AUTHENTICATION_FAILURE,
                explanation=(
                    "Customer failed two-factor or 3DS authentication during authorization (e.g. incorrect OTP or expired session). "
                    "Prompting customer with a direct payment link typically recovers this failure."
                ),
                base_confidence=0.90 if "otp" in reason or "auth" in reason else 0.82,
                matched_rule="rule_authentication_failure",
            )

        # Rule 3: User Friction / Cancellation / Drop-off
        if any(kw in combined_text for kw in ["user_cancelled", "cancelled by user", "payment_cancelled", "user_dropped", "session_expired", "timeout_by_user"]):
            return RuleMatchResult(
                category=RootCauseCategory.USER_FRICTION,
                explanation=(
                    "Customer cancelled the checkout or abandoned the verification flow before completion."
                ),
                base_confidence=0.88,
                matched_rule="rule_user_friction",
            )

        # Rule 4: Bank Technical Failure / Outage
        if (
            source == "bank" and ("downtime" in combined_text or "timeout" in combined_text or "unavailable" in combined_text or "internal" in combined_text)
            or any(kw in combined_text for kw in ["bank_offline", "gateway_timeout", "bank_timeout", "bank_error", "internal_server_error", "bank downtime", "switch_down"])
        ):
            return RuleMatchResult(
                category=RootCauseCategory.BANK_TECHNICAL_FAILURE,
                explanation=(
                    "Transaction failed due to a temporary issuing bank or payment switch network outage. "
                    "Expected to recover upon scheduled retry once network health restores."
                ),
                base_confidence=0.90 if source == "bank" else 0.82,
                matched_rule="rule_bank_technical_failure",
            )

        # Rule 5: Fraud / Risk / Security Block
        if any(kw in combined_text for kw in ["fraud", "risk_threshold", "stolen_card", "lost_card", "security_violation", "restricted_card", "pickup_card"]):
            return RuleMatchResult(
                category=RootCauseCategory.POSSIBLE_FRAUD_OR_SECURITY,
                explanation=(
                    "Transaction was flagged or blocked by issuing bank risk or fraud screening filters."
                ),
                base_confidence=0.88,
                matched_rule="rule_fraud_or_security",
            )

        # Rule 6: Merchant / Gateway Configuration
        if (
            source in ["business", "gateway"] and any(kw in combined_text for kw in ["bad_request", "invalid_api_key", "currency_not_supported", "account_inactive", "invalid_merchant", "mid_disabled"])
            or "configuration_error" in combined_text
        ):
            return RuleMatchResult(
                category=RootCauseCategory.MERCHANT_CONFIGURATION,
                explanation=(
                    "Transaction failed due to merchant account or gateway configuration settings (e.g. currency support or account status)."
                ),
                base_confidence=0.85,
                matched_rule="rule_merchant_configuration",
            )

        # Rule 7: Payment Method Failure (Expired card, invalid card number, UPI ID invalid)
        if any(kw in combined_text for kw in ["expired_card", "card_expired", "invalid_card", "card_inactive", "invalid_vpa", "vpa_not_found", "card_limit_exceeded"]):
            return RuleMatchResult(
                category=RootCauseCategory.PAYMENT_METHOD_FAILURE,
                explanation=(
                    "The selected payment instrument is expired, deactivated, or invalid. "
                    "Customer must provide an updated payment method."
                ),
                base_confidence=0.89,
                matched_rule="rule_payment_method_failure",
            )

        # Rule 8: Generic Bank Decline
        if source == "bank" or any(kw in combined_text for kw in ["do_not_honor", "generic_decline", "declined_by_bank", "transaction_not_permitted", "decline"]):
            return RuleMatchResult(
                category=RootCauseCategory.BANK_DECLINE,
                explanation=(
                    "Issuing bank declined the transaction authorization without a specific sub-code. "
                    "Usually associated with bank card controls, international transaction limits, or temporary limits."
                ),
                base_confidence=0.75 if source == "bank" else 0.65,
                matched_rule="rule_bank_decline",
            )

        # Fallback: Unknown / Insufficient Evidence
        has_some_info = bool(reason or code or desc)
        return RuleMatchResult(
            category=RootCauseCategory.UNKNOWN,
            explanation=(
                "Failure information from gateway was unclassified or incomplete. Further observation or customer contact required."
                if has_some_info
                else "No diagnostic error codes or descriptions were provided by the payment gateway."
            ),
            base_confidence=0.40 if has_some_info else 0.20,
            matched_rule="rule_unknown_fallback",
        )
