"""Razorpay Payment Link recovery action executor."""
import logging
import uuid
from typing import Optional
from app.core.config import settings
from app.execution.base import ExecutionRequest, ExecutionResult, ExecutionStatus, RecoveryExecutor
from app.integrations.razorpay.client import RazorpayClient, RazorpayAPIError

logger = logging.getLogger(__name__)


class RazorpayPaymentLinkExecutor(RecoveryExecutor):
    """Executes SEND_PAYMENT_LINK actions via official Razorpay Payment Links API."""

    provider_name: str = "RAZORPAY"

    def __init__(self, client: Optional[RazorpayClient] = None):
        self.client = client or RazorpayClient()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute payment link creation in either dry_run or razorpay_test mode."""
        mode = getattr(settings, "EXECUTION_MODE", "dry_run").lower()

        logger.info(
            f"[EXECUTION_STARTED] Executing SEND_PAYMENT_LINK for case={request.case_id} "
            f"amount={request.amount} {request.currency} mode={mode}"
        )

        # ---------------- 1. Dry Run Mode (Simulated Provider) ----------------
        if mode == "dry_run":
            sim_ref = f"sim_plink_{uuid.uuid4().hex[:12]}"
            sim_url = f"https://simulated.pay/i/{sim_ref}"

            logger.info(f"[EXECUTION_SIMULATED] Created dry run payment link {sim_ref}")
            return ExecutionResult(
                status=ExecutionStatus.SUCCEEDED,
                provider="DRY_RUN",
                provider_reference=sim_ref,
                provider_url=sim_url,
                execution_metadata={
                    "mode": "dry_run",
                    "simulated": True,
                    "amount": float(request.amount),
                    "currency": request.currency,
                },
            )

        # ---------------- 2. Real Razorpay Test Mode ----------------
        if mode == "razorpay_test":
            # Safety Check: Guarantee no Live keys are ever used in development
            key_id = settings.RAZORPAY_KEY_ID or ""
            if not key_id.startswith("rzp_test_"):
                logger.error("[EXECUTION_SECURITY_ERROR] Non-test Razorpay Key detected in razorpay_test mode!")
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    provider=self.provider_name,
                    error_code="INVALID_TEST_CREDENTIALS",
                    error_message="Live Razorpay API keys are strictly rejected in test mode.",
                )

            # Convert standard Decimal amount to paise integer (₹100.00 -> 10000 paise)
            amount_paise = int(request.amount * 100)
            description = f"Revenue recovery link for Case {request.case_id[:8]}"

            try:
                res = self.client.create_payment_link_sync(
                    amount_paise=amount_paise,
                    currency=request.currency,
                    description=description,
                    customer_name=request.customer_name,
                    customer_email=request.customer_email,
                    customer_phone=request.customer_phone,
                    reference_id=request.case_id,
                )

                link_id = res.get("id")
                short_url = res.get("short_url")

                logger.info(f"[EXECUTION_SUCCEEDED] Razorpay test payment link created: {link_id} -> {short_url}")
                return ExecutionResult(
                    status=ExecutionStatus.SUCCEEDED,
                    provider=self.provider_name,
                    provider_reference=link_id,
                    provider_url=short_url,
                    execution_metadata={
                        "mode": "razorpay_test",
                        "razorpay_status": res.get("status"),
                        "amount_paise": amount_paise,
                        "currency": request.currency,
                    },
                )
            except RazorpayAPIError as e:
                logger.error(f"[EXECUTION_FAILED] Razorpay API error ({e.status_code}): {e.message}")
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    provider=self.provider_name,
                    error_code=f"RAZORPAY_API_{e.status_code}",
                    error_message=e.message,
                    execution_metadata={"error_payload": e.error_payload},
                )
            except Exception as ex:
                logger.error(f"[EXECUTION_FAILED] Unexpected transport failure: {str(ex)}")
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    provider=self.provider_name,
                    error_code="TRANSPORT_ERROR",
                    error_message=str(ex),
                )

        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            provider="UNKNOWN",
            error_code="UNSUPPORTED_EXECUTION_MODE",
            error_message=f"Unknown execution mode '{mode}'. Supported: dry_run, razorpay_test.",
        )
