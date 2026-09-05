"""Unit and Integration tests for Twilio Voice recovery calling."""
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from twilio.base.exceptions import TwilioRestException

from app.core.config import settings
from app.integrations.voice.twilio_client import TwilioVoiceClient, DEFAULT_TWIML
from app.services.voice_recovery_service import VoiceRecoveryService, validate_e164_phone


def test_twilio_client_initialization():
    """Test 1: TwilioVoiceClient initializes with settings or explicit args."""
    client = TwilioVoiceClient(
        account_sid="AC_test_sid_123",
        auth_token="auth_test_tok_456",
        from_number="+14155552671",
    )
    assert client.account_sid == "AC_test_sid_123"
    assert client.auth_token == "auth_test_tok_456"
    assert client.from_number == "+14155552671"


def test_missing_account_sid_raises_error(monkeypatch):
    """Test 2: Missing TWILIO_ACCOUNT_SID raises ValueError."""
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", None)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "auth_token")
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+14155552671")

    client = TwilioVoiceClient()
    with pytest.raises(ValueError, match="TWILIO_ACCOUNT_SID is not configured"):
        client.create_outbound_call(to_number="+919876543210")


def test_missing_auth_token_raises_error(monkeypatch):
    """Test 3: Missing TWILIO_AUTH_TOKEN raises ValueError."""
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test_123")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+14155552671")

    client = TwilioVoiceClient()
    with pytest.raises(ValueError, match="TWILIO_AUTH_TOKEN is not configured"):
        client.create_outbound_call(to_number="+919876543210")


def test_missing_twilio_phone_number_raises_error(monkeypatch):
    """Test 4: Missing TWILIO_PHONE_NUMBER raises ValueError."""
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test_123")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "auth_token")
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", None)

    client = TwilioVoiceClient()
    with pytest.raises(ValueError, match="TWILIO_PHONE_NUMBER is not configured"):
        client.create_outbound_call(to_number="+919876543210")


def test_invalid_phone_number_validation():
    """Test 5: E.164 phone number validator rejects invalid strings."""
    assert validate_e164_phone("+919876543210") == "+919876543210"
    assert validate_e164_phone("+14155552671") == "+14155552671"

    # Invalid patterns
    with pytest.raises(ValueError, match="strictly follow E.164"):
        validate_e164_phone("9876543210")  # Missing +

    with pytest.raises(ValueError, match="strictly follow E.164"):
        validate_e164_phone("+012345")  # Leading 0

    with pytest.raises(ValueError, match="Phone number string is required"):
        validate_e164_phone("")

    with pytest.raises(ValueError, match="strictly follow E.164"):
        validate_e164_phone("not-a-phone-number")


def test_successful_mocked_twilio_call(monkeypatch):
    """Test 6: Successful outbound call with mocked Twilio SDK."""
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test_mock_123")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "mock_auth_tok_456")
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+14155552671")

    with patch("app.integrations.voice.twilio_client.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_call = MagicMock()
        mock_call.sid = "CA1234567890abcdef1234567890abcdef"
        mock_call.status = "queued"
        mock_instance.calls.create.return_value = mock_call
        mock_client_cls.return_value = mock_instance

        client = TwilioVoiceClient()
        res = client.create_outbound_call(to_number="+919876543210")

        assert res["call_sid"] == "CA1234567890abcdef1234567890abcdef"
        assert res["status"] == "queued"
        assert res["to_number"] == "+919876543210"
        assert res["from_number"] == "+14155552671"

        # Verify call params sent to Twilio
        mock_instance.calls.create.assert_called_once_with(
            to="+919876543210",
            from_="+14155552671",
            twiml=DEFAULT_TWIML,
        )


def test_twilio_api_failure_handling(monkeypatch):
    """Test 7: Upstream Twilio API error raises clean RuntimeError without leaking secrets."""
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test_mock_123")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "mock_auth_tok_456")
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+14155552671")

    with patch("app.integrations.voice.twilio_client.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.calls.create.side_effect = TwilioRestException(
            status=400,
            uri="/v1/Calls",
            msg="The number +919876543210 is unverified.",
            code=21210,
        )
        mock_client_cls.return_value = mock_instance

        client = TwilioVoiceClient()
        with pytest.raises(RuntimeError, match="Twilio Voice API error: The number.*is unverified"):
            client.create_outbound_call(to_number="+919876543210")


def test_voice_test_call_api_endpoint_success(client: TestClient, monkeypatch):
    """Test 8: POST /voice/test-call API returns valid JSON payload."""
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test_mock_123")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "mock_auth_tok_456")
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+14155552671")
    monkeypatch.setattr(settings, "EXECUTION_MODE", "real")

    with patch("app.integrations.voice.twilio_client.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_call = MagicMock()
        mock_call.sid = "CA999888777666555444333222111000aa"
        mock_call.status = "queued"
        mock_instance.calls.create.return_value = mock_call
        mock_client_cls.return_value = mock_instance

        res = client.post(
            "/voice/test-call",
            json={"phone_number": "+919876543210"},
        )

        assert res.status_code == 200
        data = res.json()
        assert data["call_sid"] == "CA999888777666555444333222111000aa"
        assert data["status"] == "queued"


def test_secrets_never_returned_in_api_or_error(client: TestClient, monkeypatch):
    """Test 9: Secret tokens are never exposed in API responses or error traces."""
    secret_auth_token = "super_secret_auth_token_xyz999"
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test_mock_123")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", secret_auth_token)
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+14155552671")
    monkeypatch.setattr(settings, "EXECUTION_MODE", "real")

    with patch("app.integrations.voice.twilio_client.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.calls.create.side_effect = RuntimeError("Mock network failure")
        mock_client_cls.return_value = mock_instance

        res = client.post(
            "/voice/test-call",
            json={"phone_number": "+919876543210"},
        )

        assert res.status_code == 502
        raw_text = res.text
        assert secret_auth_token not in raw_text


def test_voice_test_call_invalid_phone_returns_400(client: TestClient):
    """Test 10: POST /voice/test-call rejects invalid phone numbers with 400 Bad Request."""
    res = client.post(
        "/voice/test-call",
        json={"phone_number": "invalid_phone_number"},
    )
    assert res.status_code == 400
    assert "strictly follow E.164" in res.json()["detail"]
