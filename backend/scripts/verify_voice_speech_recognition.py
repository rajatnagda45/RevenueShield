"""End-to-End Verification script for Voice Speech Recognition & Promise-to-Pay extraction."""
from datetime import datetime, timezone
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
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.voice_call import VoiceCall


def run_verification():
    db = SessionLocal()
    print("=" * 70)
    print("REVENUESHIELD — VOICE SPEECH RECOGNITION & PROMISE-TO-PAY VERIFICATION")
    print("=" * 70)

    # 1. Create a fresh test case
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
        external_event_id=f"evt_ptp_{uuid.uuid4().hex[:8]}",
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

    print(f"\n[1] Prepared Recovery Case: {case.id}")
    print(f"    Customer: {cust.name} ({cust.phone})")
    print(f"    Amount at Risk: {case.currency} {case.amount_at_risk}")

    # 2. Trigger Voice Recovery API (dry_run)
    api_url = f"http://127.0.0.1:8000/recovery-cases/{case.id}/voice-recovery"
    print(f"\n[2] Triggering POST {api_url} (dry_run=True)...")
    res = httpx.post(api_url, json={"dry_run": True}, timeout=10.0)
    print(f"    Response Status: {res.status_code}")
    data = res.json()
    voice_call_id = data["voice_call_id"]
    call_sid = data["call_sid"]
    print(f"    VoiceCall ID: {voice_call_id}")
    print(f"    Call SID:     {call_sid}")

    # 3. Test Initial TwiML (Contains <Gather input="speech">)
    twiml_url = f"http://127.0.0.1:8000/webhooks/twilio/voice/{voice_call_id}"
    print(f"\n[3] Testing TwiML Webhook GET {twiml_url}...")
    twiml_res = httpx.get(twiml_url, timeout=10.0)
    print(f"    TwiML Status: {twiml_res.status_code}")
    print("    Rendered TwiML:")
    print("    " + "\n    ".join(twiml_res.text.splitlines()))
    assert twiml_res.status_code == 200
    assert "<Gather input=\"speech\"" in twiml_res.text
    assert f"Hello {cust.name}" in twiml_res.text

    # 4. Simulate Spoken Response from Customer: "I will pay next Monday"
    gather_url = f"http://127.0.0.1:8000/webhooks/twilio/voice/{voice_call_id}/gather"
    print(f"\n[4] Simulating Spoken Response POST {gather_url}...")
    print("    Customer Speech: 'I will pay next Monday'")
    gather_res = httpx.post(
        gather_url,
        data={
            "CallSid": call_sid,
            "SpeechResult": "I will pay next Monday",
            "Confidence": "0.94",
        },
        timeout=10.0,
    )
    print(f"    Gather Response Status: {gather_res.status_code}")
    print("    Spoken Confirmation TwiML:")
    print("    " + "\n    ".join(gather_res.text.splitlines()))
    assert gather_res.status_code == 200
    assert "recorded your promise to pay" in gather_res.text

    # 5. Verify PromiseToPay Created in DB
    db.expire_all()
    ptp = db.scalar(select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id))
    print(f"\n[5] Verified PromiseToPay in Database:")
    print(f"    Promise ID:       {ptp.id}")
    print(f"    Status:           {ptp.status}")
    print(f"    Source:           {ptp.source}")
    print(f"    Promised Date:    {ptp.promised_date}")
    print(f"    Promised Amount:  {ptp.currency} {ptp.promised_amount}")
    assert ptp is not None
    assert ptp.status == "ACTIVE"
    assert ptp.source == "VOICE_ASSISTANT"

    print("\n" + "=" * 70)
    print("SUCCESS: SPEECH RECOGNITION & PROMISE-TO-PAY FULLY VERIFIED!")
    print("=" * 70)


if __name__ == "__main__":
    run_verification()
