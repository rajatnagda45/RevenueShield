"""Admin endpoints for Razorpay historical synchronization and data quality monitoring."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.sync_checkpoint import SyncCheckpoint
from app.services.razorpay_sync_service import RazorpayPaymentSyncService, SyncResult

logger = logging.getLogger(__name__)

router = APIRouter()


class SyncPaymentsRequest(BaseModel):
    """Request schema for triggering a historical Razorpay payment sync."""

    model_config = ConfigDict(populate_by_name=True)

    from_timestamp: Optional[str] = Field(
        None,
        alias="from",
        description="Start timestamp in ISO 8601 string format (e.g. 2026-08-01T00:00:00Z) or epoch seconds.",
    )
    to_timestamp: Optional[str] = Field(
        None,
        alias="to",
        description="End timestamp in ISO 8601 string format (e.g. 2026-08-22T23:59:59Z) or epoch seconds.",
    )
    batch_size: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Number of records to fetch per paginated API request (max 100).",
    )


class SyncPaymentsResponse(BaseModel):
    """Structured response schema for payment synchronization."""

    model_config = ConfigDict(populate_by_name=True)

    sync_id: str = Field(..., description="UUID of the sync checkpoint execution record")
    status: str = Field(..., description="Sync status (SUCCEEDED, PARTIAL, FAILED)")
    records_fetched: int = Field(..., description="Total payment records fetched from Razorpay API")
    records_created: int = Field(..., description="New Payment rows created in PostgreSQL")
    records_updated: int = Field(..., description="Existing Payment rows updated with new metadata")
    from_timestamp: Optional[str] = Field(None, alias="from", description="Applied start timestamp")
    to_timestamp: Optional[str] = Field(None, alias="to", description="Applied end timestamp")
    started_at: str = Field(..., description="Sync start timestamp")
    completed_at: Optional[str] = Field(None, description="Sync completion timestamp")
    error_message: Optional[str] = Field(None, description="Error message if sync failed")


class DataQualitySummaryResponse(BaseModel):
    """Aggregated metrics representing payment data quality and ingestion health."""

    total_payments: int = Field(..., description="Total payments stored in PostgreSQL")
    successful_payments: int = Field(..., description="Payments in CAPTURED state")
    failed_payments: int = Field(..., description="Payments in FAILED state")
    unknown_status_payments: int = Field(..., description="Payments in non-standard or unknown status")
    total_amount: float = Field(..., description="Total monetary sum across all payments")
    failed_amount: float = Field(..., description="Total monetary sum of failed payments")
    captured_amount: float = Field(..., description="Total monetary sum of successfully captured payments")
    last_sync_time: Optional[str] = Field(None, description="Timestamp of the most recent successful API sync")
    last_webhook_time: Optional[str] = Field(None, description="Timestamp of the most recent webhook event")


class SyncCheckpointItem(BaseModel):
    """Sync checkpoint ledger record."""

    id: str
    source: str
    started_at: str
    completed_at: Optional[str]
    from_timestamp: Optional[str]
    to_timestamp: Optional[str]
    records_fetched: int
    records_created: int
    records_updated: int
    status: str
    error_message: Optional[str]


def parse_timestamp_param(val: Optional[str]) -> Optional[datetime]:
    """Safely parse epoch integer or ISO 8601 string to timezone-aware UTC datetime."""
    if not val:
        return None
    val_str = str(val).strip()
    # Check if numeric epoch seconds
    if val_str.isdigit():
        return datetime.fromtimestamp(int(val_str), tz=timezone.utc)
    try:
        # Try float epoch
        epoch = float(val_str)
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except ValueError:
        pass
    try:
        # Try ISO format
        dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date/timestamp format: '{val}'. Use ISO 8601 (e.g. 2026-08-01T00:00:00Z) or epoch seconds.",
        )


@router.post(
    "/sync/payments",
    response_model=SyncPaymentsResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger Razorpay Historical Payment Sync",
    description="Synchronize payment records from the Razorpay Payments API for a specified date range.",
)
def sync_razorpay_payments(
    payload: SyncPaymentsRequest,
    db: Session = Depends(get_db),
) -> SyncPaymentsResponse:
    """Execute paginated synchronization from Razorpay API into internal normalized tables."""
    from_dt = parse_timestamp_param(payload.from_timestamp)
    to_dt = parse_timestamp_param(payload.to_timestamp)

    try:
        result: SyncResult = RazorpayPaymentSyncService.sync_payments(
            db=db,
            from_dt=from_dt,
            to_dt=to_dt,
            batch_size=payload.batch_size,
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )
    except Exception as exc:
        logger.error(f"[ADMIN_SYNC_ERROR] Sync execution failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Razorpay synchronization failed: {str(exc)}",
        )

    return SyncPaymentsResponse(
        sync_id=result.sync_id,
        status=result.status,
        records_fetched=result.records_fetched,
        records_created=result.records_created,
        records_updated=result.records_updated,
        from_timestamp=result.from_timestamp,
        to_timestamp=result.to_timestamp,
        started_at=result.started_at,
        completed_at=result.completed_at,
        error_message=result.error_message,
    )


@router.get(
    "/sync/checkpoints",
    response_model=List[SyncCheckpointItem],
    status_code=status.HTTP_200_OK,
    summary="List Sync Checkpoints",
    description="Retrieve recent synchronization run history and execution metrics.",
)
def list_sync_checkpoints(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> List[SyncCheckpointItem]:
    """Fetch recent sync checkpoint entries."""
    checkpoints = db.scalars(
        select(SyncCheckpoint)
        .order_by(SyncCheckpoint.started_at.desc())
        .limit(limit)
    ).all()

    return [
        SyncCheckpointItem(
            id=str(cp.id),
            source=cp.source,
            started_at=cp.started_at.isoformat(),
            completed_at=cp.completed_at.isoformat() if cp.completed_at else None,
            from_timestamp=cp.from_timestamp.isoformat() if cp.from_timestamp else None,
            to_timestamp=cp.to_timestamp.isoformat() if cp.to_timestamp else None,
            records_fetched=cp.records_fetched,
            records_created=cp.records_created,
            records_updated=cp.records_updated,
            status=cp.status,
            error_message=cp.error_message,
        )
        for cp in checkpoints
    ]


@router.get(
    "/sync/data-quality",
    response_model=DataQualitySummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Payment Ingestion Data Quality Metrics",
    description="Retrieve aggregate metrics on stored payments, statuses, amounts, and latest synchronization checkpoints.",
)
def get_data_quality_metrics(
    db: Session = Depends(get_db),
) -> DataQualitySummaryResponse:
    """Fetch aggregate data quality dashboard metrics."""
    summary = RazorpayPaymentSyncService.get_data_quality_summary(db)
    return DataQualitySummaryResponse(**summary)


@router.post(
    "/seed-demo-data",
    status_code=status.HTTP_200_OK,
    summary="Seed Realistic Demonstration Data",
    description="Populates the database with rich multi-customer cases, 30-day recovery trends, PTP commitments, and channel metrics.",
)
def seed_demo_data_endpoint(
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Execute comprehensive realistic demo data seeding."""
    from app.scripts_runner import run_demo_seeder
    result = run_demo_seeder(db)
    return {"status": "success", "message": "Demo data successfully seeded.", "summary": result}


@router.post(
    "/trigger-live-outreach",
    status_code=status.HTTP_200_OK,
    summary="Trigger Live Voice Call Outreach",
    description="Initiates a live outbound recovery call via Twilio to the configured demo phone number.",
)
def trigger_live_outreach(
    phone_number: str = Query(default="+917991142735"),
    email: str = Query(default="kdmspokharahan@gmail.com"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Trigger a live Twilio voice call for interactive demonstration."""
    from app.integrations.voice.twilio_client import TwilioVoiceClient
    try:
        client = TwilioVoiceClient()
        twiml = f"""<Response>
            <Say voice="Polly.Aditi" language="en-IN">Hello! This is RevenueShield's Autonomous AI Recovery Assistant calling on behalf of ByteScale Software regarding invoice number INV-9821. An outstanding payment of 12,500 Rupees was recently declined. Would you like to schedule a payment arrangement or receive a secure payment link by SMS?</Say>
            <Gather input="speech" timeout="5" speechTimeout="auto" action="/webhooks/twilio/status">
                <Say voice="Polly.Aditi" language="en-IN">Please speak your response now.</Say>
            </Gather>
        </Response>"""
        call_res = client.create_outbound_call(to_number=phone_number, twiml=twiml)
        return {
            "status": "call_initiated",
            "phone_number": phone_number,
            "call_sid": call_res.get("call_sid"),
            "provider_status": call_res.get("status"),
        }
    except Exception as e:
        logger.error(f"Failed to initiate live call: {e}")
        return {
            "status": "error",
            "message": str(e),
            "phone_number": phone_number,
        }


@router.get(
    "/call-status/{call_sid}",
    status_code=status.HTTP_200_OK,
    summary="Get Twilio Call Status",
    description="Fetches live status and any error codes from Twilio for a given call SID.",
)
def get_call_status(
    call_sid: str,
) -> Dict[str, Any]:
    """Fetch live Twilio call details."""
    from app.core.config import settings
    from twilio.rest import Client
    try:
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        call = client.calls(call_sid).fetch()
        return {
            "sid": call.sid,
            "status": call.status,
            "duration": call.duration,
            "to": call.to,
            "from": call.from_,
            "price": call.price,
            "error_code": getattr(call, "error_code", None),
            "error_message": getattr(call, "error_message", None),
        }
    except Exception as e:
        return {"error": str(e)}
