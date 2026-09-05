"""Razorpay HTTP API Client.

Handles direct HTTP transport communication with Razorpay REST APIs using
Basic Authentication. Contains NO domain business logic or RecoveryCase logic.
"""
from typing import Any, Dict, Optional
import httpx
from app.core.config import settings


class RazorpayAPIError(Exception):
    """Exception raised when a Razorpay API request fails."""

    def __init__(self, status_code: int, message: str, error_payload: Optional[Dict[str, Any]] = None):
        super().__init__(f"Razorpay API error ({status_code}): {message}")
        self.status_code = status_code
        self.message = message
        self.error_payload = error_payload or {}


class RazorpayClient:
    """Client for interacting with official Razorpay REST APIs."""

    DEFAULT_BASE_URL = "https://api.razorpay.com/v1"

    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.key_id = key_id or settings.RAZORPAY_KEY_ID
        self.key_secret = key_secret or settings.RAZORPAY_KEY_SECRET
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        """Check if both API Key ID and Secret are provided."""
        return bool(self.key_id and self.key_secret)

    def _get_auth(self) -> httpx.BasicAuth:
        if not self.is_configured:
            raise RazorpayAPIError(
                status_code=500,
                message="Razorpay API credentials (KEY_ID, KEY_SECRET) are not configured.",
            )
        return httpx.BasicAuth(self.key_id, self.key_secret)  # type: ignore[arg-type]

    async def get_payment(self, payment_id: str) -> Dict[str, Any]:
        """Fetch payment details by Razorpay payment ID (e.g. 'pay_xxx').

        API Endpoint: GET /v1/payments/{payment_id}
        """
        url = f"{self.base_url}/payments/{payment_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, auth=self._get_auth())
            if response.status_code != 200:
                error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                raise RazorpayAPIError(
                    status_code=response.status_code,
                    message=error_data.get("error", {}).get("description", response.text),
                    error_payload=error_data,
                )
            return response.json()

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Fetch order details by Razorpay order ID (e.g. 'order_xxx').

        API Endpoint: GET /v1/orders/{order_id}
        """
        url = f"{self.base_url}/orders/{order_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, auth=self._get_auth())
            if response.status_code != 200:
                error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                raise RazorpayAPIError(
                    status_code=response.status_code,
                    message=error_data.get("error", {}).get("description", response.text),
                    error_payload=error_data,
                )
            return response.json()

    def create_payment_link_sync(
        self,
        amount_paise: int,
        currency: str,
        description: str,
        customer_name: Optional[str] = None,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
        reference_id: Optional[str] = None,
        expire_by: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a Razorpay standard Payment Link.

        API Endpoint: POST /v1/payment_links
        """
        url = f"{self.base_url}/payment_links"
        payload: Dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency.upper(),
            "accept_partial": False,
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

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload, auth=self._get_auth())
            if response.status_code not in [200, 201]:
                error_data = (
                    response.json()
                    if response.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                raise RazorpayAPIError(
                    status_code=response.status_code,
                    message=error_data.get("error", {}).get("description", response.text),
                    error_payload=error_data,
                )
            return response.json()

    def __repr__(self) -> str:
        masked_key = f"{self.key_id[:8]}..." if self.key_id and len(self.key_id) > 8 else "***"
        return f"<RazorpayClient key_id='{masked_key}' base_url='{self.base_url}'>"
