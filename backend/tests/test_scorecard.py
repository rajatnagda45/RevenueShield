"""Batch scorecard tests: incremental lift, compliance, audit coverage."""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.analytics.scorecard import ScorecardService
from app.analytics.stats import bootstrap_rate_difference, two_proportion_ztest, wilson_interval
from app.ledger.service import LedgerService
from app.models.audit_log import AuditLog
from app.models.communication import Communication
from tests.helpers_v2 import make_case, make_customer


def _seed_batch(db: Session, batch_id: str, n_treat=20, treat_recovered=12, n_hold=20, hold_recovered=4):
    customer = make_customer(db)
    cases = []
    for i in range(n_treat):
        c = make_case(db, customer, amount=1000, batch_id=batch_id, experiment_arm="TREATMENT")
        LedgerService.post_at_risk(db, c, f"pay_t{i}_{c.id.hex[:6]}")
        LedgerService.post_cost(db, c, channel="WHATSAPP", provider_reference=f"comm_{c.id}")
        db.add(Communication(
            recovery_case_id=c.id, customer_id=customer.id, channel="WHATSAPP", provider="DRY_RUN",
            template_name="T", template_version="v1", language="ENGLISH", recipient_reference="+91", recipient_masked="+91***",
            message_body="hi", status="SENT", idempotency_key=f"k_{c.id}", attempt_number=1, is_simulated=True,
            sent_at=datetime.now(timezone.utc),
        ))
        db.add(AuditLog(recovery_case_id=c.id, actor_type="SYSTEM", actor_id="t", action="WHATSAPP_SENT", entity_type="Communication", entity_id="x", audit_metadata={}))
        if i < treat_recovered:
            LedgerService.post_capture(db, c, Decimal("1000"), provider_reference=f"pay_t{i}_{c.id.hex[:6]}")
            c.status = "RECOVERED"
            c.recovered_amount = Decimal("1000")
            c.closed_at = datetime.now(timezone.utc)
        cases.append(c)
    for i in range(n_hold):
        c = make_case(db, customer, amount=1000, batch_id=batch_id, experiment_arm="HOLDOUT")
        LedgerService.post_at_risk(db, c, f"pay_h{i}_{c.id.hex[:6]}")
        if i < hold_recovered:
            LedgerService.post_capture(db, c, Decimal("1000"), provider_reference=f"pay_h{i}_{c.id.hex[:6]}")
            c.status = "RECOVERED"
            c.recovered_amount = Decimal("1000")
            c.closed_at = datetime.now(timezone.utc)
        cases.append(c)
    db.flush()
    return cases


def test_stats_helpers():
    z, p = two_proportion_ztest(12, 20, 4, 20)
    assert z is not None and z > 2.0 and p < 0.05
    lo, hi = wilson_interval(12, 20)
    assert 0 < lo < 0.6 < hi < 1
    boot = bootstrap_rate_difference([1000] * 12 + [0] * 8, [1000] * 20, [1000] * 4 + [0] * 16, [1000] * 20, iterations=300)
    assert boot["lower"] < boot["point"] < boot["upper"]
    assert abs(boot["point"] - 0.4) < 1e-9
    assert two_proportion_ztest(0, 0, 1, 2) == (None, None)


def test_scorecard_reports_lift_and_zero_violations(db_session: Session):
    _seed_batch(db_session, "batch_sc_1")
    sc = ScorecardService.compute(db_session, batch_id="batch_sc_1", seed=7)

    assert sc["scope"]["cases"] == 40
    t, h = sc["arms"]["TREATMENT"], sc["arms"]["HOLDOUT"]
    assert t["cases"] == 20 and t["recovered_cases"] == 12 and t["amount_recovered"] == 12000.0
    assert h["cases"] == 20 and h["recovered_cases"] == 4

    lift = sc["lift"]
    assert lift["available"] is True
    assert abs(lift["absolute_lift_case_rate"] - 0.4) < 1e-9
    assert lift["p_value"] < 0.05 and lift["significant_at_5pct"]
    assert lift["incremental_rupees_recovered"] == 8000.0
    assert lift["rupee_rate_difference_ci95"][0] < 0.4 < lift["rupee_rate_difference_ci95"][1]

    assert sc["compliance"]["policy_violations"] == 0
    assert sc["compliance"]["holdout_outreach_count"] == 0
    assert sc["audit"]["executed_actions"] == 20 and sc["audit"]["coverage"] == 1.0
    assert sc["costs"]["by_channel"]["WHATSAPP"]["actions"] == 20
    assert sc["costs"]["cost_per_recovered_rupee"] is not None
    assert sc["breakdowns"]["by_arm"]["HOLDOUT"]["cases"] == 20
    assert sc["breakdowns"]["by_surface"]["PAYMENT_FAILURE"]["cases"] == 40
    lbs = sc["breakdowns"]["lift_by_surface"]["PAYMENT_FAILURE"]
    assert lbs["treatment_cases"] == 20 and lbs["holdout_cases"] == 20 and abs(lbs["absolute_lift"] - 0.4) < 1e-9
    assert any("only 20 cases" in w for w in sc["warnings"])


def test_scorecard_flags_outreach_on_holdout_as_violation(db_session: Session):
    cases = _seed_batch(db_session, "batch_sc_2", n_treat=5, treat_recovered=2, n_hold=5, hold_recovered=1)
    holdout_case = next(c for c in cases if c.experiment_arm == "HOLDOUT" and c.status == "OPEN")
    db_session.add(Communication(
        recovery_case_id=holdout_case.id, customer_id=holdout_case.customer_id, channel="EMAIL", provider="X",
        template_name="T", template_version="v1", language="ENGLISH", recipient_reference="a@b.c", recipient_masked="a***",
        message_body="oops", status="SENT", idempotency_key=f"bad_{holdout_case.id}", attempt_number=1, is_simulated=True,
        sent_at=datetime.now(timezone.utc),
    ))
    db_session.flush()
    sc = ScorecardService.compute(db_session, batch_id="batch_sc_2")
    assert sc["compliance"]["holdout_outreach_count"] == 1
    assert sc["compliance"]["violations_by_rule"]["HOLDOUT_ARM_OBSERVE_ONLY"] == 1
    assert sc["compliance"]["policy_violations"] == 1


def test_scorecard_empty_scope(db_session: Session):
    sc = ScorecardService.compute(db_session, batch_id="does_not_exist")
    assert sc["scope"]["cases"] == 0 and "No cases in scope." in sc["warnings"]


def test_scorecard_api(client: TestClient, db_session: Session):
    _seed_batch(db_session, "batch_api", n_treat=6, treat_recovered=4, n_hold=6, hold_recovered=1)
    res = client.get("/scorecard", params={"batch_id": "batch_api"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope"]["cases"] == 12 and body["lift"]["available"]
    res = client.get("/scorecard/batches")
    assert any(b["batch_id"] == "batch_api" for b in res.json())
    res = client.get("/scorecard/experiments")
    assert res.status_code == 200 and "holdout_percent" in res.json()
