"""Experiment assignment and holdout enforcement tests (Phase 1 measurement layer)."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.decision.base import ActionType, DecisionContext
from app.decision.policy import PolicyEngine
from app.execution.guard import ExecutionGuard
from app.experiments.assigner import HOLDOUT_BLOCKING_RULE, ExperimentArm, ExperimentAssigner
from app.integrations.razorpay.security import compute_razorpay_signature
from app.models.audit_log import AuditLog
from app.models.experiment_assignment import ExperimentAssignment
from app.models.ledger_entry import LedgerEntry
from app.models.recovery_action import RecoveryAction
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlanStep
from app.services.email_recovery_service import EmailRecoveryService
from app.services.intervention_service import InterventionService
from app.services.recovery_scheduler import RecoveryScheduler
from app.services.voice_recovery_service import VoiceRecoveryService
from app.services.whatsapp_recovery_service import WhatsAppRecoveryService
from tests.helpers_v2 import make_case, make_customer


@pytest.fixture(autouse=True)
def _experiments_on(monkeypatch):
    monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
    monkeypatch.setattr(settings, "HOLDOUT_PERCENT", 10.0)
    monkeypatch.setattr(settings, "HOLDOUT_PERCENT_BY_SURFACE", "")


def test_compute_arm_is_deterministic():
    cid = uuid.uuid4()
    a1 = ExperimentAssigner.compute_arm(cid)
    a2 = ExperimentAssigner.compute_arm(cid)
    assert a1 == a2
    assert 0 <= a1[1] < 10_000
    assert a1[2] == 1000  # 10% -> 1000 bps


def test_holdout_fraction_is_close_to_configured():
    n = 6000
    holdouts = sum(1 for _ in range(n) if ExperimentAssigner.compute_arm(uuid.uuid4())[0] == ExperimentArm.HOLDOUT)
    frac = holdouts / n
    assert 0.08 <= frac <= 0.12, f"holdout fraction {frac:.3f} outside tolerance"


def test_disabled_experiments_put_everything_in_treatment(monkeypatch):
    monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", False)
    arms = {ExperimentAssigner.compute_arm(uuid.uuid4())[0] for _ in range(500)}
    assert arms == {ExperimentArm.TREATMENT}


def test_per_surface_override(monkeypatch):
    monkeypatch.setattr(settings, "HOLDOUT_PERCENT_BY_SURFACE", json.dumps({"CHECKOUT_ABANDONMENT": 50}))
    _, _, bps_default = ExperimentAssigner.compute_arm(uuid.uuid4(), surface="PAYMENT_FAILURE")
    _, _, bps_checkout = ExperimentAssigner.compute_arm(uuid.uuid4(), surface="CHECKOUT_ABANDONMENT")
    assert bps_default == 1000
    assert bps_checkout == 5000


def test_assign_is_idempotent_and_audited(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer)
    a1 = ExperimentAssigner.assign(db_session, case)
    a2 = ExperimentAssigner.assign(db_session, case)
    assert a1.id == a2.id
    assert case.experiment_arm == a1.arm
    assert db_session.scalar(select(ExperimentAssignment).where(ExperimentAssignment.recovery_case_id == case.id)) is not None
    audits = db_session.scalars(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "EXPERIMENT_ARM_ASSIGNED")).all()
    assert len(audits) == 1
    assert audits[0].audit_metadata["arm"] == a1.arm


def test_webhook_ingestion_assigns_arm_and_posts_at_risk(client: TestClient, db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "test_secret_exp")
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": f"pay_exp_{uuid.uuid4().hex[:8]}", "amount": 120000, "currency": "INR", "status": "failed",
            "method": "upi", "email": "exp@example.com", "contact": "+919911223344",
            "notes": {"batch_id": "batch_exp_1"}, "error_code": "BAD_REQUEST_ERROR", "error_reason": "insufficient_funds",
            "created_at": 1716300500,
        }}},
        "created_at": 1716300500,
    }
    raw = json.dumps(payload).encode()
    res = client.post(
        "/webhooks/razorpay", content=raw,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": compute_razorpay_signature(raw, "test_secret_exp"), "x-razorpay-event-id": f"evt_{uuid.uuid4().hex[:8]}"},
    )
    assert res.status_code == 200, res.text
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(res.json()["recovery_case_id"])))
    assert case.experiment_arm in {"TREATMENT", "HOLDOUT"}
    assert case.batch_id == "batch_exp_1"
    assert case.leak_surface == "PAYMENT_FAILURE"
    at_risk = db_session.scalar(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "AT_RISK"))
    assert at_risk is not None and float(at_risk.amount) == 1200.0


# --------------------------------------------------------------------------- holdout enforcement

def _holdout_case(db: Session):
    customer = make_customer(db)
    case = make_case(db, customer)
    ExperimentAssigner.assign(db, case, force_arm=ExperimentArm.HOLDOUT)
    assert ExperimentAssigner.is_holdout(case)
    return customer, case


def test_policy_engine_blocks_holdout():
    ctx = DecisionContext(
        case_id="x", case_type="PAYMENT_FAILURE", amount_at_risk=1000, currency="INR", case_age_hours=1.0, retry_count=0,
        diagnosis_category="INSUFFICIENT_FUNDS", diagnosis_confidence=0.9, risk_score=50, recovery_probability=0.5,
        metadata={"experiment_arm": "HOLDOUT"},
    )
    for action in [ActionType.SEND_PAYMENT_LINK, ActionType.RETRY_PAYMENT, ActionType.VOICE_OUTREACH, ActionType.SEND_WHATSAPP_REMINDER]:
        res = PolicyEngine.evaluate(action_type=action, context=ctx, case_status="OPEN")
        assert not res.allowed
        assert res.blocking_rule == HOLDOUT_BLOCKING_RULE
    assert PolicyEngine.evaluate(action_type=ActionType.NO_ACTION, context=ctx, case_status="OPEN").allowed


def test_scheduler_records_observe_step_for_holdout(db_session: Session):
    _, case = _holdout_case(db_session)
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    res1 = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, dry_run=True)
    res2 = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, dry_run=True)
    assert res1["status"] == "HOLDOUT_OBSERVE" and res1["action"] == "OBSERVE"
    assert res2["status"] == "HOLDOUT_OBSERVE"
    steps = db_session.scalars(select(RecoveryPlanStep).where(RecoveryPlanStep.recovery_plan_id == plan.id)).all()
    assert len(steps) == 1 and steps[0].action_type == "OBSERVE" and steps[0].channel == "NONE"
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "HOLDOUT_OBSERVE_ONLY")) is not None


def test_execution_guard_blocks_holdout(db_session: Session):
    _, case = _holdout_case(db_session)
    action = RecoveryAction(
        recovery_case_id=case.id, action_type="SEND_PAYMENT_LINK", channel="WHATSAPP", status="APPROVED",
        decision_engine_version="test", policy_engine_version="test", policy_result={"allowed": True}, alternatives=[], supporting_factors=[],
    )
    db_session.add(action)
    db_session.flush()
    res = ExecutionGuard.validate_pre_flight(db_session, case, action, idempotency_key=f"k_{case.id}")
    assert not res.allowed and res.blocking_rule == HOLDOUT_BLOCKING_RULE


def test_leaf_services_refuse_holdout(db_session: Session):
    _, case = _holdout_case(db_session)

    interv = InterventionService.execute_intervention(db_session, case.id, dry_run=True)
    assert interv.status == "BLOCKED" and "HOLDOUT" in (interv.reason or "")

    wa = WhatsAppRecoveryService.execute_recovery(db_session, case.id, dry_run=True)
    assert wa.status == "BLOCKED" and wa.policy_blocking_rule == HOLDOUT_BLOCKING_RULE

    em = EmailRecoveryService.execute_recovery(db_session, str(case.id))
    assert em["status"] == "BLOCKED" and em["blocking_rule"] == HOLDOUT_BLOCKING_RULE

    with pytest.raises(ValueError) as exc:
        VoiceRecoveryService.start_recovery_call(db_session, case.id, dry_run=True)
    assert "HOLDOUT" in str(exc.value)

    # No money-touching side effects
    assert db_session.scalar(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "COST")) is None
