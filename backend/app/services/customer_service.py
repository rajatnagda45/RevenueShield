"""Customer resolution and provisioning service."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.schemas.event import NormalizedEvent


class CustomerService:
    """Handles deterministic customer resolution and explicit fallback mapping."""

    @classmethod
    def resolve_or_create_customer(
        cls, db: Session, event: NormalizedEvent
    ) -> Customer:
        """Resolve an existing customer record matching event signals or create one.

        Priority order:
        1. Query by external_customer_id (exact match)
        2. Query by email (case-insensitive match)
        3. Query by phone number (exact match)
        4. Auto-provision customer record if email/phone exists
        5. Create explicit unresolved placeholder customer record for anonymous events

        Args:
            db: Active database session.
            event: Normalized incoming event.

        Returns:
            Resolved or newly provisioned Customer model instance.
        """
        # 1. Match by external customer ID
        if event.external_customer_id:
            stmt = select(Customer).where(Customer.external_customer_id == event.external_customer_id)
            customer = db.scalar(stmt)
            if customer:
                # A real name (e.g. from invoice customer_details) replaces an auto-generated placeholder.
                if event.customer_name and (not customer.name or customer.name.startswith("Customer (")):
                    customer.name = event.customer_name
                if event.customer_phone and not customer.phone:
                    customer.phone = event.customer_phone
                return customer

        # 2. Match by email
        if event.customer_email:
            stmt = select(Customer).where(Customer.email.ilike(event.customer_email))
            customer = db.scalar(stmt)
            if customer:
                # Update external_customer_id or phone if not previously set
                if event.external_customer_id and not customer.external_customer_id:
                    customer.external_customer_id = event.external_customer_id
                if event.customer_phone and not customer.phone:
                    customer.phone = event.customer_phone
                return customer

        # 3. Match by phone
        if event.customer_phone:
            stmt = select(Customer).where(Customer.phone == event.customer_phone)
            customer = db.scalar(stmt)
            if customer:
                return customer

        # 4. Auto-provision if email or phone available
        if event.customer_email or event.customer_phone:
            email = event.customer_email or f"rzp_{event.external_payment_id or event.event_id}@placeholder.com"
            name = event.customer_name or f"Customer ({email.split('@')[0]})"
            customer = Customer(
                external_customer_id=event.external_customer_id,
                name=name,
                email=email,
                phone=event.customer_phone,
                segment="STANDARD",
                preferred_channel="EMAIL" if event.customer_email else "SMS",
                dnd_enabled=False,
            )
            db.add(customer)
            db.flush()
            return customer

        # 5. Explicit Unresolved Customer handling
        # Ensure we never silently guess identity while satisfying foreign key requirements
        unresolved_external_id = f"unresolved_{event.external_payment_id or event.event_id}"
        stmt = select(Customer).where(Customer.external_customer_id == unresolved_external_id)
        customer = db.scalar(stmt)
        if not customer:
            customer = Customer(
                external_customer_id=unresolved_external_id,
                name="Unresolved Gateway Customer",
                email=f"{unresolved_external_id}@unresolved.local",
                segment="UNRESOLVED",
                preferred_channel=None,
                dnd_enabled=False,
            )
            db.add(customer)
            db.flush()

        return customer

    @classmethod
    def reconcile_customer(
        cls,
        db: Session,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        name: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> Customer:
        """Reconcile or provision a Customer from direct fields."""
        if external_id:
            customer = db.scalar(select(Customer).where(Customer.external_customer_id == external_id))
            if customer:
                return customer

        if email:
            customer = db.scalar(select(Customer).where(Customer.email.ilike(email)))
            if customer:
                if external_id and not customer.external_customer_id:
                    customer.external_customer_id = external_id
                if phone and not customer.phone:
                    customer.phone = phone
                return customer

        if phone:
            customer = db.scalar(select(Customer).where(Customer.phone == phone))
            if customer:
                return customer

        if email or phone:
            resolved_email = email or f"cust_{phone.replace('+', '')}@placeholder.com"
            resolved_name = name or f"Customer ({resolved_email.split('@')[0]})"
            customer = Customer(
                external_customer_id=external_id,
                name=resolved_name,
                email=resolved_email,
                phone=phone,
                segment="STANDARD",
                preferred_channel="EMAIL" if email else "SMS",
                dnd_enabled=False,
            )
            db.add(customer)
            db.flush()
            return customer

        # Unresolved fallback
        fallback_email = f"unresolved_{datetime.now(timezone.utc).timestamp()}@unresolved.local"
        customer = Customer(
            name=name or "Unresolved Customer",
            email=fallback_email,
            segment="UNRESOLVED",
            preferred_channel=None,
            dnd_enabled=False,
        )
        db.add(customer)
        db.flush()
        return customer
