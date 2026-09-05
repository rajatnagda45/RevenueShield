"""Metrics endpoint, request ids, structured logs, PII masking, readiness extras."""
import json
import logging
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.razorpay.security import compute_razorpay_signature
from app.observability.logging import JsonFormatter, PIIMaskingFilter, mask_pii, request_id_var


def test_mask_pii():
    s = mask_pii("call +919876543210 or mail asha.sharma@example.com, upi asha@okhdfcbank")
    assert "9876543210" not in s and "98******10" in s
    assert "asha.sharma@example.com" not in s and "a***@example.com" in s
    assert "asha@okhdfcbank" not in s and "a***@okhdfcbank" in s
    assert mask_pii("order 123 amount 4500") == "order 123 amount 4500"


def test_json_formatter_and_filter_include_context():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "customer +919876543210 paid", (), None)
    PIIMaskingFilter().filter(record)
    token = request_id_var.set("req-123")
    try:
        out = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert out["request_id"] == "req-123" and out["level"] == "INFO" and "9876543210" not in out["msg"]


def test_request_id_header_and_metrics(client: TestClient, db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "obs_secret")
    res = client.get("/health", headers={"X-Request-ID": "abc-1"})
    assert res.headers["x-request-id"] == "abc-1"
    res2 = client.get("/health")
    assert len(res2.headers["x-request-id"]) == 32

    payload = {"event": "payment.failed", "created_at": 1716300500, "payload": {"payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:8]}", "amount": 50000, "currency": "INR", "status": "failed", "method": "upi", "email": "m@x.com", "error_reason": "insufficient_funds"}}}}
    raw = json.dumps(payload).encode()
    client.post("/webhooks/razorpay", content=raw, headers={"Content-Type": "application/json", "X-Razorpay-Signature": compute_razorpay_signature(raw, "obs_secret"), "x-razorpay-event-id": f"evt_{uuid.uuid4().hex[:8]}"})

    m = client.get("/metrics")
    assert m.status_code == 200
    body = m.text
    assert "revenueshield_webhooks_total" in body and 'event_type="payment.failed"' in body
    assert "revenueshield_cases_opened_total" in body and "revenueshield_ledger_rupees_total" in body
    assert "revenueshield_http_request_duration_seconds" in body


def test_readiness_reports_jobs_chain_and_llm(client: TestClient):
    res = client.get("/health/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready" and "jobs" in body and body["audit_chain"]["ok"] is True and body["llm"]["provider"] in ("null", "anthropic", "openai")
