"""Razorpay payload normalization adapter.

Transforms provider-specific Razorpay webhook JSON structures into the system's canonical
internal NormalizedEvent model. v2 understands payment, order, invoice, subscription, refund and
settlement entity containers so every leak surface flows through one ingestion path.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from app.schemas.event import NormalizedEvent


def _paise_to_rupees(raw: Any) -> Decimal:
    try:
        return (Decimal(str(raw or 0)) / Decimal("100")).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _epoch_to_dt(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return None


class RazorpayAdapter:
    """Adapter for parsing and normalizing Razorpay webhook payloads."""

    @classmethod
    def _entity(cls, container: Dict[str, Any], name: str) -> Dict[str, Any]:
        node = container.get(name, {})
        if isinstance(node, dict):
            ent = node.get("entity", {})
            return ent if isinstance(ent, dict) else {}
        return {}

    @classmethod
    def normalize(
        cls,
        payload: Dict[str, Any],
        event_id_header: Optional[str] = None,
    ) -> NormalizedEvent:
        """Convert a raw Razorpay webhook payload dictionary to NormalizedEvent."""
        event_type = payload.get("event", "unknown")
        payload_container = payload.get("payload", {}) or {}
        payment_entity = cls._entity(payload_container, "payment")
        order_entity = cls._entity(payload_container, "order")
        invoice_entity = cls._entity(payload_container, "invoice")
        subscription_entity = cls._entity(payload_container, "subscription")
        refund_entity = cls._entity(payload_container, "refund")
        settlement_entity = cls._entity(payload_container, "settlement")

        family = str(event_type).split(".")[0]

        # 1. Event ID for idempotency
        event_id = event_id_header or payload.get("event_id") or payload.get("id")
        if not event_id:
            anchor = (
                payment_entity.get("id") or order_entity.get("id") or invoice_entity.get("id")
                or subscription_entity.get("id") or refund_entity.get("id") or settlement_entity.get("id") or "unknown"
            )
            created_ts = payload.get("created_at", int(datetime.now(timezone.utc).timestamp()))
            event_id = f"rzp_evt_{event_type}_{anchor}_{created_ts}"

        # 2. Timestamp
        primary_entity = {
            "payment": payment_entity, "order": order_entity, "invoice": invoice_entity,
            "subscription": subscription_entity, "refund": refund_entity, "settlement": settlement_entity,
        }.get(family, payment_entity)
        occurred_at = _epoch_to_dt(primary_entity.get("created_at")) or _epoch_to_dt(payload.get("created_at")) or datetime.now(timezone.utc)

        # 3. Amount / currency by family
        if family == "invoice":
            amount_decimal = _paise_to_rupees(invoice_entity.get("amount_due", invoice_entity.get("amount")))
            currency = (invoice_entity.get("currency") or "INR").upper()
        elif family == "order":
            amount_decimal = _paise_to_rupees(order_entity.get("amount_due", order_entity.get("amount")))
            currency = (order_entity.get("currency") or "INR").upper()
        elif family == "subscription":
            # subscription.pending/charged carry the payment entity; fall back to plan/notes amount
            raw = payment_entity.get("amount") or subscription_entity.get("amount") or (subscription_entity.get("notes") or {}).get("amount")
            amount_decimal = _paise_to_rupees(raw)
            currency = (payment_entity.get("currency") or subscription_entity.get("currency") or "INR").upper()
        elif family == "refund":
            amount_decimal = _paise_to_rupees(refund_entity.get("amount"))
            currency = (refund_entity.get("currency") or "INR").upper()
        elif family == "settlement":
            amount_decimal = _paise_to_rupees(settlement_entity.get("amount"))
            currency = "INR"
        else:
            amount_decimal = _paise_to_rupees(payment_entity.get("amount", 0))
            currency = (payment_entity.get("currency") or "INR").upper()

        # 4. Customer signals (payment entity first, then invoice customer_details, then notes)
        notes = payment_entity.get("notes") or order_entity.get("notes") or invoice_entity.get("notes") or subscription_entity.get("notes") or {}
        notes = notes if isinstance(notes, dict) else {}
        cust_details = invoice_entity.get("customer_details") or {}
        external_customer_id = (
            notes.get("customer_id") or notes.get("external_customer_id")
            or payment_entity.get("customer_id") or invoice_entity.get("customer_id")
            or subscription_entity.get("customer_id") or cust_details.get("customer_id") or order_entity.get("customer_id")
        )
        customer_email = payment_entity.get("email") or cust_details.get("email") or notes.get("customer_email") or notes.get("email")
        customer_phone = payment_entity.get("contact") or cust_details.get("contact") or notes.get("customer_phone") or notes.get("contact")
        customer_name = notes.get("customer_name") or cust_details.get("name") or cust_details.get("customer_name") or payment_entity.get("name")

        # 5. Entity references
        external_payment_id = payment_entity.get("id")
        external_order_id = payment_entity.get("order_id") or order_entity.get("id") or invoice_entity.get("order_id") or notes.get("order_id")
        external_invoice_id = payment_entity.get("invoice_id") or invoice_entity.get("id") or notes.get("invoice_id")
        external_subscription_id = (
            subscription_entity.get("id") or notes.get("subscription_id") or payment_entity.get("subscription_id")
            or invoice_entity.get("subscription_id")
        )

        # 6. Payment status & method
        raw_method = payment_entity.get("method")
        payment_method = raw_method.upper() if raw_method else ("CARD" if family == "payment" else None)
        raw_status = str(payment_entity.get("status", "")).lower()
        if raw_status in ("captured", "success"):
            payment_status = "SUCCESS"
        elif raw_status == "failed":
            payment_status = "FAILED"
        elif raw_status == "authorized":
            payment_status = "AUTHORIZED"
        else:
            payment_status = raw_status.upper() if raw_status else None

        # 7. Failure diagnostics
        failure_code = payment_entity.get("error_code") or payment_entity.get("error_reason")
        failure_description = payment_entity.get("error_description")
        failure_source = payment_entity.get("error_source")
        failure_step = payment_entity.get("error_step")
        failure_reason = payment_entity.get("error_reason")
        if family == "invoice" and event_type in ("invoice.expired",) and not failure_reason:
            failure_reason = "invoice_expired"
        if family == "subscription" and not failure_reason and event_type in ("subscription.pending", "subscription.halted"):
            failure_reason = "mandate_charge_failed"

        # 8. Metadata
        metadata: Dict[str, Any] = {
            "account_id": payload.get("account_id"),
            "bank": payment_entity.get("bank"),
            "wallet": payment_entity.get("wallet"),
            "vpa": payment_entity.get("vpa"),
            "international": payment_entity.get("international", False),
            "order_id": external_order_id,
            "invoice_id": external_invoice_id,
            "notes": notes,
            "entity_family": family,
            "replay": bool(notes.get("replay")),  # replay harness: backdate case creation to the event time
        }
        if order_entity:
            metadata["order"] = {
                "status": order_entity.get("status"), "amount": float(_paise_to_rupees(order_entity.get("amount"))),
                "amount_paid": float(_paise_to_rupees(order_entity.get("amount_paid"))), "attempts": order_entity.get("attempts", 0),
                "receipt": order_entity.get("receipt"), "created_at": order_entity.get("created_at"),
            }
        if invoice_entity:
            metadata["invoice"] = {
                "status": invoice_entity.get("status"), "amount": float(_paise_to_rupees(invoice_entity.get("amount"))),
                "amount_paid": float(_paise_to_rupees(invoice_entity.get("amount_paid"))),
                "amount_due": float(_paise_to_rupees(invoice_entity.get("amount_due"))),
                "due_by": invoice_entity.get("expire_by") or invoice_entity.get("due_by"), "issued_at": invoice_entity.get("issued_at"),
                "short_url": invoice_entity.get("short_url"), "type": invoice_entity.get("type"),
            }
        if subscription_entity:
            metadata["subscription"] = {
                "status": subscription_entity.get("status"), "plan_id": subscription_entity.get("plan_id"),
                "charge_at": subscription_entity.get("charge_at"), "auth_attempts": subscription_entity.get("auth_attempts", 0),
                "paid_count": subscription_entity.get("paid_count", 0), "remaining_count": subscription_entity.get("remaining_count"),
                "payment_method": subscription_entity.get("payment_method"), "short_url": subscription_entity.get("short_url"),
                "current_start": subscription_entity.get("current_start"), "current_end": subscription_entity.get("current_end"),
            }
        if refund_entity:
            metadata["refund"] = {"id": refund_entity.get("id"), "payment_id": refund_entity.get("payment_id"), "amount": float(amount_decimal)}
            external_payment_id = external_payment_id or refund_entity.get("payment_id")
        if settlement_entity:
            metadata["settlement"] = {"id": settlement_entity.get("id"), "status": settlement_entity.get("status"), "utr": settlement_entity.get("utr")}

        return NormalizedEvent(
            event_id=str(event_id),
            event_type=str(event_type),
            source="RAZORPAY",
            occurred_at=occurred_at,
            amount=amount_decimal,
            currency=currency,
            external_customer_id=str(external_customer_id) if external_customer_id else None,
            customer_email=str(customer_email).strip().lower() if customer_email else None,
            customer_phone=str(customer_phone).strip() if customer_phone else None,
            customer_name=str(customer_name).strip() if customer_name else None,
            external_payment_id=str(external_payment_id) if external_payment_id else None,
            external_order_id=str(external_order_id) if external_order_id else None,
            external_invoice_id=str(external_invoice_id) if external_invoice_id else None,
            external_subscription_id=str(external_subscription_id) if external_subscription_id else None,
            payment_method=payment_method,
            payment_status=payment_status,
            failure_code=str(failure_code) if failure_code else None,
            failure_description=str(failure_description) if failure_description else None,
            failure_source=str(failure_source) if failure_source else None,
            failure_step=str(failure_step) if failure_step else None,
            failure_reason=str(failure_reason) if failure_reason else None,
            batch_id=str(notes.get("batch_id")) if notes.get("batch_id") else None,
            metadata=metadata,
            raw_payload=payload,
        )
