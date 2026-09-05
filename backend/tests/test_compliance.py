"""Compliance v2: consent ledger, opt-out keywords, contact windows, DND, frequency caps, handoff, audit chain."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.audit.chain import AuditChain, compute_row_hash, canonical_payload
from app.compliance.consent import ConsentService
from app.compliance.contact_policy import ContactPolicy
from app.compliance.dnd import StaticDndRegistry, set_dnd_registry
from app.compliance.handoff import HandoffService
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.contact_attempt import ContactAttempt
from app.models.recovery_plan import RecoveryPlan
from app.models.voice_call import VoiceCall
from app.services.recovery_scheduler import RecoveryScheduler
from app.services.voice_conversation_manager import VoiceConversationManager
from app.services.whatsapp_recovery_service import WhatsAppRecoveryService
from tests.helpers_v2 import make_case, make_customer

IST = __import__("zoneinfo").ZoneInfo("Asia/Kolkata")


def ist(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)
    set_dnd_registry(None)
    yield
    set_dnd_registry(None)


# ------------------------------------------------------------------ consent

def test_consent_opt_out_blocks_channel_and_mirrors_legacy_flags(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer)
    ConsentService.record(db_session, customer=customer, channel="WHATSAPP", status="OPT_OUT", source="OPERATOR", reason="asked on call")
    assert customer.whatsapp_allowed is False
    dec_wa = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=ist(2026, 9, 7, 11))
    dec_email = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="EMAIL", now=ist(2026, 9, 7, 11))
    assert dec_wa.allowed is False and dec_wa.rule == "CONSENT_OPT_OUT"
    assert dec_email.allowed is True
    summary = ConsentService.summary(db_session, customer.id)
    assert summary["effective"]["WHATSAPP"] == "OPT_OUT" and summary["effective"]["EMAIL"] == "OPT_IN"
    # blocked decision was registered for the scorecard
    assert ContactPolicy.blocks_by_rule(db_session, [case.id]) == {"CONSENT_OPT_OUT": 1}


def test_inbound_keywords_detect_scope():
    assert ConsentService.detect_opt_out("STOP") == "ALL"
    assert ConsentService.detect_opt_out("please stop calling me") == "VOICE"
    assert ConsentService.detect_opt_out("mujhe message mat bhejo") == "ALL"
    assert ConsentService.detect_opt_out("band karo yeh sab") == "ALL"
    assert ConsentService.detect_opt_out("I will pay tomorrow") is None


def test_inbound_stop_via_api_pauses_plan(client: TestClient, db_session: Session):
    customer = make_customer(db_session, phone="+919000000021")
    case = make_case(db_session, customer)
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    res = client.post("/compliance/inbound", json={"phone": "+919000000021", "channel": "WHATSAPP", "text": "do not contact me again"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["opt_out_detected"] and body["channel"] == "ALL"
    db_session.refresh(plan)
    db_session.refresh(case)
    assert plan.status == "PAUSED" and case.case_metadata["consent_withdrawn"] is True
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "OUTREACH_STOPPED_CONSENT_WITHDRAWN")) is not None
    # a bare STOP on WhatsApp only withdraws WhatsApp
    customer2 = make_customer(db_session, phone="+919000000022")
    res2 = client.post("/compliance/inbound", json={"phone": "+919000000022", "channel": "WHATSAPP", "text": "STOP"})
    assert res2.json()["channel"] == "WHATSAPP" and res2.json()["effective"]["VOICE"] == "OPT_IN"


def test_whatsapp_inbound_webhook_honours_stop(client: TestClient, db_session: Session):
    customer = make_customer(db_session, phone="+919000000023")
    make_case(db_session, customer)
    res = client.post("/webhooks/whatsapp/inbound", data={"From": "whatsapp:+919000000023", "Body": "STOP"})
    assert res.status_code == 200 and "unsubscribed" in res.text
    assert ConsentService.is_opted_out(db_session, customer.id, "WHATSAPP") is not None
    wa = WhatsAppRecoveryService.execute_recovery(db_session, make_case(db_session, customer).id, dry_run=True, reference_time=ist(2026, 9, 7, 11))
    assert wa.status == "BLOCKED"


# ------------------------------------------------------------------ windows / DND / holidays

def test_contact_windows_follow_customer_timezone(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer)
    late = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="VOICE", now=ist(2026, 9, 7, 19, 30), record=False)
    assert late.allowed is False and late.rule == "CONTACT_WINDOW_CLOSED"
    assert late.next_eligible_at.astimezone(IST).hour == 8 and late.next_eligible_at.astimezone(IST).day == 8
    early = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="VOICE", now=ist(2026, 9, 7, 6), record=False)
    assert early.rule == "CONTACT_WINDOW_CLOSED" and early.next_eligible_at.astimezone(IST).day == 7
    ok = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="VOICE", now=ist(2026, 9, 7, 10), record=False)
    assert ok.allowed is True
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=ist(2026, 9, 7, 21, 30), record=False).rule == "CONTACT_WINDOW_CLOSED"
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=ist(2026, 9, 7, 20, 30), record=False).allowed
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="EMAIL", now=ist(2026, 9, 7, 2), record=False).allowed


def test_no_voice_on_national_holiday(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer)
    dec = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="VOICE", now=ist(2026, 10, 2, 11), record=False)
    assert dec.allowed is False and dec.rule == "HOLIDAY_NO_CONTACT"
    assert dec.next_eligible_at.astimezone(IST).date().isoformat() == "2026-10-03"
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=ist(2026, 10, 2, 11), record=False).allowed


def test_trai_dnd_registry_blocks_voice_not_whatsapp(db_session: Session):
    set_dnd_registry(StaticDndRegistry({"+919000000099"}))
    customer = make_customer(db_session, phone="+919000000099")
    case = make_case(db_session, customer)
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="VOICE", now=ist(2026, 9, 7, 11), record=False).rule == "TRAI_DND_REGISTERED"
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="SMS", now=ist(2026, 9, 7, 11), record=False).rule == "TRAI_DND_REGISTERED"
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=ist(2026, 9, 7, 11), record=False).allowed


# ------------------------------------------------------------------ frequency caps

def test_cross_channel_frequency_caps(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer)
    t0 = ist(2026, 9, 1, 10)
    ContactPolicy.record_sent(db_session, case=case, channel="WHATSAPP", reference="c1", now=t0)
    # min gap
    gap = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="EMAIL", now=t0 + timedelta(hours=5), record=False)
    assert gap.rule == "CONTACT_MIN_GAP" and gap.next_eligible_at == t0 + timedelta(hours=settings.CONTACT_MIN_GAP_HOURS)
    ContactPolicy.record_sent(db_session, case=case, channel="EMAIL", reference="c2", now=t0 + timedelta(days=1))
    ContactPolicy.record_sent(db_session, case=case, channel="VOICE", reference="v1", now=t0 + timedelta(days=2))
    capped = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=t0 + timedelta(days=3), record=False)
    assert capped.rule == "CONTACT_FREQUENCY_CAP" and capped.next_eligible_at == t0 + timedelta(days=7)
    # after the window rolls, allowed again; but a second voice call inside 3 days is not
    later = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=t0 + timedelta(days=8), record=False)
    assert later.allowed
    ContactPolicy.record_sent(db_session, case=case, channel="VOICE", reference="v2", now=t0 + timedelta(days=8))
    voice_cap = ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="VOICE", now=t0 + timedelta(days=9), record=False)
    assert voice_cap.rule == "VOICE_FREQUENCY_CAP"


def test_leaf_service_hits_cross_channel_cap_from_other_cases(db_session: Session):
    customer = make_customer(db_session)
    other = make_case(db_session, customer)
    t0 = ist(2026, 9, 1, 10)
    for i in range(3):
        ContactPolicy.record_sent(db_session, case=other, channel="EMAIL", reference=f"e{i}", now=t0 + timedelta(days=i))
    case = make_case(db_session, customer)
    wa = WhatsAppRecoveryService.execute_recovery(db_session, case.id, dry_run=True, reference_time=t0 + timedelta(days=3))
    assert wa.status == "BLOCKED" and wa.policy_blocking_rule == "CONTACT_FREQUENCY_CAP"


# ------------------------------------------------------------------ handoff

def test_handoff_freezes_and_release_resumes(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer)
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    HandoffService.freeze(db_session, case=case, reason="dispute", source="TEST")
    db_session.refresh(plan)
    assert plan.status == "PAUSED"
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="EMAIL", record=False).rule == "HUMAN_HANDOFF_FREEZE"
    HandoffService.release(db_session, case=case, operator="ops@merchant")
    db_session.refresh(plan)
    assert plan.status in ("ACTIVE", "WAITING")
    assert ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="EMAIL", record=False).allowed


def test_voice_dispute_and_wrong_number_intents_trigger_handoff(db_session: Session):
    customer = make_customer(db_session, phone="+919000000031")
    case = make_case(db_session, customer)
    RecoveryScheduler.create_or_get_plan(db_session, case.id)
    call = VoiceCall(recovery_case_id=case.id, customer_id=customer.id, provider="TWILIO", from_number="+10000000000", to_number=customer.phone, status="IN_PROGRESS", attempt_number=1, dynamic_variables={}, call_metadata={"conversation_state": "PAYMENT_STATUS", "conversation_history": []})
    db_session.add(call)
    db_session.flush()
    VoiceConversationManager.handle_turn(db_session, call, {"SpeechResult": "I want to dispute this charge, I did not order this", "Confidence": "0.95"}, gather_url="http://x/gather")
    db_session.refresh(case)
    assert case.case_metadata["human_handoff"] is True and "DISPUTE" in case.case_metadata["handoff_source"]

    customer2 = make_customer(db_session, phone="+919000000032")
    case2 = make_case(db_session, customer2)
    call2 = VoiceCall(recovery_case_id=case2.id, customer_id=customer2.id, provider="TWILIO", from_number="+10000000000", to_number=customer2.phone, status="IN_PROGRESS", attempt_number=1, dynamic_variables={}, call_metadata={"conversation_state": "PAYMENT_STATUS", "conversation_history": []})
    db_session.add(call2)
    db_session.flush()
    VoiceConversationManager.handle_turn(db_session, call2, {"SpeechResult": "this is a wrong number", "Confidence": "0.95"}, gather_url="http://x/gather")
    db_session.refresh(case2)
    assert case2.case_metadata["human_handoff"] is True
    assert ConsentService.is_opted_out(db_session, customer2.id, "VOICE") is not None


# ------------------------------------------------------------------ audit chain

def test_audit_chain_verifies_and_detects_tampering(client: TestClient, db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer)
    for i in range(5):
        db_session.add(AuditLog(recovery_case_id=case.id, actor_type="SYSTEM", actor_id="t", action=f"STEP_{i}", entity_type="X", entity_id=str(i), audit_metadata={"i": i}))
    db_session.flush()
    rows = db_session.scalars(select(AuditLog).where(AuditLog.recovery_case_id == case.id).order_by(AuditLog.sequence)).all()
    assert all(r.sequence is not None and r.row_hash for r in rows)
    assert [r.action for r in rows] == [f"STEP_{i}" for i in range(5)]
    assert rows[1].prev_hash == rows[0].row_hash
    assert rows[0].row_hash == compute_row_hash(rows[0].prev_hash, canonical_payload(rows[0]))

    report = AuditChain.verify(db_session, case_id=case.id)
    assert report["ok"] is True and report["case"]["rows"] == 5 and report["case"]["all_verified"]
    assert client.get("/audit/verify").json()["ok"] is True

    # Tamper with a historical row: change its metadata directly in SQL
    victim = rows[2]
    db_session.execute(update(AuditLog).where(AuditLog.id == victim.id).values({AuditLog.audit_metadata: {"i": 999}}))
    db_session.expire_all()
    broken = AuditChain.verify(db_session)
    assert broken["ok"] is False
    assert broken["first_break"]["sequence"] == victim.sequence and "row_hash mismatch" in broken["first_break"]["problem"]

    trail = client.get(f"/audit/cases/{case.id}").json()
    assert len(trail) >= 5 and all(t["row_hash"] for t in trail)


def test_scorecard_reports_contact_blocks_and_chain(db_session: Session):
    from app.analytics.scorecard import ScorecardService
    from app.ledger.service import LedgerService
    customer = make_customer(db_session)
    case = make_case(db_session, customer, batch_id="batch_cmp", experiment_arm="TREATMENT")
    LedgerService.post_at_risk(db_session, case, "pay_cmp")
    ConsentService.record(db_session, customer=customer, channel="ALL", status="OPT_OUT", source="OPERATOR")
    ContactPolicy.evaluate(db_session, case=case, customer=customer, channel="WHATSAPP", now=ist(2026, 9, 7, 11))
    sc = ScorecardService.compute(db_session, batch_id="batch_cmp")
    assert sc["compliance"]["contact_blocks_by_rule"] == {"CONSENT_OPT_OUT": 1}
    assert sc["compliance"]["audit_chain"]["ok"] is True
