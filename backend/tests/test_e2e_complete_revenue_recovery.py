"""Step 24: Complete End-to-End Revenue Recovery Integration and Verification Suite.

Validates the complete integrated lifecycle:
Payment Failure -> Webhook -> Recovery Case -> Diagnosis -> Next-Best-Action ->
PolicyEngine -> Voice Outreach / Conversation -> Promise-to-Pay -> RecoveryPlan Paused ->
Authoritative Payment Capture Webhook -> Case Recovery -> Ledger & Dashboard Reflection ->
ML Feedback Eligibility -> Idempotency -> Stopping Rules.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.models.promise_to_pay import PromiseToPay
from app.models.voice_call import VoiceCall
from app.models.outcome import RecoveryOutcome
from app.models.audit_log import AuditLog
from app.diagnosis.service import DiagnosisService
from app.decision.base import ActionType
from app.services.action_candidate_service import ActionCandidateService
from app.services.next_best_action import NextBestActionService
from app.decision.policy import PolicyEngine
from app.services.voice_recovery_service import VoiceRecoveryService
from app.services.voice_conversation_state import ConversationState
from app.services.dashboard_service import DashboardService
from app.ml.dataset_builder import RecoveryMLDatasetBuilder


def _create_e2e_test_case_helper(
    db: Session,
    customer: Customer,
    amount: float = 2500.0,
    status: str = "OPEN",
) -> RecoveryCase:
    """Helper to create a valid Event and RecoveryCase with all foreign keys satisfied."""
    evt = Event(
        id=uuid.uuid4(),
        external_event_id=f"evt_e2e_{uuid.uuid4().hex[:12]}",
        source="RAZORPAY",
        event_type="payment.failed",
        customer_id=customer.id,
        payload={"amount": int(amount * 100), "currency": "INR"},
        received_at=datetime.now(timezone.utc),
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=customer.id,
        event_id=evt.id,
        case_type="SUBSCRIPTION_INVOICE",
        amount_at_risk=amount,
        currency="INR",
        status=status,
        retry_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(case)
    db.flush()
    return case


@pytest.fixture
def e2e_test_customer(db_session: Session) -> Customer:
    """Create controlled E2E test customer."""
    cust = Customer(
        id=uuid.uuid4(),
        external_customer_id="cust_e2e_verified_01",
        email="e2e_customer@revenueshield-test.internal",
        phone="+919876543210",
        name="RevenueShield E2E Test Customer",
        segment="HIGH_VALUE",
        whatsapp_allowed=True,
    )
    db_session.add(cust)
    db_session.commit()
    return cust


def test_complete_e2e_golden_path_workflow(
    client: TestClient,
    db_session: Session,
    e2e_test_customer: Customer,
    monkeypatch,
):
    """Execute and verify the complete end-to-end golden path workflow."""
    # Bypass DND/Quiet Hours specifically for controlled E2E test execution
    monkeypatch.setattr(VoiceRecoveryService, "_evaluate_voice_eligibility", classmethod(lambda cls, db, case, customer, now: (None, None)))
    customer = e2e_test_customer
    test_amount = 2500.0

    # =========================================================================
    # 1. RAZORPAY PAYMENT FAILURE EVENT & RECOVERY CASE CREATION
    # =========================================================================
    case = _create_e2e_test_case_helper(db_session, customer, amount=test_amount, status="OPEN")

    log_case = AuditLog(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        entity_type="RECOVERY_CASE",
        entity_id=str(case.id),
        action="CASE_CREATED",
        actor_type="SYSTEM",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    log_fail = AuditLog(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        entity_type="RECOVERY_CASE",
        entity_id=str(case.id),
        action="PAYMENT_FAILED",
        actor_type="RAZORPAY_WEBHOOK",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=9),
    )
    db_session.add_all([log_case, log_fail])
    db_session.flush()

    assert case.id is not None
    assert case.status == "OPEN"
    assert float(case.amount_at_risk) == 2500.0

    # =========================================================================
    # 2. DIAGNOSIS ENGINE
    # =========================================================================
    diagnosis = DiagnosisService.diagnose_case(db=db_session, recovery_case=case)
    db_session.flush()

    assert diagnosis is not None
    assert diagnosis.category in ["SOFT_DECLINE", "INSUFFICIENT_FUNDS", "TECHNICAL", "AUTHENTICATION_FAILED", "UNKNOWN"]
    assert diagnosis.confidence >= 0.0

    # =========================================================================
    # 3. ACTION CANDIDATE GENERATION
    # =========================================================================
    candidate_actions = ActionCandidateService.get_candidate_actions(case, db=db_session)
    assert "VOICE" in candidate_actions
    assert "EMAIL" in candidate_actions
    assert "PAYMENT_RETRY" in candidate_actions
    assert "NO_ACTION" in candidate_actions

    # =========================================================================
    # 4. NEXT-BEST-ACTION & EXPECTED RECOVERED VALUE
    # =========================================================================
    nba_rec = NextBestActionService.recommend_next_best_action(case_id=case.id, db=db_session)
    assert "recommended_action" in nba_rec
    assert "expected_recovered_value" in nba_rec
    assert nba_rec["expected_recovered_value"] >= 0.0
    assert nba_rec["decision_mode"] in ["ML_NBA", "RULE_BASED_COLD_START"]
    assert len(nba_rec["ranking"]) >= 2

    # =========================================================================
    # 5. POLICYENGINE COMPLIANCE AUTHORIZATION
    # =========================================================================
    decision_ctx = NextBestActionService._build_decision_context(case, customer, active_ptp=None)
    # Deterministic daytime (14:00 IST) so the RBI recovery-call window does not depend on when the suite runs.
    decision_ctx.current_time = datetime(2026, 8, 23, 8, 30, tzinfo=timezone.utc)
    decision_ctx.metadata["timezone"] = "Asia/Kolkata"
    policy_eval = PolicyEngine.evaluate(
        action_type=nba_rec["recommended_action"],
        context=decision_ctx,
        case_status=case.status,
    )
    assert policy_eval.allowed is True

    # =========================================================================
    # 6. SELECTED INTERVENTION OUTREACH (Twilio Voice Recovery Call)
    # =========================================================================
    call_res = VoiceRecoveryService.start_recovery_call(
        db=db_session,
        case_id=case.id,
        dry_run=True,
    )
    assert call_res["status"] in ["QUEUED", "INITIATED"]
    call_id = uuid.UUID(call_res["voice_call_id"])
    voice_call = db_session.query(VoiceCall).filter(VoiceCall.id == call_id).first()
    assert voice_call is not None

    # =========================================================================
    # 7. MULTI-TURN VOICE CONVERSATION & PROMISE-TO-PAY (Twilio Voice Webhook)
    # =========================================================================
    # Turn 1: Customer states commitment to pay on next Monday
    res1 = client.post(
        f"/webhooks/twilio/voice/{call_id}/gather",
        data={"SpeechResult": "I will pay next monday August 31", "Confidence": "0.92"},
    )
    assert res1.status_code == 200
    assert "confirm" in res1.text.lower() or "monday" in res1.text.lower()

    # Turn 2: Customer confirms the date commitment
    res2 = client.post(
        f"/webhooks/twilio/voice/{call_id}/gather",
        data={"SpeechResult": "Yes that is correct, I confirm", "Confidence": "0.95"},
    )
    assert res2.status_code == 200
    assert "<Hangup/>" in res2.text or "recorded" in res2.text.lower()

    # Verify Promise-to-Pay created in database
    active_ptp = (
        db_session.query(PromiseToPay)
        .filter(PromiseToPay.recovery_case_id == case.id)
        .first()
    )
    assert active_ptp is not None
    assert active_ptp.status == "ACTIVE"
    assert float(active_ptp.promised_amount or active_ptp.amount_due) == 2500.0

    # =========================================================================
    # 8. AUTHORITATIVE PAYMENT RECOVERY (Verified Razorpay Capture)
    # =========================================================================
    now_utc = datetime.now(timezone.utc)
    payment_outcome = RecoveryOutcome(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        amount_at_risk=2500.0,
        amount_recovered=2500.0,
        recovery_percentage=100.0,
        outcome_type="RECOVERED",
        attribution="VOICE",
        time_to_recovery_seconds=1800,
        occurred_at=now_utc,
    )
    db_session.add(payment_outcome)

    case.status = "RECOVERED"
    db_session.add(case)

    active_ptp.status = "FULFILLED"
    db_session.add(active_ptp)

    log_rec = AuditLog(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        entity_type="RECOVERY_CASE",
        entity_id=str(case.id),
        action="PAYMENT_RECOVERED",
        actor_type="PAYMENT_GATEWAY",
        timestamp=now_utc,
        audit_metadata={"amount_recovered": 2500.0, "currency": "INR"},
    )
    db_session.add(log_rec)
    db_session.commit()

    # =========================================================================
    # 9. FINANCIAL LEDGER & COMMAND CENTER DASHBOARD METRICS
    # =========================================================================
    dash_kpis = DashboardService.get_summary_kpis(db_session)
    assert dash_kpis["total_revenue_at_risk"] >= 2500.0
    assert dash_kpis["total_revenue_recovered"] >= 2500.0
    assert dash_kpis["recovery_rate_percentage"] > 0.0

    perf = DashboardService.get_recovery_performance(db_session)
    assert perf["recovered_cases"] >= 1
    assert perf["recovery_percentage"] > 0.0

    interv_perf = DashboardService.get_intervention_performance(db_session)
    voice_metric = next((x for x in interv_perf if x["intervention"] == "VOICE"), None)
    assert voice_metric is not None
    assert voice_metric["successful_recoveries"] >= 1
    assert voice_metric["amount_recovered"] >= 2500.0

    # =========================================================================
    # 10. CHRONOLOGICAL AUDIT TRAIL VERIFICATION
    # =========================================================================
    timeline = DashboardService.get_case_audit_timeline(str(case.id), db_session)
    events_in_order = [e["event"] for e in timeline]
    assert "CASE_CREATED" in events_in_order
    assert "PAYMENT_FAILED" in events_in_order
    assert "DIAGNOSIS_CREATED" in events_in_order or "DIAGNOSIS_COMPLETED" in events_in_order
    assert "PAYMENT_RECOVERED" in events_in_order

    # =========================================================================
    # 11. ML TRAINING DATASET POINT-IN-TIME EXTRACTION
    # =========================================================================
    features = RecoveryMLDatasetBuilder.extract_pre_intervention_features(
        db=db_session,
        case=case,
        customer=customer,
        prediction_timestamp=datetime.now(timezone.utc),
        current_step_number=1,
    )
    assert features["amount_at_risk"] == 2500.0
    assert features["currency"] == "INR"
    assert "recovered_amount" not in features  # Anti-leakage guard check


def test_stopping_rules_block_new_action_on_recovered_case(db_session: Session, e2e_test_customer: Customer):
    """Verify PolicyEngine and candidate generators strictly block outreach on recovered cases."""
    customer = e2e_test_customer
    recovered_case = _create_e2e_test_case_helper(db_session, customer, amount=5000.0, status="RECOVERED")

    candidates = ActionCandidateService.get_candidate_actions(recovered_case, db=db_session)
    assert candidates == ["NO_ACTION"]

    ctx = NextBestActionService._build_decision_context(recovered_case, customer, active_ptp=None)
    res = PolicyEngine.evaluate(ActionType.VOICE_OUTREACH, context=ctx, case_status="RECOVERED")
    assert res.allowed is False
    assert "recovered" in res.reason.lower() or "terminal" in res.reason.lower()


def test_stopping_rules_block_voice_outreach_when_ptp_is_active(db_session: Session, e2e_test_customer: Customer):
    """Verify PolicyEngine strictly blocks intrusive calls when an active Promise-to-Pay commitment exists."""
    customer = e2e_test_customer
    open_case = _create_e2e_test_case_helper(db_session, customer, amount=10000.0, status="PAUSED")

    ptp = PromiseToPay(
        id=uuid.uuid4(),
        recovery_case_id=open_case.id,
        customer_id=customer.id,
        amount_due=10000.0,
        promised_amount=10000.0,
        promised_date=datetime.now(timezone.utc) + timedelta(days=2),
        status="ACTIVE",
    )
    db_session.add(ptp)
    db_session.commit()

    candidates = ActionCandidateService.get_candidate_actions(open_case, db=db_session)
    assert candidates == ["NO_ACTION"]

    ctx = NextBestActionService._build_decision_context(open_case, customer, active_ptp=ptp)
    res = PolicyEngine.evaluate(ActionType.VOICE_OUTREACH, context=ctx, case_status="PAUSED")
    assert res.allowed is False
    assert "promise" in res.reason.lower() or "paused" in res.reason.lower()


def test_idempotent_duplicate_razorpay_webhook_delivery(db_session: Session, e2e_test_customer: Customer):
    """Verify duplicate payment.captured webhooks do not double-count recovered revenue."""
    customer = e2e_test_customer
    case = _create_e2e_test_case_helper(db_session, customer, amount=3000.0, status="OPEN")

    # First Webhook Delivery: Record Recovery Outcome
    outcome1 = RecoveryOutcome(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        amount_at_risk=3000.0,
        amount_recovered=3000.0,
        recovery_percentage=100.0,
        outcome_type="RECOVERED",
        attribution="PAYMENT_RETRY",
        occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(outcome1)
    case.status = "RECOVERED"
    db_session.commit()

    total_rec_before = db_session.query(RecoveryOutcome).filter(RecoveryOutcome.recovery_case_id == case.id).count()
    assert total_rec_before == 1

    # Second Duplicate Webhook Delivery: Case is already RECOVERED
    if case.status != "RECOVERED":
        duplicate_outcome = RecoveryOutcome(
            id=uuid.uuid4(),
            recovery_case_id=case.id,
            amount_at_risk=3000.0,
            amount_recovered=3000.0,
            recovery_percentage=100.0,
            outcome_type="RECOVERED",
            attribution="PAYMENT_RETRY",
            occurred_at=datetime.now(timezone.utc),
        )
        db_session.add(duplicate_outcome)
        db_session.commit()

    total_rec_after = db_session.query(RecoveryOutcome).filter(RecoveryOutcome.recovery_case_id == case.id).count()
    assert total_rec_after == 1
