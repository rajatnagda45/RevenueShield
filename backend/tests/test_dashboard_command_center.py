"""Unit and integration tests for Revenue Recovery Command Center Dashboard."""
from datetime import datetime, timezone, timedelta
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.customer import Customer
from app.models.event import Event
from app.models.diagnosis import Diagnosis
from app.models.outcome import RecoveryOutcome
from app.models.recovery_action import RecoveryAction
from app.models.promise_to_pay import PromiseToPay
from app.models.audit_log import AuditLog
from app.models.voice_call import VoiceCall
from app.models.recovery_attribution import RecoveryAttribution
from app.services.dashboard_service import DashboardService


@pytest.fixture
def dashboard_test_data(db_session: Session):
    """Seed comprehensive test data for dashboard aggregations."""
    cust1 = Customer(
        id=uuid.uuid4(),
        external_customer_id="cust_dash_01",
        email="dash_user1@example.com",
        phone="+919876543210",
        name="Dashboard Test User 1",
        segment="MID_VALUE",
    )
    cust2 = Customer(
        id=uuid.uuid4(),
        external_customer_id="cust_dash_02",
        email="dash_user2@example.com",
        phone="+919876543211",
        name="Dashboard Test User 2",
        segment="HIGH_VALUE",
    )
    db_session.add_all([cust1, cust2])
    db_session.flush()

    evt1 = Event(
        id=uuid.uuid4(),
        external_event_id="evt_dash_01",
        source="RAZORPAY",
        event_type="payment.failed",
        customer_id=cust1.id,
    )
    evt2 = Event(
        id=uuid.uuid4(),
        external_event_id="evt_dash_02",
        source="RAZORPAY",
        event_type="payment.failed",
        customer_id=cust2.id,
    )
    db_session.add_all([evt1, evt2])
    db_session.flush()

    # Case 1: Recovered (Amount: 5000)
    case1 = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=cust1.id,
        event_id=evt1.id,
        case_type="SUBSCRIPTION_INVOICE",
        amount_at_risk=5000.0,
        currency="INR",
        status="RECOVERED",
        retry_count=1,
    )
    # Case 2: Open (Amount: 15000)
    case2 = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=cust2.id,
        event_id=evt2.id,
        case_type="ONE_TIME_CHARGE",
        amount_at_risk=15000.0,
        currency="INR",
        status="OPEN",
        retry_count=0,
    )
    db_session.add_all([case1, case2])
    db_session.flush()

    # Diagnosis
    diag1 = Diagnosis(
        id=uuid.uuid4(),
        recovery_case_id=case1.id,
        category="TECHNICAL",
        explanation="Payment network failure.",
        confidence=0.9,
    )
    diag2 = Diagnosis(
        id=uuid.uuid4(),
        recovery_case_id=case2.id,
        category="SOFT_DECLINE",
        explanation="Card issuer declined.",
        confidence=0.85,
    )
    db_session.add_all([diag1, diag2])
    db_session.flush()

    # Verified Recovery Outcome on Case 1
    now = datetime.now(timezone.utc)
    outcome = RecoveryOutcome(
        id=uuid.uuid4(),
        recovery_case_id=case1.id,
        amount_at_risk=5000.0,
        amount_recovered=5000.0,
        recovery_percentage=100.0,
        outcome_type="RECOVERED",
        attribution="VOICE",
        time_to_recovery_seconds=3600,
        occurred_at=now,
    )
    db_session.add(outcome)

    # Promise to Pay on Case 2
    ptp = PromiseToPay(
        id=uuid.uuid4(),
        recovery_case_id=case2.id,
        customer_id=cust2.id,
        amount_due=15000.0,
        promised_amount=15000.0,
        promised_date=now + timedelta(days=3),
        status="ACTIVE",
    )
    db_session.add(ptp)

    # Actions & Voice Calls
    vcall = VoiceCall(
        id=uuid.uuid4(),
        recovery_case_id=case1.id,
        customer_id=cust1.id,
        to_number=cust1.phone,
        from_number="+17372212163",
        status="COMPLETED",
        duration_seconds=120,
    )
    action = RecoveryAction(
        id=uuid.uuid4(),
        recovery_case_id=case1.id,
        action_type="VOICE_CALL",
        status="COMPLETED",
    )
    db_session.add_all([vcall, action])

    # Audit Logs
    log1 = AuditLog(
        id=uuid.uuid4(),
        recovery_case_id=case1.id,
        entity_type="RECOVERY_CASE",
        entity_id=str(case1.id),
        action="PAYMENT_FAILED",
        actor_type="SYSTEM",
        timestamp=now - timedelta(hours=2),
    )
    log2 = AuditLog(
        id=uuid.uuid4(),
        recovery_case_id=case1.id,
        entity_type="RECOVERY_CASE",
        entity_id=str(case1.id),
        action="VOICE_CALL_COMPLETED",
        actor_type="VOICE_AGENT",
        timestamp=now - timedelta(hours=1),
    )
    log3 = AuditLog(
        id=uuid.uuid4(),
        recovery_case_id=case1.id,
        entity_type="RECOVERY_CASE",
        entity_id=str(case1.id),
        action="PAYMENT_RECOVERED",
        actor_type="PAYMENT_GATEWAY",
        timestamp=now,
    )
    db_session.add_all([log1, log2, log3])
    db_session.commit()

    return {"cust1": cust1, "cust2": cust2, "case1": case1, "case2": case2}


def test_dashboard_summary_kpi_calculations(db_session: Session, dashboard_test_data):
    """Verify authoritative summary calculations for at-risk, recovered, and recovery rate."""
    kpis = DashboardService.get_summary_kpis(db_session)
    
    assert kpis["total_revenue_at_risk"] == 20000.0  # 5000 + 15000
    assert kpis["total_revenue_recovered"] == 5000.0  # From verified RecoveryOutcome
    assert kpis["recovery_rate_percentage"] == 25.0  # 5000 / 20000 * 100
    assert kpis["active_recovery_cases"] == 1  # case2 is OPEN
    assert kpis["active_promise_to_pay_count"] == 1
    assert kpis["active_promise_to_pay_volume"] == 15000.0


def test_dashboard_summary_empty_division_by_zero_safety(db_session: Session):
    """Verify empty database returns 0.0 without division by zero errors."""
    kpis = DashboardService.get_summary_kpis(db_session)
    assert kpis["total_revenue_at_risk"] == 0.0
    assert kpis["total_revenue_recovered"] == 0.0
    assert kpis["recovery_rate_percentage"] == 0.0
    assert kpis["active_recovery_cases"] == 0
    assert kpis["active_promise_to_pay_count"] == 0


def test_dashboard_recovery_performance_metrics(db_session: Session, dashboard_test_data):
    """Verify recovery performance aggregates and time-to-recovery calculation."""
    perf = DashboardService.get_recovery_performance(db_session)
    assert perf["total_cases"] == 2
    assert perf["recovered_cases"] == 1
    assert perf["in_progress_cases"] == 1
    assert perf["recovery_percentage"] == 50.0
    assert perf["average_time_to_recovery_seconds"] == 3600.0
    assert perf["average_time_to_recovery_hours"] == 1.0


def test_dashboard_intervention_performance(db_session: Session, dashboard_test_data):
    """Verify channel-level recovery conversion calculation."""
    channels = DashboardService.get_intervention_performance(db_session)
    voice_ch = next((c for c in channels if c["intervention"] == "VOICE"), None)
    assert voice_ch is not None
    assert voice_ch["interventions_attempted"] >= 1
    assert voice_ch["successful_recoveries"] == 1
    assert voice_ch["amount_recovered"] == 5000.0
    assert voice_ch["recovery_rate"] > 0.0


def test_dashboard_recovery_trend_series(db_session: Session, dashboard_test_data):
    """Verify daily and cumulative trend generation."""
    trend = DashboardService.get_recovery_trend(db_session, days=7)
    assert "daily_trend" in trend
    assert "cumulative_trend" in trend
    assert len(trend["daily_trend"]) == 8  # 7 days + 1 today
    assert trend["total_period_recovered"] == 5000.0


def test_dashboard_model_status_cold_start_handling():
    """Verify model status endpoint safely returns cold-start state if model is uncalibrated."""
    status_info = DashboardService.get_model_status_info()
    assert "model_name" in status_info
    assert "model_version" in status_info
    assert "status" in status_info
    assert status_info["status"] in ["ACTIVE", "COLD_START", "UNAVAILABLE"]


def test_case_audit_timeline_chronological_ordering(db_session: Session, dashboard_test_data):
    """Verify audit timeline returns immutable events in chronological order."""
    case1 = dashboard_test_data["case1"]
    timeline = DashboardService.get_case_audit_timeline(str(case1.id), db_session)
    
    assert len(timeline) == 3
    assert timeline[0]["event"] == "PAYMENT_FAILED"
    assert timeline[1]["event"] == "VOICE_CALL_COMPLETED"
    assert timeline[2]["event"] == "PAYMENT_RECOVERED"


def test_api_dashboard_summary_endpoint(client: TestClient, dashboard_test_data):
    """Verify GET /dashboard/summary returns 200 with all KPI fields."""
    res = client.get("/dashboard/summary")
    assert res.status_code == 200
    data = res.json()
    assert "total_revenue_at_risk" in data
    assert "total_revenue_recovered" in data
    assert "recovery_rate_percentage" in data
    assert "active_recovery_cases" in data
    assert "expected_recovery_value" in data
    assert "decision_mode" in data


def test_api_dashboard_recovery_performance_endpoint(client: TestClient, dashboard_test_data):
    """Verify GET /dashboard/recovery-performance endpoint."""
    res = client.get("/dashboard/recovery-performance")
    assert res.status_code == 200
    data = res.json()
    assert data["total_cases"] == 2
    assert data["recovered_cases"] == 1


def test_api_dashboard_intervention_performance_endpoint(client: TestClient, dashboard_test_data):
    """Verify GET /dashboard/intervention-performance endpoint."""
    res = client.get("/dashboard/intervention-performance")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 4  # EMAIL, VOICE, PAYMENT_RETRY, WHATSAPP


def test_api_dashboard_recommendations_endpoint(client: TestClient, dashboard_test_data):
    """Verify GET /dashboard/recommendations endpoint."""
    res = client.get("/dashboard/recommendations?limit=5")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    if len(data) > 0:
        assert "recommended_action" in data[0]
        assert "expected_recovered_value" in data[0]


def test_api_dashboard_recovery_trend_endpoint(client: TestClient, dashboard_test_data):
    """Verify GET /dashboard/recovery-trend endpoint."""
    res = client.get("/dashboard/recovery-trend?days=14")
    assert res.status_code == 200
    data = res.json()
    assert "daily_trend" in data
    assert "cumulative_trend" in data


def test_api_dashboard_model_status_endpoint(client: TestClient):
    """Verify GET /dashboard/model-status endpoint."""
    res = client.get("/dashboard/model-status")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "model_version" in data


def test_api_dashboard_promises_to_pay_endpoint(client: TestClient, dashboard_test_data):
    """Verify GET /dashboard/promises-to-pay endpoint."""
    res = client.get("/dashboard/promises-to-pay")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["amount"] == 15000.0


def test_api_recovery_case_timeline_endpoint(client: TestClient, dashboard_test_data):
    """Verify GET /recovery-cases/{case_id}/timeline returns chronological audit events."""
    case1 = dashboard_test_data["case1"]
    res = client.get(f"/recovery-cases/{case1.id}/timeline")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 3
    assert data[0]["event"] == "PAYMENT_FAILED"
