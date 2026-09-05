"""Command Center v2 case endpoints."""
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.ledger.service import LedgerService
from tests.helpers_v2 import make_case, make_customer


def test_case_list_filters_and_detail(client: TestClient, db_session: Session):
    customer = make_customer(db_session, name="Kavya Iyer", phone="+919000000071")
    c1 = make_case(db_session, customer, amount=1500, batch_id="v2b", experiment_arm="TREATMENT", leak_surface="PAYMENT_FAILURE")
    make_case(db_session, customer, amount=99000, batch_id="v2b", experiment_arm="HOLDOUT", leak_surface="RECEIVABLE_OVERDUE", case_type="RECEIVABLE_OVERDUE")
    LedgerService.post_at_risk(db_session, c1, "pay_v2")

    res = client.get("/v2/cases", params={"batch_id": "v2b"})
    assert res.status_code == 200 and res.json()["total"] == 2
    res = client.get("/v2/cases", params={"batch_id": "v2b", "arm": "HOLDOUT"})
    assert res.json()["total"] == 1 and res.json()["items"][0]["surface"] == "RECEIVABLE_OVERDUE"
    res = client.get("/v2/cases", params={"batch_id": "v2b", "q": "kavya"})
    assert res.json()["total"] == 2
    item = res.json()["items"][0]
    assert item["customer"]["phone_masked"] and "9000000071" not in item["customer"]["phone_masked"]

    detail = client.get(f"/v2/cases/{c1.id}").json()
    assert detail["dossier"]["case"]["outstanding_amount"] == 1500.0
    assert "action_verdicts" in detail["dossier"]["constraints"] and detail["ledger"]["outstanding"] == 1500.0
    assert client.get("/v2/cases/00000000-0000-0000-0000-000000000000").status_code == 404
