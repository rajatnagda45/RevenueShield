"""Comprehensive verification script for STEP 19 Voice Recovery integration."""
from decimal import Decimal
import os
import sys
import uuid
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.voice_call import VoiceCall


def run_verification():
    db = SessionLocal()
    print("=" * 60)
    print("REVENUESHIELD — STEP 19 VOICE RECOVERY VERIFICATION")
    print("=" * 60)

    # 1. Ensure a test customer and recovery case exist with daytime timezone
    phone_target = "+917991142735"
    cust = db.scalar(select(Customer).where(Customer.phone == phone_target))
    if not cust:
        cust = Customer(
            id=uuid.uuid4(),
            external_customer_id=f"cust_test_{uuid.uuid4().hex[:6]}",
            name="Ankit Kumar",
            email="ankit.recovery@example.com",
            phone=phone_target,
            dnd_enabled=False,
            timezone="UTC",
        )
        db.add(cust)
        db.flush()
    else:
        cust.timezone = "UTC"
        cust.dnd_enabled = False
        db.flush()

    evt = Event(
        id=uuid.uuid4(),
        external_event_id=f"evt_voice_{uuid.uuid4().hex[:8]}",
        event_type="payment.failed",
        source="RAZORPAY",
        payload={"amount": 499900},
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=Decimal("4999.00"),
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db.add(case)
    db.commit()
    db.refresh(case)

    print(f"\n[1] Prepared Test Recovery Case: {case.id}")
    print(f"    Customer: {cust.name} ({cust.phone})")
    print(f"    Amount at Risk: {case.currency} {case.amount_at_risk}")

    # 2. Trigger Voice Recovery API (dry_run)
    api_url = f"http://127.0.0.1:8000/recovery-cases/{case.id}/voice-recovery"
    print(f"\n[2] Triggering POST {api_url} (dry_run=True)...")
    res = httpx.post(api_url, json={"dry_run": True}, timeout=10.0)
    print(f"    Response Status: {res.status_code}")
    data = res.json()
    print(f"    Response Body:   {data}")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    voice_call_id = data["voice_call_id"]
    call_sid = data["call_sid"]

    # 3. Test Dynamic Personalized English TwiML Generation
    twiml_url = f"http://127.0.0.1:8000/webhooks/twilio/voice/{voice_call_id}"
    print(f"\n[3] Testing TwiML Webhook GET {twiml_url}...")
    twiml_res = httpx.get(twiml_url, timeout=10.0)
    print(f"    TwiML Status: {twiml_res.status_code}")
    print(f"    TwiML Content-Type: {twiml_res.headers.get('content-type')}")
    print("    Rendered TwiML XML:")
    print("    " + "\n    ".join(twiml_res.text.splitlines()))
    assert twiml_res.status_code == 200
    assert f"Hello {cust.name}" in twiml_res.text
    assert "4,999.00 Rupees" in twiml_res.text
    assert "Polly.Aditi" in twiml_res.text

    # 4. Test Status Callback Webhook
    status_url = "http://127.0.0.1:8000/webhooks/twilio/status"
    print(f"\n[4] Testing Status Callback POST {status_url}...")
    cb_res = httpx.post(
        status_url,
        data={
            "CallSid": call_sid,
            "CallStatus": "completed",
            "CallDuration": "52",
        },
        timeout=10.0,
    )
    print(f"    Callback Status: {cb_res.status_code}")
    print(f"    Callback Body:   {cb_res.json()}")
    assert cb_res.status_code == 200

    # 5. Verify Database State
    db.expire_all()
    vcall = db.scalar(select(VoiceCall).where(VoiceCall.id == uuid.UUID(voice_call_id)))
    print(f"\n[5] Verified VoiceCall in PostgreSQL:")
    print(f"    VoiceCall ID:    {vcall.id}")
    print(f"    Provider Call:   {vcall.provider_call_id}")
    print(f"    Status:          {vcall.status}")
    print(f"    Duration:        {vcall.duration_seconds}s")
    assert vcall.status == "COMPLETED"
    assert vcall.duration_seconds == 52

    # 6. Test Stopping Rule (e.g. Case already recovered)
    case.status = "RECOVERED"
    db.commit()
    print(f"\n[6] Testing Stopping Rule (Case RECOVERED)...")
    res_blocked = httpx.post(api_url, json={"dry_run": True}, timeout=10.0)
    print(f"    Blocked Status:  {res_blocked.status_code} (Expected 400)")
    print(f"    Blocked Detail:  {res_blocked.json().get('detail')}")
    assert res_blocked.status_code == 400

    print("\n" + "=" * 60)
    print("ALL FEATURE VERIFICATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    run_verification()
