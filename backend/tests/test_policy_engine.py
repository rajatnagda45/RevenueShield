"""Unit tests for the PolicyEngine compliance and safety rules."""
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.decision.base import ActionType, DecisionContext
from app.decision.policy import PolicyEngine


@pytest.fixture
def base_context():
    return DecisionContext(
        case_id="case_policy_01",
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("1500.00"),
        currency="INR",
        case_age_hours=2.0,
        retry_count=0,
        diagnosis_category="INSUFFICIENT_FUNDS",
        diagnosis_confidence=0.90,
        risk_score=40.0,
        recovery_probability=0.80,
        customer_phone_available=True,
        customer_email_available=True,
        promise_to_pay_active=False,
        current_time=datetime(2026, 8, 22, 14, 0, 0, tzinfo=timezone.utc),  # 14:00 (afternoon)
        previous_action_types=[],
        previous_action_outcomes=[],
    )


def test_rule_1_and_5_block_when_case_already_recovered(base_context: DecisionContext):
    """RULE 1 & 5: Ensure no actions are allowed on RECOVERED or CLOSED cases."""
    res_rec = PolicyEngine.evaluate(
        action_type=ActionType.SEND_PAYMENT_LINK,
        context=base_context,
        case_status="RECOVERED",
    )
    assert res_rec.allowed is False
    assert res_rec.blocking_rule == "CASE_ALREADY_RECOVERED_OR_CLOSED"

    res_closed = PolicyEngine.evaluate(
        action_type=ActionType.RETRY_PAYMENT,
        context=base_context,
        case_status="CLOSED",
    )
    assert res_closed.allowed is False
    assert res_closed.blocking_rule == "CASE_ALREADY_RECOVERED_OR_CLOSED"


def test_rule_2_and_7_max_retries_capped_at_3(base_context: DecisionContext):
    """RULE 2 & 7: Ensure payment retry is blocked after 3 attempts."""
    # 2 attempts -> allowed
    base_context.previous_action_types = [ActionType.RETRY_PAYMENT, ActionType.RETRY_PAYMENT]
    res_ok = PolicyEngine.evaluate(
        action_type=ActionType.RETRY_PAYMENT,
        context=base_context,
        case_status="OPEN",
    )
    assert res_ok.allowed is True
    assert res_ok.blocking_rule is None

    # 3 attempts -> blocked
    base_context.previous_action_types.append(ActionType.RETRY_PAYMENT)
    res_blocked = PolicyEngine.evaluate(
        action_type=ActionType.RETRY_PAYMENT,
        context=base_context,
        case_status="OPEN",
    )
    assert res_blocked.allowed is False
    assert res_blocked.blocking_rule == "MAX_RETRIES_EXCEEDED"


def test_rule_3_quiet_hours_blocks_voice_outreach_after_20_00(base_context: DecisionContext):
    """RULE 3: Voice outreach must be blocked between 20:00 and 08:00."""
    # 20:30 (night) -> blocked
    base_context.current_time = datetime(2026, 8, 22, 20, 30, 0, tzinfo=timezone.utc)
    res_night = PolicyEngine.evaluate(
        action_type=ActionType.VOICE_OUTREACH,
        context=base_context,
        case_status="OPEN",
    )
    assert res_night.allowed is False
    assert res_night.blocking_rule == "QUIET_HOURS_VOICE_PROHIBITED"

    # 06:00 (early morning) -> blocked
    base_context.current_time = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    res_morning = PolicyEngine.evaluate(
        action_type=ActionType.VOICE_OUTREACH,
        context=base_context,
        case_status="OPEN",
    )
    assert res_morning.allowed is False
    assert res_morning.blocking_rule == "QUIET_HOURS_VOICE_PROHIBITED"

    # 15:00 (daytime) -> allowed
    base_context.current_time = datetime(2026, 8, 22, 15, 0, 0, tzinfo=timezone.utc)
    res_day = PolicyEngine.evaluate(
        action_type=ActionType.VOICE_OUTREACH,
        context=base_context,
        case_status="OPEN",
    )
    assert res_day.allowed is True


def test_rule_4_active_promise_to_pay_pauses_routine_outreach(base_context: DecisionContext):
    """RULE 4: Active Promise-to-Pay must block automated contact & retries."""
    base_context.promise_to_pay_active = True

    for action in [
        ActionType.RETRY_PAYMENT,
        ActionType.SEND_PAYMENT_LINK,
        ActionType.SEND_WHATSAPP_REMINDER,
        ActionType.VOICE_OUTREACH,
    ]:
        res = PolicyEngine.evaluate(
            action_type=action,
            context=base_context,
            case_status="OPEN",
        )
        assert res.allowed is False
        assert res.blocking_rule == "PROMISE_TO_PAY_ACTIVE"


def test_rule_6_active_intervention_blocks_parallel_duplicate_action(base_context: DecisionContext):
    """RULE 6: Active in-progress intervention blocks parallel duplicate action."""
    res = PolicyEngine.evaluate(
        action_type=ActionType.SEND_PAYMENT_LINK,
        context=base_context,
        case_status="OPEN",
        active_interventions_count=1,
    )
    assert res.allowed is False
    assert res.blocking_rule == "ACTIVE_INTERVENTION_EXISTS"
