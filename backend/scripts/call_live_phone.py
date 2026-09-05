"""Script to trigger an instant live Twilio Voice Call with Indian-accented Voice AI script."""
import os
import sys
import time
import httpx
from twilio.rest import Client

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import Settings

s = Settings(_env_file="backend/.env")

account_sid = s.TWILIO_ACCOUNT_SID
auth_token = s.TWILIO_AUTH_TOKEN
from_number = s.TWILIO_PHONE_NUMBER
to_number = "+917991142735"

live_voice_url = "https://revenueshield-backend.onrender.com/webhooks/twilio/test-voice"

print("=" * 70)
print("REVENUESHIELD - TRIGGER LIVE VOICE RECOVERY CALL")
print("=" * 70)
print(f"From (Twilio AI Agent): {from_number}")
print(f"To (Recipient Phone):   {to_number}")
print(f"Voice XML Endpoint:     {live_voice_url}")

# Pre-warm server to guarantee instant 0-second TwiML response
print("Pre-warming voice webhook endpoint...")
try:
    httpx.get("https://revenueshield-backend.onrender.com/health", timeout=10)
    print("Voice endpoint is online and responsive.")
except Exception as e:
    print(f"Pre-warm note: {e}")

print("Connecting to Twilio Voice API...")

try:
    client = Client(account_sid, auth_token)
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=live_voice_url
    )
    print("\n" + "=" * 70)
    print("SUCCESS: LIVE VOICE CALL DISPATCHED BY TWILIO!")
    print(f"Call SID: {call.sid}")
    print(f"Status:   {call.status}")
    print("Your phone +917991142735 is ringing now with Polly.Aditi Indian Voice AI!")
    print("=" * 70)
except Exception as e:
    print(f"\nTwilio API Response: {e}")
