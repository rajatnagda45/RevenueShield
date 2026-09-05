"""Security and authentication dependencies for server-to-server internal integrations."""
import logging
import secrets
from typing import Optional
from fastapi import HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader

from app.core.config import settings

logger = logging.getLogger(__name__)

# Standard header schemes
x_internal_secret_header = APIKeyHeader(name="X-Internal-Secret", auto_error=False)
x_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
auth_header = APIKeyHeader(name="Authorization", auto_error=False)
x_legacy_secret_header = APIKeyHeader(name="X-Retell-Secret", auto_error=False)


def verify_internal_api_auth(
    x_internal_secret: Optional[str] = Security(x_internal_secret_header),
    x_api_key: Optional[str] = Security(x_api_key_header),
    authorization: Optional[str] = Security(auth_header),
    x_legacy_secret: Optional[str] = Security(x_legacy_secret_header),
) -> bool:
    """Verify incoming server-to-server request credential against configured secret.

    Supports:
      - X-Internal-Secret: <secret>
      - X-API-Key: <secret>
      - Authorization: Bearer <secret>
      - X-Retell-Secret: <secret> (legacy compatibility)

    If INTERNAL_API_SECRET is not configured (e.g. in default local development),
    requests are permitted. When configured, constant-time validation is strictly enforced.
    """
    configured_secret = settings.INTERNAL_API_SECRET
    if not configured_secret:
        # Development mode when secret is not configured
        return True

    # Extract provided token
    provided_token = None
    if x_internal_secret:
        provided_token = x_internal_secret.strip()
    elif x_api_key:
        provided_token = x_api_key.strip()
    elif x_legacy_secret:
        provided_token = x_legacy_secret.strip()
    elif authorization:
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided_token = parts[1].strip()
        else:
            provided_token = authorization.strip()

    if not provided_token or not secrets.compare_digest(provided_token, configured_secret):
        logger.warning("[AUTH_UNAUTHORIZED] Rejected request with missing or invalid authentication credential.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing API authentication credential.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return True


# Backward-compatible alias
verify_retell_auth = verify_internal_api_auth
