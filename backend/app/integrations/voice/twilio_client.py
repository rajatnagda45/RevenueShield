"""Twilio Voice client for initiating outbound phone recovery calls."""
import logging
from typing import Any, Dict, Optional
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TWIML = """<Response>
    <Say language="en-IN">Namaste. This is a test call from the AI Revenue Recovery system. Your payment recovery assistant is being connected.</Say>
</Response>"""


class TwilioVoiceClient:
    """Client for initiating outbound phone calls via Twilio Voice API."""

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
    ) -> None:
        self.account_sid = account_sid or settings.TWILIO_ACCOUNT_SID
        self.auth_token = auth_token or settings.TWILIO_AUTH_TOKEN
        self.from_number = from_number or settings.TWILIO_PHONE_NUMBER
        self._client: Optional[Client] = None

    def _get_client(self) -> Client:
        if not self.account_sid:
            raise ValueError("TWILIO_ACCOUNT_SID is not configured.")
        if not self.auth_token:
            raise ValueError("TWILIO_AUTH_TOKEN is not configured.")

        if self._client is None:
            self._client = Client(self.account_sid, self.auth_token)
        return self._client

    def create_outbound_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        twiml: Optional[str] = None,
        url: Optional[str] = None,
        status_callback: Optional[str] = None,
        status_callback_event: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Create an outbound phone call using Twilio Voice Calls API.

        Args:
            to_number: Destination phone number in E.164 format (e.g. +919876543210).
            from_number: Optional origin phone number; defaults to TWILIO_PHONE_NUMBER.
            twiml: Optional inline TwiML XML string.
            url: Optional webhook URL pointing to TwiML instructions.
            status_callback: Optional URL for status lifecycle webhooks.
            status_callback_event: Optional list of lifecycle events to report.

        Returns:
            Dict containing call_sid, status, to_number, from_number.
        """
        caller_id = from_number or self.from_number
        if not caller_id:
            raise ValueError("TWILIO_PHONE_NUMBER is not configured.")
        if not to_number:
            raise ValueError("Destination phone number is required.")

        client = self._get_client()

        masked_to = f"{to_number[:4]}***{to_number[-2:]}" if len(to_number) > 6 else "***"
        logger.info(f"[TWILIO_VOICE_CALL_REQUEST] Initiating call to {masked_to} from {caller_id}")

        # Prepare call parameters
        kwargs: Dict[str, Any] = {
            "to": to_number,
            "from_": caller_id,
        }
        if url:
            kwargs["url"] = url
        else:
            kwargs["twiml"] = twiml or DEFAULT_TWIML

        if status_callback:
            kwargs["status_callback"] = status_callback
            if status_callback_event:
                kwargs["status_callback_event"] = status_callback_event

        try:
            try:
                call = client.calls.create(**kwargs)
            except Exception as exc:
                exc_str = str(exc).lower()
                # If trial account rejects raw 'twiml' parameter, fallback to template URL
                if "disallowed parameters" in exc_str or "trial accounts" in exc_str or "21620" in exc_str:
                    logger.warning("[TWILIO_TRIAL_FALLBACK] Twilio trial account detected. Falling back to template URL.")
                    fallback_url = "https://webhooks.twilio.com/v1/Voice/Template/voice_speech_recognition"
                    call = client.calls.create(
                        to=to_number,
                        from_=caller_id,
                        url=fallback_url,
                    )
                else:
                    raise exc

            logger.info(f"[TWILIO_VOICE_CALL_QUEUED] Call created successfully. call_sid={call.sid}, status={call.status}")
            return {
                "call_sid": call.sid,
                "status": call.status,
                "to_number": to_number,
                "from_number": caller_id,
            }

        except TwilioRestException as exc:
            logger.error(f"[TWILIO_VOICE_API_ERROR] Twilio API error (code {exc.code}): {exc.msg}")
            raise RuntimeError(f"Twilio Voice API error: {exc.msg}")
        except Exception as exc:
            logger.error(f"[TWILIO_VOICE_CALL_ERROR] Failed to initiate Twilio call: {exc}")
            raise RuntimeError(f"Failed to initiate Twilio voice call: {str(exc)}")
