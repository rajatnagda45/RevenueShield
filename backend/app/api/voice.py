"""Voice Recovery API Endpoints for Twilio outbound calling and webhooks."""
import logging
from typing import Any, Dict, Optional
import uuid
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services.voice_recovery_service import VoiceRecoveryService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Twilio Voice Recovery"])


class VoiceRecoveryRequest(BaseModel):
    """Request payload for triggering an outbound voice recovery call."""
    dry_run: Optional[bool] = Field(default=None, description="Simulate call without real Twilio dispatch")


class VoiceRecoveryResponse(BaseModel):
    """Response payload containing case ID, call SID, status, and provider."""
    model_config = ConfigDict(populate_by_name=True)

    case_id: str = Field(..., description="Recovery Case UUID")
    call_sid: str = Field(..., description="Twilio Call SID")
    status: str = Field(..., description="Call status (e.g. QUEUED, RINGING)")
    provider: str = Field(default="TWILIO", description="Voice provider name")
    voice_call_id: Optional[str] = Field(default=None, description="Internal VoiceCall record UUID")


class VoiceTestCallRequest(BaseModel):
    """Request payload for triggering an outbound voice smoke test call."""
    phone_number: str = Field(..., description="Recipient phone number in E.164 format (e.g. +919876543210)")


class VoiceTestCallResponse(BaseModel):
    """Response payload containing Twilio call SID and status."""
    call_sid: str = Field(..., description="Twilio Call SID")
    status: str = Field(..., description="Twilio call status (e.g. queued, ringing, in-progress)")


@router.post(
    "/recovery-cases/{case_id}/voice-recovery",
    response_model=VoiceRecoveryResponse,
    status_code=status.HTTP_200_OK,
    summary="Initiate personalized Twilio voice recovery call",
)
def trigger_case_voice_recovery(
    case_id: uuid.UUID,
    payload: Optional[VoiceRecoveryRequest] = None,
    request: Request = None,
    db: Session = Depends(get_db),
) -> VoiceRecoveryResponse:
    """Validate recovery case eligibility, generate personalized VoiceCall record, and dispatch Twilio call."""
    try:
        base_url = str(request.base_url) if request else None
        dry_run = payload.dry_run if payload else None

        res = VoiceRecoveryService.start_recovery_call(
            db=db,
            case_id=case_id,
            dry_run=dry_run,
            webhook_base_url=base_url,
        )

        return VoiceRecoveryResponse(
            case_id=res["case_id"],
            call_sid=res["call_sid"],
            status=res["status"],
            provider=res.get("provider", "TWILIO"),
            voice_call_id=res.get("voice_call_id"),
        )
    except ValueError as exc:
        err_msg = str(exc)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        logger.exception(f"[VOICE_RECOVERY_API_ERROR] Unexpected error starting recovery call for case {case_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred while initiating voice recovery call.",
        )


@router.api_route(
    "/webhooks/twilio/voice/{call_id}",
    methods=["GET", "POST"],
    summary="Fetch personalized English TwiML response for answered Twilio call",
)
async def twilio_voice_twiml_webhook(
    call_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
) -> Response:
    """Dynamically render personalized English TwiML XML greeting for answered recovery call."""
    # Read form/query params
    if request.method == "POST":
        try:
            form_data = await request.form()
            params = dict(form_data)
        except Exception:
            params = {}
    else:
        params = dict(request.query_params)

    # Optional signature check
    if x_twilio_signature:
        url = str(request.url)
        is_valid = VoiceRecoveryService.validate_twilio_webhook_signature(
            signature=x_twilio_signature,
            url=url,
            params=params,
        )
        if not is_valid:
            logger.warning(f"[TWILIO_WEBHOOK_REJECTED] Invalid X-Twilio-Signature for URL: {url}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")

    try:
        twiml_content = VoiceRecoveryService.generate_recovery_twiml(db=db, call_id=call_id)
        return Response(content=twiml_content, media_type="application/xml")
    except ValueError as exc:
        logger.warning(f"[TWILIO_TWIML_NOT_FOUND] Error rendering TwiML for call_id {call_id}: {exc}")
        fallback_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="en-IN" voice="Polly.Aditi">Hello. This is a payment reminder from RevenueShield. Please check your registered email to complete your pending payment. Thank you.</Say>
    <Hangup/>
</Response>"""
        return Response(content=fallback_xml, media_type="application/xml")

@router.api_route(
    "/webhooks/twilio/test-voice",
    methods=["GET", "POST"],
    summary="Dynamic TwiML endpoint for test calls with Gather speech recognition",
)
async def twilio_test_voice_twiml(
    db: Session = Depends(get_db),
) -> Response:
    """Render dynamic English TwiML with Gather speech recognition for test calls."""
    try:
        from app.models.customer import Customer
        from sqlalchemy import select
        cust = db.scalar(select(Customer).where(Customer.phone == "+917991142735"))
        customer_name = cust.name if cust and cust.name else "Ankit Kumar"
    except Exception:
        customer_name = "Ankit Kumar"

    base_url = settings.TWILIO_WEBHOOK_BASE_URL or ""
    gather_url = f"{base_url}/webhooks/twilio/test-voice/gather" if base_url else "/webhooks/twilio/test-voice/gather"

    twiml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="{gather_url}" method="POST" speechTimeout="auto" language="en-IN">
        <Say language="en-IN" voice="Polly.Aditi">Namaste! This is Aditi, calling from RevenueShield's Autonomous Recovery Team for ByteScale Software. This is an important payment reminder regarding your pending subscription payment of 10,000 Rupees for invoice INV-9821.</Say>
        <Pause length="1"/>
        <Say language="en-IN" voice="Polly.Aditi">Could you please confirm if you will be able to complete this payment by this Friday, so we can record your promise to pay?</Say>
    </Gather>
    <Say language="en-IN" voice="Polly.Aditi">Thank you. A direct Razorpay payment link of 10,000 Rupees has also been sent to your registered email kdmspokharahan@gmail.com. Have a wonderful day!</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml_content.strip(), media_type="application/xml")


@router.post(
    "/webhooks/twilio/voice/{call_id}/gather",
    summary="Handle Twilio Gather Speech Recognition response for RecoveryCase",
)
async def twilio_voice_gather_webhook(
    call_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
) -> Response:
    """Process speech transcript from Twilio Gather, extract date, record PromiseToPay, and return confirmation XML."""
    import urllib.parse
    payload: Dict[str, Any] = {}
    body_bytes = await request.body()
    if body_bytes:
        try:
            import json
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            try:
                parsed_form = urllib.parse.parse_qs(body_bytes.decode("utf-8", errors="ignore"))
                for k, v in parsed_form.items():
                    payload[k] = v[0] if len(v) == 1 else v
            except Exception:
                pass

    if not payload:
        try:
            form_data = await request.form()
            if form_data:
                payload.update(dict(form_data))
        except Exception:
            pass

    if not payload:
        payload.update(dict(request.query_params))

    # Optional signature check
    if x_twilio_signature:
        url = str(request.url)
        is_valid = VoiceRecoveryService.validate_twilio_webhook_signature(
            signature=x_twilio_signature,
            url=url,
            params=payload,
        )
        if not is_valid:
            logger.warning(f"[TWILIO_GATHER_REJECTED] Invalid signature for URL: {url}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")

    twiml_content = VoiceRecoveryService.handle_voice_gather_response(
        db=db,
        call_id=call_id,
        payload=payload,
    )
    return Response(content=twiml_content, media_type="application/xml")


@router.post(
    "/webhooks/twilio/test-voice/gather",
    summary="Handle Twilio Gather Speech Recognition response for test calls",
)
async def twilio_test_voice_gather_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Process speech transcript from test call and return confirmation XML."""
    import urllib.parse
    payload: Dict[str, Any] = {}
    body_bytes = await request.body()
    if body_bytes:
        try:
            import json
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            try:
                parsed_form = urllib.parse.parse_qs(body_bytes.decode("utf-8", errors="ignore"))
                for k, v in parsed_form.items():
                    payload[k] = v[0] if len(v) == 1 else v
            except Exception:
                pass

    if not payload:
        try:
            form_data = await request.form()
            if form_data:
                payload.update(dict(form_data))
        except Exception:
            pass

    if not payload:
        payload.update(dict(request.query_params))

    twiml_content = VoiceRecoveryService.handle_test_voice_gather_response(
        db=db,
        payload=payload,
    )
    return Response(content=twiml_content, media_type="application/xml")


@router.post(
    "/webhooks/twilio/status",
    summary="Handle Twilio Call Status lifecycle callbacks",
)
async def twilio_call_status_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
) -> Dict[str, str]:
    import urllib.parse

    payload: Dict[str, Any] = {}
    body_bytes = await request.body()
    if body_bytes:
        try:
            import json
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            try:
                parsed_form = urllib.parse.parse_qs(body_bytes.decode("utf-8", errors="ignore"))
                for k, v in parsed_form.items():
                    payload[k] = v[0] if len(v) == 1 else v
            except Exception:
                pass

    if not payload:
        try:
            form_data = await request.form()
            if form_data:
                payload.update(dict(form_data))
        except Exception:
            pass

    if not payload:
        payload.update(dict(request.query_params))

    # Optional signature check
    if x_twilio_signature:
        url = str(request.url)
        is_valid = VoiceRecoveryService.validate_twilio_webhook_signature(
            signature=x_twilio_signature,
            url=url,
            params=payload,
        )
        if not is_valid:
            logger.warning(f"[TWILIO_STATUS_REJECTED] Invalid X-Twilio-Signature for URL: {url}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")

    VoiceRecoveryService.handle_status_callback(db=db, payload=payload)
    return {"status": "received"}


@router.post(
    "/voice/test-call",
    response_model=VoiceTestCallResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger outbound Twilio voice smoke test call",
)
def trigger_voice_test_call(
    payload: VoiceTestCallRequest,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> VoiceTestCallResponse:
    """Validate phone number and initiate an outbound Twilio voice test call."""
    if settings.ENVIRONMENT == "production":
        if not settings.INTERNAL_API_SECRET or x_internal_secret != settings.INTERNAL_API_SECRET:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Test call endpoint is restricted in production environment.",
            )

    try:
        res = VoiceRecoveryService.start_test_call(
            phone_number=payload.phone_number,
        )
        return VoiceTestCallResponse(
            call_sid=res["call_sid"],
            status=res["status"],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )
    except Exception as exc:
        logger.exception(f"[VOICE_TEST_CALL_ERROR] Unexpected error in trigger_voice_test_call: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred while initiating voice test call.",
        )
