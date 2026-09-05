"""Test script to dispatch a live WhatsApp message via Twilio to the configured recipient."""
import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from decimal import Decimal
from app.core.config import settings
from app.integrations.twilio.client import TwilioWhatsAppClient, TwilioMessageResponse
from app.integrations.razorpay.payment_link_client import RazorpayPaymentLinkClient


def test_live_send():
    print("=" * 80)
    print("TESTING LIVE TWILIO WHATSAPP DISPATCH")
    print("=" * 80)

    print(f"From (Sender):    {settings.TWILIO_WHATSAPP_FROM}")
    print(f"To (Recipient):   {settings.TWILIO_WHATSAPP_TO}")
    print(f"Sandbox Mode:     {settings.TWILIO_WHATSAPP_MODE}")
    print(f"API Key SID:      {settings.TWILIO_API_KEY_SID}")

    # 1. Create a real Razorpay Test Mode Payment Link
    print("\n[Step 1] Creating a real Razorpay Test Mode Payment Link...")
    try:
        rzp_client = RazorpayPaymentLinkClient()
        link_dto = rzp_client.create_payment_link(
            amount_paise=500000,
            currency="INR",
            description="Payment recovery test",
            customer_name="Ankit Kumar",
            customer_phone="+917991142735",
        )
        payment_url = link_dto.short_url
        print(f"  [+] Created Real Razorpay Payment Link: {payment_url} (ID: {link_dto.payment_link_id})")
    except Exception as exc:
        print(f"  [-] Failed to create Razorpay link: {str(exc)}. Using test fallback link.")
        payment_url = "https://rzp.io/i/plink_demo_test"

    # 2. Format WhatsApp Message Body
    message_body = (
        f"Hi Ankit, your payment of ₹5,000.00 could not be completed.\n\n"
        f"You can securely complete your payment here:\n"
        f"{payment_url}\n\n"
        f"Thank you.\n- RevenueShield AI"
    )
    print(f"\n[Step 2] Message Body to be sent:\n---\n{message_body}\n---")

    # 3. Dispatch via TwilioWhatsAppClient
    print("\n[Step 3] Calling Twilio Messages API...")
    twilio_client = TwilioWhatsAppClient()
    res: TwilioMessageResponse = twilio_client.send_whatsapp_message(
        recipient="+917991142735",
        message_body=message_body,
    )

    print(f"\n[Step 4] Result from Twilio:")
    print(f"  Success:       {res.success}")
    print(f"  Status:        {res.status}")
    print(f"  Message SID:   {res.message_sid}")
    print(f"  Error Code:    {res.error_code}")
    print(f"  Error Message: {res.error_message}")
    if res.raw_payload:
        print(f"  Raw Payload:   {res.raw_payload}")

    print("=" * 80)
    if res.success:
        print(">>> LIVE TWILIO WHATSAPP MESSAGE SUCCESSFULLY DISPATCHED! <<<")
    else:
        print(">>> DISPATCH FAILED. CHECK ERROR CODE AND DETAILS ABOVE. <<<")
    print("=" * 80)


if __name__ == "__main__":
    test_live_send()
