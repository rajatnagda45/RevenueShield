"""Recovery ledger and settlement reconciliation tests."""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.ledger.service import LedgerEntryType, LedgerService
from app.ledger.settlement_reconciliation import SettlementReconRow, SettlementReconciliationService
from app.models.ledger_entry import LedgerEntry
from app.outcomes.engine import OutcomeEngine
from tests.helpers_v2 import make_case, make_customer


def test_record_is_idempotent_on_natural_key(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer, amount=1000)
    e1 = LedgerService.record(db_session, case.id, LedgerEntryType.AT_RISK, 1000, provider_reference="pay_1")
    e2 = LedgerService.record(db_session, case.id, LedgerEntryType.AT_RISK, 1000, provider_reference="pay_1")
    assert e1.id == e2.id
    assert len(LedgerService.entries_for_case(db_session, case.id)) == 1


def test_capture_type_full_vs_partial_and_balance(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer, amount=1000)
    LedgerService.post_at_risk(db_session, case, "pay_x")
    partial = LedgerService.post_capture(db_session, case, Decimal("400"), provider_reference="pay_p1")
    assert partial.entry_type == LedgerEntryType.RECOVERED_PARTIAL.value
    full = LedgerService.post_capture(db_session, case, Decimal("1000"), provider_reference="pay_p2")
    assert full.entry_type == LedgerEntryType.RECOVERED_CAPTURE.value
    LedgerService.post_refund(db_session, case, Decimal("100"), provider_reference="rfnd_1")
    LedgerService.post_cost(db_session, case, channel="WHATSAPP", provider_reference="comm_1")

    bal = LedgerService.case_balance(db_session, case.id)
    assert bal["recovered_gross"] == 1400.0
    assert bal["recovered_net_of_refunds"] == 1300.0
    assert bal["cost"] == 0.80
    assert bal["outstanding"] == 0.0


def test_outcome_engine_posts_capture_with_payment_reference(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer, amount=2500)
    LedgerService.post_at_risk(db_session, case, "pay_abc")
    OutcomeEngine.process_payment_capture(
        db_session, case, captured_amount=Decimal("2500"), captured_at=datetime.now(timezone.utc),
        provider_event_id="evt_1", provider_payment_id="pay_abc",
    )
    entry = db_session.scalar(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "RECOVERED_CAPTURE"))
    assert entry is not None and entry.provider_reference == "pay_abc"
    assert case.status == "RECOVERED"


def test_settlement_reconciliation_matches_recovered_payments(db_session: Session):
    customer = make_customer(db_session)
    case = make_case(db_session, customer, amount=2000)
    LedgerService.post_at_risk(db_session, case, "pay_s1")
    LedgerService.post_capture(db_session, case, Decimal("2000"), provider_reference="pay_s1")

    rows = [
        SettlementReconRow(entity_id="pay_s1", settlement_id="setl_1", amount=Decimal("1960.00"), settlement_utr="UTR123"),
        SettlementReconRow(entity_id="pay_unknown", settlement_id="setl_1"),
    ]
    summary = SettlementReconciliationService.reconcile_rows(db_session, rows)
    assert summary["matched_count"] == 1 and summary["unmatched_count"] == 1
    assert summary["settled_amount"] == 1960.0

    # second run is idempotent
    summary2 = SettlementReconciliationService.reconcile_rows(db_session, rows)
    assert summary2["matched_count"] == 1
    settled = db_session.scalars(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "SETTLED")).all()
    assert len(settled) == 1
    assert LedgerService.case_balance(db_session, case.id)["settled"] == 1960.0


def test_from_razorpay_row_normalises_paise():
    row = SettlementReconRow.from_razorpay({"entity_id": "pay_z", "settlement_id": "setl_z", "amount": 123456, "settled_at": 1716300500, "type": "payment"})
    assert row.amount == Decimal("1234.56")
    assert row.settled_at.tzinfo is not None


def test_ledger_api(client: TestClient, db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)
    customer = make_customer(db_session)
    case = make_case(db_session, customer, amount=500)
    LedgerService.post_at_risk(db_session, case, "pay_api")
    LedgerService.post_capture(db_session, case, Decimal("500"), provider_reference="pay_api")

    res = client.post("/ledger/reconcile", json={"rows": [{"entity_id": "pay_api", "settlement_id": "setl_api"}]})
    assert res.status_code == 200, res.text
    assert res.json()["matched_count"] == 1

    res = client.get(f"/ledger/cases/{case.id}")
    assert res.status_code == 200
    body = res.json()
    assert body["balance"]["settled"] == 500.0
    assert {e["entry_type"] for e in body["entries"]} == {"AT_RISK", "RECOVERED_CAPTURE", "SETTLED"}
