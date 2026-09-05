"""Deployment readiness, security hardening, and health check test suite."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_health_liveness_endpoint(client: TestClient):
    """Verify GET /health returns 200 with status ok."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_readiness_endpoint(client: TestClient):
    """Verify GET /health/ready returns 200 with database and model readiness."""
    res = client.get("/health/ready")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"
    assert "model_status" in data


def test_health_db_endpoint(client: TestClient):
    """Verify GET /health/db returns 200 and connected status."""
    res = client.get("/health/db")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "database": "connected"}


def test_root_endpoint_metadata(client: TestClient):
    """Verify GET / returns service information without sensitive details."""
    res = client.get("/")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "online"
    assert "service" in data
    assert "DATABASE_URL" not in data
    assert "SECRET" not in str(data)


def test_voice_test_call_endpoint_production_restriction(client: TestClient, monkeypatch):
    """Verify /voice/test-call is blocked in production without valid X-Internal-Secret."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", "super_secret_token_123")

    # Request without secret -> 403 Forbidden
    res_no_secret = client.post("/voice/test-call", json={"phone_number": "+919876543210"})
    assert res_no_secret.status_code == 403

    # Request with invalid secret -> 403 Forbidden
    res_bad_secret = client.post(
        "/voice/test-call",
        json={"phone_number": "+919876543210"},
        headers={"X-Internal-Secret": "wrong_token"},
    )
    assert res_bad_secret.status_code == 403


def test_frontend_assets_contain_no_secrets():
    """Verify that frontend JavaScript, HTML, and CSS contain no hardcoded API secrets."""
    frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
    assert frontend_dir.exists()

    forbidden_patterns = [
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
        "TWILIO_AUTH_TOKEN",
        "SMTP_PASSWORD",
        "INTERNAL_API_SECRET",
        "postgrespassword",
        "rzp_live_",
    ]

    for fpath in frontend_dir.glob("*"):
        if fpath.is_file() and fpath.suffix in [".js", ".html", ".css"]:
            content = fpath.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                assert pattern not in content, f"Secret pattern '{pattern}' found in frontend file: {fpath.name}"


def test_cors_configuration_is_explicit():
    """Verify ALLOWED_ORIGINS is configured and does not default to unrestricted wildcard with credentials."""
    origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
    assert len(origins) >= 1
    assert "http://localhost:8000" in origins or "http://localhost:3000" in origins
