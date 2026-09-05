"""FastAPI routes for Real Twilio WhatsApp recovery outreach, previews, webhooks, and dashboard metrics."""
from datetime import datetime
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.recovery_case import RecoveryCase
from app.models.customer import Customer
from app.models.communication import Communication
from app.models.audit_log import AuditLog
from app.services.communication_orchestrator import CommunicationOrchestrator
from app.services.whatsapp_recovery_service import WhatsAppRecoveryService, WhatsAppRecoveryResponse
from app.services.recovery_message_generator import RecoveryMessageGenerator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WhatsApp Recovery Communications"])


class WhatsAppDispatchRequest(BaseModel):
    """Request payload to initiate WhatsApp outreach."""

    language: str = Field(default="ENGLISH", description="Communication language: 'ENGLISH' or 'HINGLISH'")
    dry_run: Optional[bool] = Field(default=None, description="Explicit dry-run override")
    evaluation_time: Optional[datetime] = Field(default=None, description="Optional evaluation timestamp for tests")


class WhatsAppRecoveryActionResponse(BaseModel):
    """Standardized response from POST /recovery-cases/{case_id}/whatsapp-recovery."""

    case_id: str
    action: str
    status: str
    payment_link: Dict[str, Any]
    communication: Dict[str, Any]
    reason: Optional[str] = None
    policy_blocking_rule: Optional[str] = None


class WhatsAppPreviewResponse(BaseModel):
    """Response returned from preview endpoint without external side-effects."""

    case_id: str
    channel: str = "WHATSAPP"
    language: str
    template_name: Optional[str] = None
    template_version: Optional[str] = None
    recipient_masked: str
    message: str
    payment_link: str
    policy_status: str
    policy_reasons: List[str]
    policy_blocking_rule: Optional[str] = None
    attempt_number: int
    max_attempts: int
    next_eligible_at: Optional[datetime] = None


class TimelineEvent(BaseModel):
    """Event in the recovery timeline."""

    action: str
    timestamp: str
    actor: str
    metadata: Dict[str, Any]


class WhatsAppDashboardMetrics(BaseModel):
    """Aggregated WhatsApp performance and recovery metrics with timeline."""

    whatsapp_messages_sent: int
    whatsapp_messages_delivered: int
    whatsapp_messages_failed: int
    whatsapp_messages_blocked: int
    whatsapp_recovery_rate: float
    revenue_recovered_from_whatsapp: float
    current_provider_mode: str
    timeline: List[TimelineEvent] = []


@router.post(
    "/recovery-cases/{case_id}/whatsapp-recovery",
    response_model=WhatsAppRecoveryActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute real Twilio WhatsApp recovery outreach",
)
def execute_whatsapp_recovery_endpoint(
    case_id: uuid.UUID,
    payload: WhatsAppDispatchRequest = WhatsAppDispatchRequest(),
    db: Session = Depends(get_db),
):
    """Validate case, run policy, create/reuse Razorpay link, and dispatch via Twilio WhatsApp Sandbox."""
    try:
        res: WhatsAppRecoveryResponse = WhatsAppRecoveryService.execute_recovery(
            db=db,
            recovery_case_id=case_id,
            language=payload.language,
            dry_run=payload.dry_run,
            reference_time=payload.evaluation_time,
        )
        return WhatsAppRecoveryActionResponse(
            case_id=res.case_id,
            action=res.action,
            status=res.status,
            payment_link=res.payment_link,
            communication=res.communication,
            reason=res.reason,
            policy_blocking_rule=res.policy_blocking_rule,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error(f"[WHATSAPP_RECOVERY_ENDPOINT_ERROR] Case={case_id} Error={str(exc)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process WhatsApp recovery: {str(exc)}",
        )


@router.get(
    "/recovery-cases/{case_id}/whatsapp-preview",
    response_model=WhatsAppPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Preview WhatsApp recovery message without calling Twilio",
)
def preview_whatsapp_recovery_endpoint(
    case_id: uuid.UUID,
    language: str = Query(default="ENGLISH", description="Communication language: 'ENGLISH' or 'HINGLISH'"),
    db: Session = Depends(get_db),
):
    """Preview WhatsApp message and policy evaluation without generating external network calls."""
    try:
        prev = WhatsAppRecoveryService.preview_recovery(
            db=db,
            recovery_case_id=case_id,
            language=language,
        )
        return WhatsAppPreviewResponse(
            case_id=prev["case_id"],
            channel=prev["channel"],
            language=prev["language"],
            template_name=prev["template_name"],
            template_version=prev["template_version"],
            recipient_masked=prev["recipient_masked"],
            message=prev["message"],
            payment_link=prev["payment_link"],
            policy_status=prev["policy_status"],
            policy_reasons=prev["policy_reasons"],
            policy_blocking_rule=prev.get("policy_blocking_rule"),
            attempt_number=prev["attempt_number"],
            max_attempts=prev["max_attempts"],
            next_eligible_at=prev["next_eligible_at"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post(
    "/recovery-cases/{case_id}/email-recovery",
    status_code=status.HTTP_200_OK,
    summary="Dispatch email recovery message with Razorpay payment link",
)
def execute_email_recovery_endpoint(
    case_id: uuid.UUID,
    recipient_email: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Execute high-converting payment recovery email outreach with real Razorpay payment link."""
    from app.services.email_recovery_service import EmailRecoveryService
    res = EmailRecoveryService.execute_recovery(
        db=db,
        case_id=str(case_id),
        recipient_email=recipient_email,
    )
    if res.get("status") == "NOT_FOUND":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=res.get("error"))
    return res


# Alias for backward compatibility
@router.post(
    "/recovery-cases/{case_id}/communications/whatsapp",
    response_model=WhatsAppRecoveryActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Dispatch WhatsApp recovery message (alias)",
)
def dispatch_whatsapp_communication_alias(
    case_id: uuid.UUID,
    payload: WhatsAppDispatchRequest = WhatsAppDispatchRequest(),
    db: Session = Depends(get_db),
):
    return execute_whatsapp_recovery_endpoint(case_id=case_id, payload=payload, db=db)


@router.get(
    "/recovery-cases/{case_id}/communications/whatsapp/preview",
    response_model=WhatsAppPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Preview WhatsApp recovery message (alias)",
)
def preview_whatsapp_communication_alias(
    case_id: uuid.UUID,
    language: str = Query(default="ENGLISH"),
    db: Session = Depends(get_db),
):
    return preview_whatsapp_recovery_endpoint(case_id=case_id, language=language, db=db)


@router.post(
    "/webhooks/whatsapp/status",
    status_code=status.HTTP_200_OK,
    summary="Receive WhatsApp delivery status callbacks",
)
async def receive_whatsapp_status_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """Receive and verify asynchronous message delivery status callbacks from WhatsApp providers."""
    body_bytes = await request.body()
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        import json
        payload = json.loads(body_bytes.decode("utf-8"))
        msg_id = payload.get("provider_message_id") or payload.get("id") or payload.get("MessageSid")
        status_val = payload.get("status") or payload.get("MessageStatus") or "UNKNOWN"
        error_msg = payload.get("error_message") or payload.get("ErrorMessage")
    else:
        form_data = await request.form()
        payload = dict(form_data)
        msg_id = payload.get("MessageSid") or payload.get("provider_message_id") or payload.get("id")
        status_val = payload.get("MessageStatus") or payload.get("status") or "UNKNOWN"
        error_msg = payload.get("ErrorMessage") or payload.get("error_message")

    if not msg_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing provider message identifier (MessageSid / provider_message_id).",
        )

    updated_comm = CommunicationOrchestrator.handle_status_webhook(
        db=db,
        provider_message_id=str(msg_id),
        status=str(status_val),
        error_reason=error_msg,
        raw_payload=payload,
    )

    if not updated_comm:
        return {"status": "ignored", "reason": "Message ID not recognized."}

    return {
        "status": "processed",
        "communication_id": str(updated_comm.id),
        "updated_status": updated_comm.status,
    }


@router.post(
    "/webhooks/whatsapp/inbound",
    status_code=status.HTTP_200_OK,
    summary="Receive inbound customer WhatsApp message and auto-reply with payment link",
)
async def receive_whatsapp_inbound_message(
    request: Request,
    db: Session = Depends(get_db),
):
    """Handle inbound WhatsApp message from customer and respond with active Razorpay payment recovery link (TwiML)."""
    from fastapi.responses import Response
    form_data = await request.form()
    sender_phone = (form_data.get("From") or "").replace("whatsapp:", "").strip()
    msg_body = form_data.get("Body", "")

    logger.info(f"[WHATSAPP_INBOUND] From={sender_phone} Body={msg_body}")

    # 1. Resolve Customer and Open Recovery Case
    customer = db.scalar(select(Customer).where(Customer.phone.contains(sender_phone[-10:])))
    case = None
    if customer:
        case = db.scalar(
            select(RecoveryCase)
            .where(RecoveryCase.customer_id == customer.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS"]))
            .order_by(RecoveryCase.created_at.desc())
        )

    # 1b. Consent: STOP / UNSUBSCRIBE / Hinglish opt-out keywords stop outreach before anything else is sent.
    if customer:
        from app.compliance.consent import ConsentService
        opt_out = ConsentService.process_inbound_text(db, customer=customer, text=msg_body, channel="WHATSAPP", case=case)
        if opt_out:
            db.commit()
            reply_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Response>\n'
                '  <Message>You have been unsubscribed from RevenueShield payment reminders. Reply START to receive them again.</Message>\n'
                '</Response>'
            )
            return Response(content=reply_xml, media_type="application/xml")
        if msg_body.strip().upper() in ("START", "RESUME", "UNSTOP"):
            ConsentService.record(db, customer=customer, channel="ALL", status="OPT_IN", source="CUSTOMER_KEYWORD", reason="Customer replied START", case=case)
            db.commit()

    if not case:
        # Generic helpful reply
        reply_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Response>\n'
            '  <Message>Hello from RevenueShield AI. We could not find an active pending payment for your account.</Message>\n'
            '</Response>'
        )
        return Response(content=reply_xml, media_type="application/xml")

    # 2. Get or Create Razorpay Payment Link
    link, _ = WhatsAppRecoveryService._get_or_create_payment_link(db=db, case=case)

    # 3. Format Personalized Reply Message
    draft = RecoveryMessageGenerator.generate(
        recovery_case=case,
        payment_link_url=link.payment_url,
        language="HINGLISH",
    )

    reply_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'  <Message>{draft.message_body}</Message>\n'
        '</Response>'
    )
    return Response(content=reply_xml, media_type="application/xml")


@router.get(
    "/admin/communications/whatsapp/dashboard",
    response_model=WhatsAppDashboardMetrics,
    status_code=status.HTTP_200_OK,
    summary="WhatsApp recovery dashboard metrics with live timeline",
)
def get_whatsapp_dashboard_metrics(
    db: Session = Depends(get_db),
):
    """Aggregated operational and business recovery metrics with live recovery timeline."""
    sent_count = db.scalar(
        select(func.count(Communication.id)).where(Communication.channel == "WHATSAPP", Communication.status == "SENT")
    ) or 0
    deliv_count = db.scalar(
        select(func.count(Communication.id)).where(Communication.channel == "WHATSAPP", Communication.status.in_(["DELIVERED", "READ"]))
    ) or 0
    failed_count = db.scalar(
        select(func.count(Communication.id)).where(Communication.channel == "WHATSAPP", Communication.status == "FAILED")
    ) or 0
    blocked_count = db.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.action == "WHATSAPP_BLOCKED")
    ) or 0

    cases_with_whatsapp = select(Communication.recovery_case_id).where(
        Communication.channel == "WHATSAPP",
        Communication.status.in_(["SENT", "DELIVERED", "READ"]),
    ).scalar_subquery()

    total_wa_cases = db.scalar(
        select(func.count(func.distinct(cases_with_whatsapp)))
    ) or 0

    recovered_wa_cases = db.scalar(
        select(func.count(func.distinct(RecoveryCase.id))).where(
            RecoveryCase.id.in_(cases_with_whatsapp),
            RecoveryCase.status == "RECOVERED",
        )
    ) or 0

    recovery_rate = (recovered_wa_cases / total_wa_cases * 100.0) if total_wa_cases > 0 else 0.0

    recovered_amount = db.scalar(
        select(func.sum(RecoveryCase.recovered_amount)).where(
            RecoveryCase.id.in_(cases_with_whatsapp),
            RecoveryCase.status == "RECOVERED",
        )
    ) or Decimal("0.00")

    # Fetch recent timeline audit entries
    timeline_logs = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action.in_([
                "RECOVERY_CASE_OPENED",
                "DIAGNOSIS_CREATED",
                "RECOVERY_ACTION_RECOMMENDED",
                "WHATSAPP_POLICY_CHECKED",
                "WHATSAPP_APPROVED",
                "PAYMENT_LINK_CREATED",
                "WHATSAPP_MESSAGE_GENERATED",
                "WHATSAPP_SENT",
                "WHATSAPP_DELIVERED",
                "PAYMENT_CAPTURED",
                "WHATSAPP_STOPPED_AFTER_RECOVERY",
                "CASE_RECOVERED",
            ])
        )
        .order_by(desc(AuditLog.timestamp))
        .limit(20)
    ).all()

    timeline_items = [
        TimelineEvent(
            action=log.action,
            timestamp=log.timestamp.isoformat() if log.timestamp else "",
            actor=f"{log.actor_type} ({log.actor_id})",
            metadata=log.audit_metadata or {},
        )
        for log in reversed(timeline_logs)
    ]

    return WhatsAppDashboardMetrics(
        whatsapp_messages_sent=sent_count + deliv_count,
        whatsapp_messages_delivered=deliv_count,
        whatsapp_messages_failed=failed_count,
        whatsapp_messages_blocked=blocked_count,
        whatsapp_recovery_rate=round(recovery_rate, 2),
        revenue_recovered_from_whatsapp=float(recovered_amount),
        current_provider_mode=settings.TWILIO_WHATSAPP_MODE,
        timeline=timeline_items,
    )
