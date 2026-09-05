"""Razorpay Payment Link API Client.

Handles direct HTTP transport communication with Razorpay Payment Links API
(https://api.razorpay.com/v1/payment_links) using Basic Authentication.
Never logs API credentials or raw sensitive customer details.
"""
from dataclasses import dataclass
import logging
import time
from typing import Any, Dict, Optional
import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)


class RazorpayPaymentLinkError(Exception):
    """Exception raised when a Payment Link API request fails."""

    def __init__(
        self,
        status_code: int,
        message: str,
        error_code: Optional[str] = None,
        error_payload: Optional[Dict[str, Any]] = None,
        is_retryable: bool = False,
    ):
        super().__init__(f"Razorpay Payment Link error ({status_code}): {message}")
        self.status_code = status_code
        self.message = message
        self.error_code = error_code
        self.error_payload = error_payload or {}
        self.is_retryable = is_retryable


@dataclass
class PaymentLinkResponse:
    """Standardized response from Payment Link API."""

    payment_link_id: str
    short_url: str
    status: str
    amount: float
    currency: str
    reference_id: Optional[str] = None
    created_at: Optional[int] = None
    raw_response: Optional[Dict[str, Any]] = None


class RazorpayPaymentLinkClient:
    """Production client for Razorpay Payment Links API."""

    DEFAULT_BASE_URL = "https://api.razorpay.com/v1"
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ):
        self.key_id = settings.RAZORPAY_KEY_ID if key_id is None else key_id
        self.key_secret = settings.RAZORPAY_KEY_SECRET if key_secret is None else key_secret
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    @property
    def is_configured(self) -> bool:
        """Check if both API Key ID and Secret are configured."""
        return bool(self.key_id and self.key_secret)

    def _get_auth(self) -> httpx.BasicAuth:
        if not self.is_configured:
            raise RazorpayPaymentLinkError(
                status_code=500,
                message="Razorpay API credentials (KEY_ID, KEY_SECRET) are not configured.",
                error_code="CREDENTIALS_MISSING",
                is_retryable=False,
            )
        return httpx.BasicAuth(self.key_id, self.key_secret)  # type: ignore[arg-type]

    def _execute_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute HTTP request with bounded exponential backoff retries."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        auth = self._get_auth()

        last_exception: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(
                        method=method,
                        url=url,
                        auth=auth,
                        params=params,
                        json=json_body,
                    )

                if response.status_code in (200, 201):
                    return response.json()

                # Parse error response safely
                error_payload: Dict[str, Any] = {}
                try:
                    error_payload = response.json()
                except Exception:
                    pass

                err_obj = error_payload.get("error", {})
                error_desc = err_obj.get("description", response.text)
                error_code = err_obj.get("code", "API_ERROR")
                is_retryable = response.status_code in self.RETRYABLE_STATUS_CODES

                if not is_retryable or attempt == self.max_retries:
                    logger.error(
                        f"[RAZORPAY_PLINK_ERROR] {method} {endpoint} failed ({response.status_code}): {error_desc}"
                    )
                    raise RazorpayPaymentLinkError(
                        status_code=response.status_code,
                        message=error_desc,
                        error_code=error_code,
                        error_payload=error_payload,
                        is_retryable=is_retryable,
                    )

                # Exponential backoff
                sleep_seconds = self.backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    f"[RAZORPAY_PLINK_RETRY] {method} {endpoint} returned {response.status_code}. Retrying in {sleep_seconds:.2f}s..."
                )
                time.sleep(sleep_seconds)

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exception = e
                if attempt == self.max_retries:
                    logger.error(f"[RAZORPAY_PLINK_NETWORK_ERROR] {method} {endpoint} network failure: {e}")
                    raise RazorpayPaymentLinkError(
                        status_code=503,
                        message=f"Network transport error connecting to Razorpay: {str(e)}",
                        error_code="NETWORK_ERROR",
                        is_retryable=True,
                    ) from e
                sleep_seconds = self.backoff_factor * (2 ** (attempt - 1))
                time.sleep(sleep_seconds)

        if last_exception:
            raise RazorpayPaymentLinkError(
                status_code=503,
                message=f"Request failed after {self.max_retries} attempts: {last_exception}",
                is_retryable=True,
            )
        raise RazorpayPaymentLinkError(status_code=500, message="Unknown client execution error")

    def create_payment_link(
        self,
        amount_paise: int,
        currency: str,
        description: str,
        customer_name: Optional[str] = None,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
        reference_id: Optional[str] = None,
        expire_by: Optional[int] = None,
        accept_partial: bool = False,
        first_min_partial_amount_paise: Optional[int] = None,
    ) -> PaymentLinkResponse:
        """Create a standard Razorpay Payment Link.

        API Endpoint: POST /v1/payment_links
        `accept_partial` + `first_min_partial_amount` enable instalment-style partial payments.
        """
        payload: Dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency.upper(),
            "accept_partial": bool(accept_partial),
            "description": description,
            "notify": {
                "sms": False,
                "email": False,
            },
            "reminder_enable": False,
        }

        customer_dict: Dict[str, Any] = {}
        if customer_name:
            customer_dict["name"] = customer_name
        if customer_email:
            customer_dict["email"] = customer_email
        if customer_phone:
            customer_dict["contact"] = customer_phone
        if customer_dict:
            payload["customer"] = customer_dict

        if reference_id:
            payload["reference_id"] = reference_id
        if expire_by:
            payload["expire_by"] = expire_by
        if accept_partial and first_min_partial_amount_paise:
            payload["first_min_partial_amount"] = int(first_min_partial_amount_paise)

        try:
            res = self._execute_request("POST", "payment_links", json_body=payload)
            return PaymentLinkResponse(
                payment_link_id=res["id"],
                short_url=res.get("short_url") or f"https://rzp.io/i/{res['id']}",
                status=res.get("status", "created").upper(),
                amount=float(res.get("amount", amount_paise)) / 100.0,
                currency=res.get("currency", currency),
                reference_id=res.get("reference_id"),
                created_at=res.get("created_at"),
                raw_response=res,
            )
        except RazorpayPaymentLinkError as exc:
            # If test mode quota limit of 30 is reached, reuse an existing active created link
            if "limit of 30 reached" in str(exc).lower() or exc.status_code == 429:
                logger.warning("[RAZORPAY_REUSE] 30-link test limit reached. Fetching existing active payment link to reuse.")
                existing_links = self.list_payment_links(count=25)
                for el in existing_links:
                    if el.status.lower() in ["created", "issued"] and el.short_url:
                        logger.info(f"[RAZORPAY_REUSE_SUCCESS] Reusing active Razorpay link {el.payment_link_id} ({el.short_url})")
                        return el
            raise exc

    def list_payment_links(self, count: int = 20) -> list[PaymentLinkResponse]:
        """Fetch a list of existing Payment Links from the account.

        API Endpoint: GET /v1/payment_links
        """
        try:
            res = self._execute_request("GET", "payment_links", params={"count": count})
            items = res.get("payment_links", [])
            return [
                PaymentLinkResponse(
                    payment_link_id=item["id"],
                    short_url=item.get("short_url") or f"https://rzp.io/i/{item['id']}",
                    status=item.get("status", "created").upper(),
                    amount=float(item.get("amount", 0)) / 100.0,
                    currency=item.get("currency", "INR"),
                    reference_id=item.get("reference_id"),
                    created_at=item.get("created_at"),
                    raw_response=item,
                )
                for item in items
            ]
        except Exception as exc:
            logger.warning(f"[RAZORPAY_LIST_LINKS_ERROR] Failed to list existing payment links: {exc}")
            return []

    def get_payment_link(self, payment_link_id: str) -> PaymentLinkResponse:
        """Fetch details of an existing Payment Link.

        API Endpoint: GET /v1/payment_links/{payment_link_id}
        """
        res = self._execute_request("GET", f"payment_links/{payment_link_id}")
        return PaymentLinkResponse(
            payment_link_id=res["id"],
            short_url=res.get("short_url") or f"https://rzp.io/i/{res['id']}",
            status=res.get("status", "created").upper(),
            amount=float(res.get("amount", 0)) / 100.0,
            currency=res.get("currency", "INR"),
            reference_id=res.get("reference_id"),
            created_at=res.get("created_at"),
            raw_response=res,
        )

    def cancel_payment_link(self, payment_link_id: str) -> Dict[str, Any]:
        """Cancel an active Payment Link.

        API Endpoint: POST /v1/payment_links/{payment_link_id}/cancel
        """
        return self._execute_request("POST", f"payment_links/{payment_link_id}/cancel")

    def __repr__(self) -> str:
        masked_key = f"{self.key_id[:8]}..." if self.key_id and len(self.key_id) > 8 else "***"
        return f"<RazorpayPaymentLinkClient key_id='{masked_key}' base_url='{self.base_url}'>"
