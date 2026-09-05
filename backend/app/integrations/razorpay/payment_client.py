"""Dedicated Razorpay Payments REST API Client.

Handles communication with Razorpay Payments API (GET /v1/payments) with
safe basic authentication, pagination, and bounded exponential backoff retries.
"""
import logging
import time
from typing import Any, Dict, Optional
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class RazorpayClientError(Exception):
    """Base exception for Razorpay HTTP API errors."""

    def __init__(self, status_code: int, message: str, error_payload: Optional[Dict[str, Any]] = None):
        super().__init__(f"Razorpay API Error [{status_code}]: {message}")
        self.status_code = status_code
        self.message = message
        self.error_payload = error_payload or {}


class RazorpayPaymentClient:
    """Official Razorpay Payments API Client."""

    DEFAULT_BASE_URL = "https://api.razorpay.com/v1"
    MAX_PAGE_COUNT = 100  # Official Razorpay maximum count per request

    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self.key_id = settings.RAZORPAY_KEY_ID if key_id is None else key_id
        self.key_secret = settings.RAZORPAY_KEY_SECRET if key_secret is None else key_secret
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def is_configured(self) -> bool:
        """Check whether API credentials are present."""
        return bool(self.key_id and self.key_secret)

    def _get_auth(self) -> httpx.BasicAuth:
        if not self.is_configured:
            raise RazorpayClientError(
                status_code=500,
                message="Razorpay API credentials (KEY_ID, KEY_SECRET) are missing or not configured.",
            )
        return httpx.BasicAuth(self.key_id, self.key_secret)  # type: ignore[arg-type]

    def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute HTTP request with bounded exponential backoff retries for transient 5xx/network errors."""
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
                        json=json_data,
                    )

                # If successful, return JSON
                if response.status_code == 200:
                    return response.json()

                # Handle 4xx Client Errors (Non-retryable)
                if 400 <= response.status_code < 500:
                    try:
                        err_body = response.json()
                        desc = err_body.get("error", {}).get("description", response.text)
                    except Exception:
                        err_body = {}
                        desc = response.text
                    logger.error(
                        f"[RAZORPAY_API_ERROR] {method} {endpoint} -> HTTP {response.status_code}: {desc}"
                    )
                    raise RazorpayClientError(
                        status_code=response.status_code,
                        message=desc,
                        error_payload=err_body,
                    )

                # For 5xx Server Errors, log and attempt retry
                logger.warning(
                    f"[RAZORPAY_API_RETRY] Attempt {attempt}/{self.max_retries} failed with HTTP {response.status_code}"
                )
                last_exception = RazorpayClientError(
                    status_code=response.status_code,
                    message=f"Server returned HTTP {response.status_code}",
                )

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning(
                    f"[RAZORPAY_NETWORK_RETRY] Attempt {attempt}/{self.max_retries} failed: {exc}"
                )
                last_exception = exc

            if attempt < self.max_retries:
                backoff_time = 0.5 * (2 ** (attempt - 1))  # 0.5s, 1.0s, 2.0s
                time.sleep(backoff_time)

        if isinstance(last_exception, RazorpayClientError):
            raise last_exception
        raise RazorpayClientError(
            status_code=504,
            message=f"Razorpay API request failed after {self.max_retries} attempts: {last_exception}",
        )

    def fetch_payments(
        self,
        from_timestamp: Optional[int] = None,
        to_timestamp: Optional[int] = None,
        count: int = 100,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """Fetch paginated payments list from Razorpay.

        API Endpoint: GET /v1/payments
        Query Parameters:
            from: Timestamp in epoch seconds
            to: Timestamp in epoch seconds
            count: Number of records to return (max 100)
            skip: Number of records to skip (offset)
        """
        clamped_count = min(max(1, count), self.MAX_PAGE_COUNT)
        params: Dict[str, Any] = {
            "count": clamped_count,
            "skip": skip,
        }
        if from_timestamp is not None:
            params["from"] = from_timestamp
        if to_timestamp is not None:
            params["to"] = to_timestamp

        logger.debug(f"[RAZORPAY_CLIENT] Fetching payments: count={clamped_count}, skip={skip}")
        return self._request_with_retry(method="GET", endpoint="payments", params=params)

    def fetch_payment(self, payment_id: str) -> Dict[str, Any]:
        """Fetch individual payment entity by Razorpay payment ID (e.g. 'pay_xxx').

        API Endpoint: GET /v1/payments/{payment_id}
        """
        if not payment_id:
            raise ValueError("payment_id cannot be empty")
        return self._request_with_retry(method="GET", endpoint=f"payments/{payment_id}")
