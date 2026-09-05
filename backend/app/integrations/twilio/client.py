"""Twilio WhatsApp client with Basic Auth, Sandbox recipient safety, number formatting, API Key support, and exponential backoff."""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, Optional
import httpx

from app.core.config import settings
from app.services.notification_service import mask_contact

logger = logging.getLogger(__name__)


@dataclass
class TwilioMessageResponse:
    """Standardized response from Twilio API call."""

    success: bool
    status: str  # "SENT", "QUEUED", "DELIVERED", "FAILED", "BLOCKED"
    message_sid: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None
    dispatched_at: Optional[datetime] = None


def normalize_whatsapp_address(phone_number: str) -> str:
    """Normalize phone number to Twilio WhatsApp address format: whatsapp:+<country_code><number>."""
    if not phone_number:
        return ""
    cleaned = phone_number.strip()
    if cleaned.startswith("whatsapp:"):
        return cleaned
    if not cleaned.startswith("+"):
        cleaned = f"+{cleaned}"
    return f"whatsapp:{cleaned}"


class TwilioWhatsAppClient:
    """Twilio REST API client for WhatsApp messaging with Sandbox security and bounded retries."""

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        api_key_sid: Optional[str] = None,
        api_key_secret: Optional[str] = None,
        whatsapp_from: Optional[str] = None,
        whatsapp_to: Optional[str] = None,
        mode: Optional[str] = None,
        max_retries: int = 3,
        timeout: float = 10.0,
    ) -> None:
        self.account_sid = account_sid if account_sid is not None else settings.TWILIO_ACCOUNT_SID
        self.auth_token = auth_token if auth_token is not None else settings.TWILIO_AUTH_TOKEN
        self.api_key_sid = api_key_sid if api_key_sid is not None else settings.TWILIO_API_KEY_SID
        self.api_key_secret = api_key_secret if api_key_secret is not None else settings.TWILIO_API_KEY_SECRET

        self.whatsapp_from = normalize_whatsapp_address(
            whatsapp_from or settings.TWILIO_WHATSAPP_FROM or settings.TWILIO_WHATSAPP_NUMBER or ""
        )
        self.whatsapp_to = normalize_whatsapp_address(
            whatsapp_to or settings.TWILIO_WHATSAPP_TO or ""
        )
        self.mode = (mode or settings.TWILIO_WHATSAPP_MODE or "SANDBOX").upper()
        self.max_retries = max_retries
        self.timeout = timeout

        # Resolve Basic Auth credentials: API Key (SK...) or Account SID (AC...)
        self.auth_user = self.api_key_sid or self.account_sid
        self.auth_pass = self.api_key_secret or self.auth_token

        # In Twilio URL path, use account_sid if provided, else fallback to auth_user
        self.account_url_id = self.account_sid or self.api_key_sid

        if not self.auth_user or not self.auth_pass:
            logger.warning("[TWILIO_INIT] Twilio credentials not configured. Client in unconfigured state.")

    def send_whatsapp_message(
        self,
        recipient: str,
        message_body: str,
        status_callback_url: Optional[str] = None,
    ) -> TwilioMessageResponse:
        """Send a WhatsApp message via Twilio Messages API with Sandbox safety checks and retry backoff."""
        if not self.auth_user or not self.auth_pass or not self.whatsapp_from:
            return TwilioMessageResponse(
                success=False,
                status="FAILED",
                error_code="TWILIO_NOT_CONFIGURED",
                error_message="Twilio credentials or TWILIO_WHATSAPP_FROM not configured.",
            )

        recipient_formatted = normalize_whatsapp_address(recipient)
        masked_to = mask_contact(recipient_formatted.replace("whatsapp:", ""))
        masked_from = mask_contact(self.whatsapp_from.replace("whatsapp:", ""))

        # 1. Sandbox Recipient Restriction Guard
        if self.mode == "SANDBOX":
            if not self.whatsapp_to:
                return TwilioMessageResponse(
                    success=False,
                    status="BLOCKED",
                    error_code="SANDBOX_RECIPIENT_RESTRICTION",
                    error_message="TWILIO_WHATSAPP_TO test recipient must be configured in SANDBOX mode.",
                )
            if recipient_formatted != self.whatsapp_to:
                logger.warning(
                    f"[TWILIO_SANDBOX_BLOCKED] Attempted to send to {masked_to} but Sandbox is restricted to {mask_contact(self.whatsapp_to.replace('whatsapp:', ''))}."
                )
                return TwilioMessageResponse(
                    success=False,
                    status="BLOCKED",
                    error_code="SANDBOX_RECIPIENT_RESTRICTION",
                    error_message=f"SANDBOX mode: Outgoing messages restricted to configured recipient {mask_contact(self.whatsapp_to.replace('whatsapp:', ''))}.",
                )

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_url_id}/Messages.json"
        data = {
            "From": self.whatsapp_from,
            "To": recipient_formatted,
            "Body": message_body,
        }
        cb_url = status_callback_url or settings.TWILIO_STATUS_CALLBACK_URL
        if cb_url:
            data["StatusCallback"] = cb_url

        logger.info(f"[TWILIO_SEND_ATTEMPT] Mode={self.mode} From={masked_from} To={masked_to}")

        # 2. Bounded Exponential Backoff HTTP Dispatch
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        url,
                        data=data,
                        auth=(self.auth_user, self.auth_pass),
                    )

                if resp.status_code in [200, 201]:
                    payload = resp.json()
                    sid = payload.get("sid")
                    status_raw = (payload.get("status") or "queued").upper()
                    mapped_status = "SENT" if status_raw in ["QUEUED", "SENT", "SENDING"] else status_raw
                    logger.info(f"[TWILIO_SEND_SUCCESS] MessageSid={sid} Status={mapped_status}")
                    return TwilioMessageResponse(
                        success=True,
                        status=mapped_status,
                        message_sid=sid,
                        raw_payload=payload,
                        dispatched_at=datetime.now(timezone.utc),
                    )

                # Non-retryable 4xx client errors
                if 400 <= resp.status_code < 500:
                    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    err_code = str(payload.get("code") or resp.status_code)
                    err_msg = payload.get("message") or resp.text
                    logger.error(f"[TWILIO_4XX_ERROR] Status={resp.status_code} Code={err_code} Message={err_msg}")
                    return TwilioMessageResponse(
                        success=False,
                        status="FAILED",
                        error_code=err_code,
                        error_message=err_msg,
                        raw_payload=payload,
                    )

                # Retryable 5xx server errors
                last_error = f"Twilio HTTP {resp.status_code}: {resp.text}"
                logger.warning(f"[TWILIO_5XX_RETRY] Attempt {attempt}/{self.max_retries} failed: {last_error}")

            except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
                last_error = f"Network exception: {str(exc)}"
                logger.warning(f"[TWILIO_NETWORK_RETRY] Attempt {attempt}/{self.max_retries} error: {last_error}")

            if attempt < self.max_retries:
                backoff_wait = 0.5 * (2 ** (attempt - 1))
                time.sleep(backoff_wait)

        return TwilioMessageResponse(
            success=False,
            status="FAILED",
            error_code="TWILIO_DISPATCH_TIMEOUT_OR_SERVER_ERROR",
            error_message=last_error,
        )

    def get_message_status(self, message_sid: str) -> TwilioMessageResponse:
        """Fetch message status from Twilio REST API."""
        if not self.auth_user or not self.auth_pass:
            return TwilioMessageResponse(
                success=False,
                status="FAILED",
                error_code="TWILIO_NOT_CONFIGURED",
                error_message="Twilio credentials not configured.",
            )

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_url_id}/Messages/{message_sid}.json"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, auth=(self.auth_user, self.auth_pass))

            if resp.status_code == 200:
                payload = resp.json()
                status_raw = (payload.get("status") or "unknown").upper()
                mapped_status = "SENT" if status_raw in ["QUEUED", "SENT", "SENDING"] else status_raw
                return TwilioMessageResponse(
                    success=True,
                    status=mapped_status,
                    message_sid=message_sid,
                    raw_payload=payload,
                )
            return TwilioMessageResponse(
                success=False,
                status="FAILED",
                error_code=str(resp.status_code),
                error_message=resp.text,
            )
        except Exception as exc:
            return TwilioMessageResponse(
                success=False,
                status="FAILED",
                error_code="TWILIO_QUERY_ERROR",
                error_message=str(exc),
            )
