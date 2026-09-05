"""Webhook ingestion endpoints for payment gateways."""
import json
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.integrations.razorpay.security import verify_razorpay_signature
from app.integrations.razorpay.adapter import RazorpayAdapter
from app.services.event_processor import EventProcessor
from app.schemas.event import WebhookProcessingResult

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/razorpay",
    response_model=WebhookProcessingResult,
    status_code=status.HTTP_200_OK,
    summary="Razorpay Webhook Endpoint",
    description="Ingests, cryptographically verifies, normalizes, and processes Razorpay webhook events.",
)
async def razorpay_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_razorpay_signature: Optional[str] = Header(None, alias="X-Razorpay-Signature"),
    x_razorpay_event_id: Optional[str] = Header(None, alias="x-razorpay-event-id"),
) -> WebhookProcessingResult:
    """Handle incoming Razorpay webhook events.

    1. Reads raw binary request body.
    2. Verifies cryptographic HMAC-SHA256 signature BEFORE parsing JSON.
    3. Normalizes payload into internal canonical event schema.
    4. Idempotently processes and persists event & recovery state.
    """
    # 1. Check required headers
    if not x_razorpay_signature:
        logger.warning("[WEBHOOK_REJECTED] Missing X-Razorpay-Signature header.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required header: X-Razorpay-Signature",
        )

    # 2. Check secret configuration
    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not webhook_secret:
        logger.error("[WEBHOOK_ERROR] RAZORPAY_WEBHOOK_SECRET is not configured on server.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook verification secret is not configured on server.",
        )

    # 3. Read raw request body
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body cannot be empty.",
        )

    # 4. Verify cryptographic signature BEFORE JSON parsing
    is_valid = verify_razorpay_signature(raw_body, x_razorpay_signature, webhook_secret)
    if not is_valid:
        logger.warning("[WEBHOOK_REJECTED] Invalid signature provided for Razorpay webhook.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    # 5. Parse JSON payload
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        logger.warning(f"[WEBHOOK_MALFORMED] Failed to parse JSON payload: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON body.",
        )

    # 6. Normalize via adapter
    normalized_event = RazorpayAdapter.normalize(
        payload=payload,
        event_id_header=x_razorpay_event_id,
    )

    # 7. Process event through core service
    result = EventProcessor.process_normalized_event(db=db, event=normalized_event)
    return result
