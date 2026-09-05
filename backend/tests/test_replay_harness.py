"""Batch replay harness: the whole system, end to end, measured."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.simulation.customer_model import CustomerBehaviourModel, TouchEvent
from app.simulation.replay import BatchReplay, ChaosConfig, ReplayConfig
from app.simulation.report import render_markdown
from app.simulation.scenarios import ScenarioGenerator

START = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "null")


def test_scenario_generator_is_seeded_and_covers_surfaces():
    a = ScenarioGenerator(seed=3, batch_id="b").generate(n_cases=80, start=START)
    b = ScenarioGenerator(seed=3, batch_id="b").generate(n_cases=80, start=START)
    assert [c.key for c in a.cases] == [c.key for c in b.cases]
    assert [c.amount for c in a.cases] == [c.amount for c in b.cases]
    surfaces = {c.surface for c in a.cases}
    assert surfaces == {"PAYMENT_FAILURE", "SUBSCRIPTION_MANDATE_FAILURE", "CHECKOUT_ABANDONMENT", "RECEIVABLE_OVERDUE"}
    assert sum(1 for c in a.cases if c.systemic) == 45 and a.degradation_window is not None
    ev = next(c for c in a.cases if c.surface == "SUBSCRIPTION_MANDATE_FAILURE").open_events[0]
    assert ev["event"] == "subscription.pending" and ev["payload"]["payment"]["entity"]["notes"]["batch_id"] == "b"


def test_customer_model_prefers_treatment_and_respects_outage():
    m = CustomerBehaviourModel(seed=1)
    organic = m.organic_probability(category="INSUFFICIENT_FUNDS", hours=48)
    uplift = m.uplift_probability(category="INSUFFICIENT_FUNDS", segment="STANDARD", touches=[TouchEvent(channel="WHATSAPP")], touches_before=0, preferred_language="ENGLISH")
    assert 0 < organic < 0.15 and uplift > organic
    fatigued = m.uplift_probability(category="INSUFFICIENT_FUNDS", segment="STANDARD", touches=[TouchEvent(channel="WHATSAPP")], touches_before=4, preferred_language="ENGLISH")
    assert fatigued < uplift
    assert not any(m.pays(category="BANK_TECHNICAL_FAILURE", segment="STANDARD", hours=24, touches=[], touches_before=0, preferred_language="ENGLISH", systemic_active=True) for _ in range(50))


def test_replay_end_to_end_measures_lift_and_zero_violations(db_session: Session):
    cfg = ReplayConfig(n_cases=40, days=5, tick_hours=12, seed=11, holdout_percent=30.0, agent_enabled=True, provider="null",
                       degradation_episode=True, batch_id="replay_test_1", start=START, chaos=ChaosConfig(duplicate_webhook_rate=0.3, llm_outage_hours=(24.0, 48.0)))
    report = BatchReplay(db_session, cfg).run()

    sc = report["scorecard"]
    assert sc["scope"]["cases"] >= 40  # 40 scenario cases + 45 systemic burst cases
    assert sc["arms"]["TREATMENT"]["cases"] > 0 and sc["arms"]["HOLDOUT"]["cases"] > 0
    assert sc["lift"]["available"] is True
    assert sc["arms"]["TREATMENT"]["case_recovery_rate"] > sc["arms"]["HOLDOUT"]["case_recovery_rate"]
    assert sc["compliance"]["policy_violations"] == 0, sc["compliance"]["violations_by_rule"]
    assert sc["compliance"]["holdout_outreach_count"] == 0
    assert sc["audit"]["coverage"] == 1.0
    assert report["audit_chain"]["ok"] is True

    stats = report["replay_stats"]
    assert stats["duplicates_injected"] > 0 and stats["duplicate_side_effects"] == 0
    assert stats["events_duplicate"] == stats["duplicates_injected"]
    assert stats["captures_emitted"] > 0 and stats["plan_evaluations"] > 0

    agent = report["agent"]
    assert agent["runs"] > 0 and agent["outage_refusals"] > 0 and agent["degraded_to_rules"] > 0
    assert report["settlement"]["matched_count"] > 0 and sc["arms"]["TREATMENT"]["amount_settled"] > 0

    # the issuer outage was detected, held cases, and later failures in the cell were diagnosed systemic
    deg = report["degradation"]
    assert deg["incidents"] >= 1 and "HDFC/UPI" in deg["cells"] and deg["affected_cases"] > 0
    assert deg["systemic_diagnoses"] > 0
    assert "SYSTEMIC_ISSUER_DEGRADATION" in sc["breakdowns"]["by_root_cause"]

    md = render_markdown(report)
    assert "Incremental recovery attributable" in md and "Policy violations: **0**" in md


def test_replay_api(client: TestClient, db_session: Session):
    res = client.post("/simulation/replay", json={"n_cases": 12, "days": 3, "tick_hours": 24, "seed": 2, "holdout_percent": 25, "agent_enabled": False, "provider": "null", "degradation_episode": False, "duplicate_webhook_rate": 0.0, "llm_outage": False, "batch_id": "replay_api"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scorecard"]["scope"]["cases"] == 12 and "markdown" in body
