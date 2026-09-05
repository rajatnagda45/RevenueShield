"""SMTP Email client for sending transactional recovery emails with rich HTML and plain text."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import smtplib
from app.core.resilience import retry_with_backoff
import ssl
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class EmailMessageResponse:
    """Standardized response from SMTP email dispatch."""

    success: bool
    status: str  # "SENT", "FAILED"
    message_id: Optional[str] = None
    recipient: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    dispatched_at: Optional[datetime] = None


class SMTPClient:
    """Production SMTP client for payment recovery emails with HTML templates and SSL/TLS."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self.host = host if host is not None else settings.SMTP_HOST
        self.port = port if port is not None else settings.SMTP_PORT
        self.user = user if user is not None else settings.SMTP_USER
        self.password = password if password is not None else settings.SMTP_PASSWORD
        self.from_email = from_email if from_email is not None else (settings.SMTP_FROM_EMAIL or self.user or "")
        self.from_name = from_name if from_name is not None else settings.SMTP_FROM_NAME
        self.timeout = timeout

        if not self.user or not self.password:
            logger.warning("[SMTP_INIT] SMTP credentials not fully configured.")

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.from_email)

    def send_recovery_email(
        self,
        recipient_email: str,
        subject: str,
        html_content: str,
        plain_text_content: Optional[str] = None,
    ) -> EmailMessageResponse:
        """Send a recovery email with multipart HTML and plain text fallback."""
        if not self.is_configured:
            return EmailMessageResponse(
                success=False,
                status="FAILED",
                recipient=recipient_email,
                error_code="SMTP_NOT_CONFIGURED",
                error_message="SMTP credentials (user, password, from_email) not configured.",
            )

        if not recipient_email or "@" not in recipient_email:
            return EmailMessageResponse(
                success=False,
                status="FAILED",
                recipient=recipient_email,
                error_code="INVALID_RECIPIENT",
                error_message=f"Invalid recipient email address: '{recipient_email}'",
            )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.from_name} <{self.from_email}>"
        msg["To"] = recipient_email

        # Attach text and HTML parts
        if plain_text_content:
            msg.attach(MIMEText(plain_text_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        dispatched_time = datetime.now(timezone.utc)
        msg_id = f"email_{int(dispatched_time.timestamp())}_{recipient_email.split('@')[0]}"

        try:
            context = ssl.create_default_context()

            def _send() -> None:
                if self.port == 465:
                    with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=self.timeout) as server:
                        server.login(self.user, self.password)
                        server.sendmail(self.from_email, [recipient_email], msg.as_string())
                else:
                    with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                        server.ehlo()
                        server.starttls(context=context)
                        server.ehlo()
                        server.login(self.user, self.password)
                        server.sendmail(self.from_email, [recipient_email], msg.as_string())

            # Transient transport failures are retried with exponential backoff; auth errors are not.
            retry_with_backoff(
                _send, retries=2, base_seconds=0.5, max_seconds=4.0,
                retry_on=(smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, smtplib.SMTPHeloError, ConnectionError, TimeoutError, OSError),
            )

            logger.info(f"[SMTP_SEND_SUCCESS] To={recipient_email} Subject='{subject}'")
            return EmailMessageResponse(
                success=True,
                status="SENT",
                message_id=msg_id,
                recipient=recipient_email,
                dispatched_at=dispatched_time,
            )

        except smtplib.SMTPAuthenticationError as exc:
            logger.error(f"[SMTP_AUTH_ERROR] Failed authentication for {self.user}: {exc}")
            return EmailMessageResponse(
                success=False,
                status="FAILED",
                recipient=recipient_email,
                error_code="SMTP_AUTH_ERROR",
                error_message=str(exc),
            )
        except Exception as exc:
            logger.error(f"[SMTP_SEND_ERROR] Error sending email to {recipient_email}: {exc}")
            return EmailMessageResponse(
                success=False,
                status="FAILED",
                recipient=recipient_email,
                error_code="SMTP_SEND_FAILED",
                error_message=str(exc),
            )
