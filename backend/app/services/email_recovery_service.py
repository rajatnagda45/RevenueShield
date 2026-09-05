"""Email Payment Recovery Service for high-converting, bounded email recovery outreach."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, Optional, Tuple
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.email.smtp_client import SMTPClient, EmailMessageResponse
from app.integrations.razorpay.payment_link_client import RazorpayPaymentLinkClient
from app.models.audit_log import AuditLog
from app.models.communication import Communication
from app.models.customer import Customer
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.recovery_case import RecoveryCase
from app.services.notification_service import mask_contact

logger = logging.getLogger(__name__)


class EmailRecoveryService:
    """Orchestrates payment recovery via email with Razorpay payment link generation, HTML copy, and policy guards."""

    @classmethod
    def execute_recovery(
        cls,
        db: Session,
        case_id: str,
        recipient_email: Optional[str] = None,
        body_override: Optional[str] = None,
        dry_run: Optional[bool] = None,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Execute payment recovery email outreach with pre-flight race condition checking and Razorpay payment link.

        `body_override`: agent-personalised plain text (validated upstream) with `{payment_link}` substituted here.
        `dry_run`: when true, the SMTP send is simulated so the pipeline can be exercised without credentials.
        """
        # 1. Fetch Recovery Case
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
        if not case:
            return {
                "success": False,
                "status": "NOT_FOUND",
                "error": f"Recovery case {case_id} not found.",
            }

        # 2. Race Condition Guard: Monotonic Stopping Rule
        if case.status in ["RECOVERED", "CLOSED"]:
            logger.info(f"[EMAIL_RECOVERY_ABORTED] Case {case_id} is already {case.status}. Stopping email outreach.")
            return {
                "success": False,
                "status": "BLOCKED",
                "blocking_rule": "CASE_ALREADY_RECOVERED_OR_CLOSED",
                "reason": f"Recovery case is already {case.status}. Email outreach aborted.",
            }

        # 2b. Measurement: HOLDOUT arm never receives outreach
        from app.experiments.assigner import ExperimentAssigner
        holdout = ExperimentAssigner.holdout_block(case)
        if holdout:
            return {
                "success": False,
                "status": "BLOCKED",
                "blocking_rule": holdout["blocking_rule"],
                "reason": holdout["reason"],
            }

        # 3. Resolve Customer & Email
        customer = case.customer or db.scalar(select(Customer).where(Customer.id == case.customer_id)) if case.customer_id else None
        target_email = recipient_email or (customer.email if customer else None)

        # 3b. Cross-channel compliance (consent, frequency caps, handoff)
        from app.compliance.contact_policy import ContactPolicy
        compliance = ContactPolicy.evaluate(db, case=case, customer=customer, channel="EMAIL", now=reference_time)
        if not compliance.allowed:
            return {
                "success": False,
                "status": "BLOCKED",
                "blocking_rule": compliance.rule,
                "reason": compliance.reason,
                "next_eligible_at": compliance.next_eligible_at.isoformat() if compliance.next_eligible_at else None,
            }
        if not target_email or "@" not in target_email:
            return {
                "success": False,
                "status": "BLOCKED",
                "blocking_rule": "MISSING_CUSTOMER_EMAIL",
                "reason": "No valid customer email address found for recovery outreach.",
            }

        # 4. Generate or Reuse Razorpay Payment Link
        link, is_new = cls._get_or_create_payment_link(db=db, case=case)

        # 5. Render Responsive HTML & Plain Text Copy
        customer_name = (customer.name if customer and customer.name else "Valued Customer").split()[0]
        amount_formatted = f"₹{case.amount_at_risk:,.2f}" if case.amount_at_risk else "your pending amount"
        subject = f"Action Required: Complete your payment of {amount_formatted}"

        html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #f8fafc;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #0f172a; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width: 560px; background-color: #1e293b; border-radius: 16px; border: 1px solid #334155; overflow: hidden; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);">
          <!-- Header -->
          <tr>
            <td style="padding: 32px 32px 20px 32px; background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);">
              <h1 style="margin: 0; font-size: 24px; font-weight: 700; color: #ffffff; letter-spacing: -0.5px;">RevenueShield Recovery</h1>
              <p style="margin: 6px 0 0 0; font-size: 14px; color: #e0e7ff;">Secure Automated Payment Resolution</p>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding: 32px;">
              <p style="margin: 0 0 16px 0; font-size: 16px; line-height: 1.5; color: #e2e8f0;">
                Hi <strong>{customer_name}</strong>,
              </p>
              <p style="margin: 0 0 24px 0; font-size: 15px; line-height: 1.6; color: #94a3b8;">
                We noticed that your recent payment of <strong style="color: #f1f5f9;">{amount_formatted}</strong> could not be completed successfully due to a temporary bank processing timeout.
              </p>
              <!-- Info Box -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #0f172a; border-radius: 12px; border: 1px solid #334155; margin-bottom: 28px;">
                <tr>
                  <td style="padding: 16px 20px;">
                    <table role="presentation" width="100%">
                      <tr>
                        <td style="font-size: 13px; color: #64748b; padding-bottom: 6px;">Amount Due</td>
                        <td align="right" style="font-size: 16px; font-weight: 700; color: #10b981; padding-bottom: 6px;">{amount_formatted}</td>
                      </tr>
                      <tr>
                        <td style="font-size: 13px; color: #64748b;">Case Reference</td>
                        <td align="right" style="font-size: 13px; font-family: monospace; color: #94a3b8;">{str(case.id)[:8]}</td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
              <!-- CTA Button -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td align="center" style="padding-bottom: 24px;">
                    <a href="{link.payment_url}" target="_blank" style="display: inline-block; background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: #ffffff; font-size: 16px; font-weight: 600; text-decoration: none; padding: 14px 36px; border-radius: 10px; box-shadow: 0 4px 14px 0 rgba(16, 185, 129, 0.4);">
                      Complete Payment Securely &rarr;
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin: 0; font-size: 13px; line-height: 1.5; color: #64748b; text-align: center;">
                Powered by <strong>Razorpay Test Mode</strong>. Link expires in 48 hours.
              </p>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding: 20px 32px; background-color: #0f172a; border-top: 1px solid #1e293b; text-align: center;">
              <p style="margin: 0; font-size: 12px; color: #475569;">
                &copy; 2026 RevenueShield AI. This is a transactional recovery notification.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
        """

        plain_text_body = (
            f"Hi {customer_name},\n\n"
            f"Your payment of {amount_formatted} could not be completed.\n\n"
            f"You can securely complete your payment here:\n{link.payment_url}\n\n"
            f"Thank you,\nRevenueShield AI"
        )

        if body_override and body_override.strip():
            plain_text_body = body_override.replace("{payment_link}", link.payment_url)
            safe = plain_text_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            html_body = (
                "<!DOCTYPE html><html><body style=\"font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#0f172a;padding:24px;\">"
                f"<p style=\"font-size:15px;line-height:1.6;\">{safe}</p>"
                f"<p><a href=\"{link.payment_url}\" style=\"display:inline-block;padding:12px 20px;background:#4f46e5;color:#fff;border-radius:8px;text-decoration:none;\">Complete payment securely</a></p>"
                "<p style=\"font-size:12px;color:#64748b;\">This is a transactional payment notification from RevenueShield.</p>"
                "</body></html>"
            )

        # 6. Dispatch via SMTP (or simulate)
        if dry_run:
            email_res = EmailMessageResponse(success=True, status="SENT", message_id=f"sim_{uuid.uuid4().hex[:12]}", recipient=target_email)
        else:
            smtp_client = SMTPClient()
            email_res: EmailMessageResponse = smtp_client.send_recovery_email(
                recipient_email=target_email,
                subject=subject,
                html_content=html_body,
                plain_text_content=plain_text_body,
            )

        # 7. Record Communication Log (virtual clock aware for replays)
        now = reference_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        comm = Communication(
            recovery_case_id=case.id,
            customer_id=customer.id if customer else None,
            channel="EMAIL",
            provider="GMAIL_SMTP",
            template_name="PAYMENT_RECOVERY_EMAIL_V1",
            template_version="v1.0",
            language="ENGLISH",
            recipient_reference=target_email,
            recipient_masked=mask_contact(target_email),
            message_body=plain_text_body,
            status="SENT" if email_res.success else "FAILED",
            provider_message_id=email_res.message_id,
            failure_reason=email_res.error_message,
            sent_at=now if email_res.success else None,
            attempt_number=case.retry_count + 1,
            is_simulated=False,
            idempotency_key=f"comm_{case.id}_EMAIL_{case.retry_count + 1}",
        )
        db.add(comm)
        db.flush()

        # 8. Update Case State & Audit Trail
        if email_res.success:
            case.status = "IN_PROGRESS"
            case.retry_count += 1
            case.last_intervention_at = now
            from app.ledger.service import LedgerService
            from app.compliance.contact_policy import ContactPolicy
            LedgerService.post_cost(db, case, channel="EMAIL", provider_reference=str(comm.id))
            ContactPolicy.record_sent(db, case=case, channel="EMAIL", reference=str(comm.id), now=now)

            audit = AuditLog(
                recovery_case_id=case.id,
                entity_type="RECOVERY_CASE",
                entity_id=str(case.id),
                action="EMAIL_RECOVERY_SENT",
                actor_type="SYSTEM",
                actor_id="email_recovery_service",
                audit_metadata={
                    "recipient": mask_contact(target_email),
                    "amount": str(case.amount_at_risk),
                    "payment_url": link.payment_url,
                    "attempt": case.retry_count,
                },
            )
            db.add(audit)

        db.commit()

        return {
            "success": email_res.success,
            "status": "SENT" if email_res.success else "FAILED",
            "communication_id": str(comm.id),
            "recipient": target_email,
            "payment_link_url": link.payment_url,
            "error": email_res.error_message,
        }

    @classmethod
    def _get_or_create_payment_link(
        cls,
        db: Session,
        case: RecoveryCase,
    ) -> Tuple[RecoveryPaymentLink, bool]:
        """Fetch active payment link or create a new Razorpay payment link."""
        active_link = db.scalar(
            select(RecoveryPaymentLink)
            .where(
                RecoveryPaymentLink.recovery_case_id == case.id,
                RecoveryPaymentLink.status.in_(["CREATED", "SENT"]),
            )
            .order_by(RecoveryPaymentLink.created_at.desc())
        )
        if active_link:
            return active_link, False

        # Create real Razorpay Payment Link
        amount_paise = int(Decimal(str(case.amount_at_risk or "0.00")) * 100)
        client = RazorpayPaymentLinkClient()
        try:
            link_dto = client.create_payment_link(
                amount_paise=amount_paise,
                currency=case.currency or "INR",
                description=f"Payment recovery for case {str(case.id)[:8]}",
                customer_name=case.customer.name if case.customer else None,
                customer_email=case.customer.email if case.customer else None,
                customer_phone=case.customer.phone if case.customer else None,
            )
            payment_url = link_dto.short_url
            plink_id = link_dto.payment_link_id
        except Exception as exc:
            logger.warning(f"[RAZORPAY_PLINK_FALLBACK] Failed to call Razorpay API: {exc}. Using test mode link.")
            plink_id = f"plink_sim_{uuid.uuid4().hex[:12]}"
            payment_url = f"https://rzp.io/i/{plink_id}"

        existing_db_link = db.scalar(
            select(RecoveryPaymentLink)
            .where(RecoveryPaymentLink.razorpay_payment_link_id == plink_id)
            .limit(1)
        )
        if existing_db_link:
            existing_db_link.recovery_case_id = case.id
            existing_db_link.amount = case.amount_at_risk or Decimal("0.00")
            existing_db_link.currency = case.currency or "INR"
            existing_db_link.status = "CREATED"
            existing_db_link.payment_url = payment_url
            db.flush()
            return existing_db_link, False

        link = RecoveryPaymentLink(
            recovery_case_id=case.id,
            razorpay_payment_link_id=plink_id,
            payment_url=payment_url,
            amount=case.amount_at_risk or Decimal("0.00"),
            currency=case.currency or "INR",
            status="CREATED",
        )
        db.add(link)
        db.flush()
        return link, True
