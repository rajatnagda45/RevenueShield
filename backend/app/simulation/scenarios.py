"""Scenario generator: realistic, seeded, Razorpay-shaped leak events across all surfaces.

Distributions are chosen to look like an Indian subscription/commerce merchant: UPI-heavy,
insufficient-funds and OTP failures dominate, a long tail of bank declines and expired cards,
some B2B invoices, and (optionally) one issuer outage episode.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

SURFACE_MIX = [("PAYMENT_FAILURE", 0.55), ("SUBSCRIPTION_MANDATE_FAILURE", 0.20), ("CHECKOUT_ABANDONMENT", 0.15), ("RECEIVABLE_OVERDUE", 0.10)]

# (error_reason, error_description, error_source, error_step, weight) - Razorpay-style reason vocabulary
FAILURE_MIX = [
    ("insufficient_funds", "Your payment could not be completed due to insufficient funds in the account.", "customer", "payment_authorization", 0.30),
    ("payment_failed", "Customer failed to complete OTP verification. incorrect_otp", "customer", "payment_authentication", 0.15),
    ("gateway_timeout", "Issuing bank timed out during authorization. bank_timeout", "bank", "payment_authorization", 0.12),
    ("do_not_honor", "Transaction declined by the issuing bank. do_not_honor", "bank", "payment_authorization", 0.12),
    ("card_expired", "The card has expired. expired_card", "customer", "payment_authorization", 0.08),
    ("invalid_vpa", "The UPI ID entered is invalid or not found. invalid_vpa", "customer", "payment_initiation", 0.06),
    ("payment_cancelled", "Payment was cancelled by the user", "customer", "payment_authentication", 0.10),
    ("card_limit_exceeded", "Card transaction limit exceeded. card_limit_exceeded", "bank", "payment_authorization", 0.04),
    ("risk_threshold", "Transaction blocked by risk checks. risk_threshold", "gateway", "payment_authorization", 0.03),
]

METHOD_BANKS = {
    "upi": ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "PAYTM"],
    "card": ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "AMEX"],
    "netbanking": ["HDFC", "ICICI", "SBI", "AXIS"],
}
METHOD_MIX = [("upi", 0.62), ("card", 0.28), ("netbanking", 0.10)]
SEGMENTS = [("STANDARD", 0.55), ("SMB", 0.20), ("MID_MARKET", 0.15), ("ENTERPRISE", 0.10)]
LANGUAGES = [("ENGLISH", 0.65), ("HINGLISH", 0.35)]
FIRST_NAMES = ["Aarav", "Vihaan", "Ananya", "Diya", "Ishaan", "Kavya", "Rohan", "Priya", "Arjun", "Meera", "Kabir", "Sara", "Aditya", "Nisha", "Rahul", "Pooja", "Dev", "Riya", "Karan", "Neha"]
LAST_NAMES = ["Sharma", "Verma", "Iyer", "Reddy", "Patel", "Khan", "Nair", "Singh", "Das", "Mehta", "Gupta", "Bose", "Joshi", "Rao", "Chopra"]
B2B_NAMES = ["Acme Retail Pvt Ltd", "Nimbus Logistics LLP", "Saffron Foods Ltd", "Kestrel Analytics Pvt Ltd", "Bluebird Apparel", "Trident Infra Pvt Ltd", "Orchid Pharma Distributors", "Zenith EdTech Pvt Ltd"]


def _weighted(rng: random.Random, pairs):
    r = rng.random()
    acc = 0.0
    for value, w in pairs:
        acc += w
        if r <= acc:
            return value
    return pairs[-1][0]


@dataclass
class SimCustomer:
    external_id: str
    name: str
    email: str
    phone: str
    segment: str
    language: str
    is_b2b: bool = False


@dataclass
class SimCase:
    """One leak to replay, with the webhooks that open it and the facts the behaviour model needs."""

    key: str
    surface: str
    customer: SimCustomer
    amount: Decimal
    method: str
    bank: Optional[str]
    failure: Dict[str, Any]
    occurred_at: datetime
    open_events: List[Dict[str, Any]] = field(default_factory=list)   # webhook payloads (in order)
    ids: Dict[str, str] = field(default_factory=dict)                  # payment / subscription / invoice / order ids
    systemic: bool = False


@dataclass
class Scenario:
    batch_id: str
    seed: int
    start: datetime
    cases: List[SimCase]
    baseline_events: List[Dict[str, Any]]  # healthy captured payments (degradation baseline + organic traffic)
    degradation_window: Optional[tuple[datetime, datetime]] = None

    def all_open_events(self) -> List[tuple[datetime, Dict[str, Any], str]]:
        out: List[tuple[datetime, Dict[str, Any], str]] = []
        for c in self.cases:
            for i, ev in enumerate(c.open_events):
                out.append((c.occurred_at + timedelta(seconds=i * 5), ev, c.key))
        return out


class ScenarioGenerator:
    def __init__(self, *, seed: int = 7, batch_id: Optional[str] = None):
        self.rng = random.Random(seed)
        self.seed = seed
        self.batch_id = batch_id or f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ public

    def generate(self, *, n_cases: int, start: datetime, spread_days: int = 3, degradation_episode: bool = True) -> Scenario:
        rng = self.rng
        cases: List[SimCase] = []
        customers = [self._customer(i) for i in range(n_cases)]
        for i in range(n_cases):
            surface = _weighted(rng, SURFACE_MIX)
            cust = customers[i]
            if surface == "RECEIVABLE_OVERDUE":
                cust.is_b2b = True
                cust.name = rng.choice(B2B_NAMES) + f" {i % 97:02d}"
                cust.email = f"ap{i}@{cust.name.split()[0].lower()}.example.com"
            occurred = start + timedelta(seconds=rng.randint(0, spread_days * 86400))
            cases.append(self._case(i, surface, cust, occurred))

        baseline: List[Dict[str, Any]] = []
        window = None
        if degradation_episode:
            # Healthy baseline traffic for HDFC UPI over the prior week (so the monitor has a baseline),
            # then a 15-minute burst of bank_timeout failures on day 2 across 45 extra customers.
            for j in range(220):
                t = start - timedelta(days=rng.uniform(1, 6))
                baseline.append(self._captured_payment(customers[j % n_cases], amount=Decimal(str(rng.randint(199, 4999))), method="upi", bank="HDFC", at=t, batch_tag=False))
            burst_start = start + timedelta(days=2, hours=11)
            window = (burst_start, burst_start + timedelta(minutes=15))
            for j in range(45):
                cust = self._customer(100000 + j)
                occurred = burst_start + timedelta(seconds=rng.randint(0, 14 * 60))
                failure = {"error_reason": "gateway_timeout", "error_description": "Issuing bank timed out during authorization. bank_timeout", "error_source": "bank", "error_step": "payment_authorization"}
                c = SimCase(key=f"sys_{j}", surface="PAYMENT_FAILURE", customer=cust, amount=Decimal(str(rng.randint(299, 2999))), method="upi", bank="HDFC", failure=failure, occurred_at=occurred, systemic=True)
                c.ids["payment_id"] = f"pay_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}"
                c.open_events.append(self._payment_failed(c))
                cases.append(c)
            # a few healthy HDFC UPI captures in the burst window so the cell is not 100% failures
            for j in range(12):
                baseline.append(self._captured_payment(customers[j], amount=Decimal("499"), method="upi", bank="HDFC", at=burst_start + timedelta(seconds=rng.randint(0, 14 * 60)), batch_tag=False))

        cases.sort(key=lambda c: c.occurred_at)
        return Scenario(batch_id=self.batch_id, seed=self.seed, start=start, cases=cases, baseline_events=baseline, degradation_window=window)

    # ------------------------------------------------------------------ builders

    def _customer(self, i: int) -> SimCustomer:
        rng = self.rng
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        return SimCustomer(
            external_id=f"cust_sim_{self.seed}_{i}",
            name=f"{first} {last}",
            email=f"{first.lower()}.{last.lower()}.{i}@example.com",
            phone=f"+9198{rng.randint(10000000, 99999999)}",
            segment=_weighted(rng, SEGMENTS),
            language=_weighted(rng, LANGUAGES),
        )

    def _amount(self, surface: str, segment: str) -> Decimal:
        rng = self.rng
        base = {"STANDARD": 900, "SMB": 3500, "MID_MARKET": 12000, "ENTERPRISE": 45000}[segment]
        if surface == "RECEIVABLE_OVERDUE":
            base = {"STANDARD": 25000, "SMB": 60000, "MID_MARKET": 180000, "ENTERPRISE": 450000}[segment]
        if surface == "CHECKOUT_ABANDONMENT":
            base = base * 0.6
        amt = rng.lognormvariate(0, 0.55) * base
        return Decimal(str(round(max(99.0, min(amt, 2_500_000.0)), 2)))

    def _case(self, i: int, surface: str, cust: SimCustomer, occurred: datetime) -> SimCase:
        rng = self.rng
        method = _weighted(rng, METHOD_MIX)
        bank = rng.choice(METHOD_BANKS[method])
        reason, desc, source, step, _ = rng.choices(FAILURE_MIX, weights=[f[4] for f in FAILURE_MIX])[0]
        if surface == "CHECKOUT_ABANDONMENT":
            reason, desc, source, step = "payment_cancelled", "Payment was cancelled by the user", "customer", "payment_authentication"
        if surface == "RECEIVABLE_OVERDUE":
            reason, desc, source, step = "invoice_expired", "Invoice passed its due date", "merchant", "invoice"
        failure = {"error_reason": reason, "error_description": desc, "error_source": source, "error_step": step}
        c = SimCase(key=f"case_{i}", surface=surface, customer=cust, amount=self._amount(surface, cust.segment), method=method, bank=bank, failure=failure, occurred_at=occurred)
        c.ids["payment_id"] = f"pay_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}"
        if surface == "PAYMENT_FAILURE":
            c.open_events.append(self._payment_failed(c))
        elif surface == "CHECKOUT_ABANDONMENT":
            c.ids["order_id"] = f"order_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}"
            c.open_events.append(self._payment_failed(c, order_id=c.ids["order_id"]))
        elif surface == "SUBSCRIPTION_MANDATE_FAILURE":
            c.ids["subscription_id"] = f"sub_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}"
            c.open_events.append(self._subscription_pending(c))
        elif surface == "RECEIVABLE_OVERDUE":
            c.ids["invoice_id"] = f"inv_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}"
            c.open_events.append(self._invoice_expired(c))
        return c

    def _notes(self, c: SimCase) -> Dict[str, Any]:
        return {"batch_id": self.batch_id, "replay": True, "customer_id": c.customer.external_id, "customer_name": c.customer.name, "scenario_key": c.key}

    def _payment_entity(self, c: SimCase, *, status: str, payment_id: Optional[str] = None, order_id: Optional[str] = None, at: Optional[datetime] = None) -> Dict[str, Any]:
        at = at or c.occurred_at
        ent: Dict[str, Any] = {
            "id": payment_id or c.ids["payment_id"], "entity": "payment", "amount": int(c.amount * 100), "currency": "INR", "status": status,
            "method": c.method, "bank": c.bank if c.method != "upi" else None, "vpa": f"{c.customer.name.split()[0].lower()}@{(c.bank or 'upi').lower()}" if c.method == "upi" else None,
            "email": c.customer.email, "contact": c.customer.phone, "notes": self._notes(c), "created_at": int(at.timestamp()),
            "captured": status == "captured", "international": False,
        }
        if c.method == "upi":
            ent["bank"] = c.bank  # Razorpay reports the PSP/issuer for UPI in `bank` for many flows
        if order_id:
            ent["order_id"] = order_id
        if status == "failed":
            ent.update({"error_code": "BAD_REQUEST_ERROR", **c.failure})
        return ent

    def _payment_failed(self, c: SimCase, order_id: Optional[str] = None) -> Dict[str, Any]:
        return {"entity": "event", "account_id": "acc_sim", "event": "payment.failed", "contains": ["payment"], "created_at": int(c.occurred_at.timestamp()),
                "payload": {"payment": {"entity": self._payment_entity(c, status="failed", order_id=order_id)}}}

    def _subscription_pending(self, c: SimCase) -> Dict[str, Any]:
        return {"entity": "event", "account_id": "acc_sim", "event": "subscription.pending", "contains": ["subscription", "payment"], "created_at": int(c.occurred_at.timestamp()),
                "payload": {
                    "subscription": {"entity": {"id": c.ids["subscription_id"], "entity": "subscription", "plan_id": "plan_sim_pro", "status": "pending", "customer_id": c.customer.external_id,
                                                "charge_at": int((c.occurred_at + timedelta(days=1)).timestamp()), "auth_attempts": 1, "paid_count": 3, "remaining_count": 8, "payment_method": c.method, "notes": self._notes(c)}},
                    "payment": {"entity": self._payment_entity(c, status="failed")},
                }}

    def _invoice_expired(self, c: SimCase) -> Dict[str, Any]:
        due = c.occurred_at - timedelta(days=self.rng.choice([5, 12, 20, 35, 50, 75]))
        return {"entity": "event", "account_id": "acc_sim", "event": "invoice.expired", "contains": ["invoice"], "created_at": int(c.occurred_at.timestamp()),
                "payload": {"invoice": {"entity": {"id": c.ids["invoice_id"], "entity": "invoice", "amount": int(c.amount * 100), "amount_paid": 0, "amount_due": int(c.amount * 100), "currency": "INR",
                                                    "status": "expired", "expire_by": int(due.timestamp()), "issued_at": int((due - timedelta(days=30)).timestamp()),
                                                    "customer_details": {"name": c.customer.name, "email": c.customer.email, "contact": c.customer.phone, "customer_id": c.customer.external_id},
                                                    "notes": self._notes(c)}}}}

    def _captured_payment(self, cust: SimCustomer, *, amount: Decimal, method: str, bank: str, at: datetime, batch_tag: bool = True) -> Dict[str, Any]:
        pid = f"pay_{uuid.UUID(int=self.rng.getrandbits(128)).hex[:14]}"
        notes = {"customer_id": cust.external_id, "customer_name": cust.name, "replay": True}
        if batch_tag:
            notes["batch_id"] = self.batch_id
        return {"entity": "event", "account_id": "acc_sim", "event": "payment.captured", "contains": ["payment"], "created_at": int(at.timestamp()),
                "payload": {"payment": {"entity": {"id": pid, "entity": "payment", "amount": int(amount * 100), "currency": "INR", "status": "captured", "method": method, "bank": bank,
                                                    "email": cust.email, "contact": cust.phone, "notes": notes, "created_at": int(at.timestamp()), "captured": True}}}}

    # ------------------------------------------------------------------ recovery events (used by the replay when the model says "pays")

    def recovery_event(self, c: SimCase, *, at: datetime, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        amount = amount or c.amount
        pid = f"pay_{uuid.UUID(int=self.rng.getrandbits(128)).hex[:14]}"
        pay_ent = self._payment_entity(c, status="captured", payment_id=pid, at=at)
        pay_ent["amount"] = int(amount * 100)
        if c.surface == "SUBSCRIPTION_MANDATE_FAILURE":
            return {"entity": "event", "event": "subscription.charged", "created_at": int(at.timestamp()), "payload": {
                "subscription": {"entity": {"id": c.ids["subscription_id"], "status": "active", "paid_count": 4, "customer_id": c.customer.external_id, "notes": self._notes(c)}},
                "payment": {"entity": pay_ent}}}
        if c.surface == "RECEIVABLE_OVERDUE":
            pay_ent["invoice_id"] = c.ids["invoice_id"]
            return {"entity": "event", "event": "invoice.paid", "created_at": int(at.timestamp()), "payload": {
                "invoice": {"entity": {"id": c.ids["invoice_id"], "amount": int(c.amount * 100), "amount_paid": int(amount * 100), "amount_due": 0, "currency": "INR", "status": "paid",
                                       "customer_details": {"name": c.customer.name, "email": c.customer.email, "contact": c.customer.phone, "customer_id": c.customer.external_id}, "notes": self._notes(c)}},
                "payment": {"entity": pay_ent}}}
        if c.surface == "CHECKOUT_ABANDONMENT":
            pay_ent["order_id"] = c.ids["order_id"]
            return {"entity": "event", "event": "order.paid", "created_at": int(at.timestamp()), "payload": {
                "order": {"entity": {"id": c.ids["order_id"], "amount": int(c.amount * 100), "amount_paid": int(amount * 100), "amount_due": 0, "currency": "INR", "status": "paid", "attempts": 2, "notes": self._notes(c)}},
                "payment": {"entity": pay_ent}}}
        return {"entity": "event", "event": "payment.captured", "created_at": int(at.timestamp()), "payload": {"payment": {"entity": pay_ent}}}
