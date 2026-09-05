# RevenueShield Production Readiness & Deployment Guide

This document establishes the authoritative operational runbook and readiness checklist for deploying the RevenueShield Revenue Recovery platform to production environments.

---

## 1. System Architecture & Component Overview

```
                          [ Incoming Traffic / Webhooks ]
                                       │
                                       ▼
                       [ Reverse Proxy / Cloudflare / TLS ]
                                       │
                                       ▼
                     [ FastAPI Application (Gunicorn/Uvicorn) ]
                      Bind: 0.0.0.0:${PORT:-8000} (Non-Root UID 1000)
                                       │
             ┌─────────────────────────┼─────────────────────────┐
             ▼                         ▼                         ▼
   [ PostgreSQL 16 ]         [ Twilio Voice API ]       [ Razorpay Gateway ]
   (Relational Ledger)        (Speech + TwiML)           (Capture + Webhooks)
             │                         │                         │
             ▼                         ▼                         ▼
    [ ML Action Engine ]    [ State Machine / PTP ]    [ Audit Trail & KPIs ]
```

---

## 2. Environment Configuration Matrix

The application expects environment variables configured via container environment or secure secret stores (AWS Secrets Manager, GCP Secret Manager, Vault).

| Variable Name | Classification | Description & Format |
| :--- | :--- | :--- |
| `DATABASE_URL` | `[REQUIRED]` | `postgresql+psycopg2://<user>:<password>@<host>:5432/<dbname>` |
| `ENVIRONMENT` | `[REQUIRED]` | Set to `production`. Activates error masking & secret protections. |
| `DEBUG` | `[REQUIRED]` | Must be `False` in production. |
| `PORT` | `[REQUIRED]` | Service port (defaults to `8000`, respects platform `$PORT`). |
| `HOST` | `[REQUIRED]` | Must bind to `0.0.0.0` for container/orchestrator ingress. |
| `ALLOWED_ORIGINS` | `[REQUIRED]` | Comma-separated HTTPS frontend URLs (e.g. `https://app.revenueshield.io`). Wildcard `*` prohibited. |
| `INTERNAL_API_SECRET`| `[REQUIRED]` | Cryptographic secret for internal admin/test endpoint authentication (`X-Internal-Secret`). |
| `RAZORPAY_KEY_ID` | `[REQUIRED]` | Razorpay API Key ID (`rzp_live_...` or `rzp_test_...`). |
| `RAZORPAY_KEY_SECRET`| `[REQUIRED]` | Razorpay API Key Secret. |
| `RAZORPAY_WEBHOOK_SECRET` | `[REQUIRED]` | Webhook secret configured in Razorpay dashboard for HMAC-SHA256 signature verification. |
| `EXECUTION_MODE` | `[REQUIRED]` | `live` for real charges, `razorpay_test` for test mode sandbox, `dry_run` for local tests. |
| `TWILIO_ACCOUNT_SID`| `[REQUIRED]` | Twilio Account SID (`AC...`). |
| `TWILIO_AUTH_TOKEN` | `[REQUIRED]` | Twilio Primary Auth Token. |
| `TWILIO_PHONE_NUMBER`| `[REQUIRED]` | Verified Twilio Caller ID in E.164 format (`+1...` or `+91...`). |
| `TWILIO_WEBHOOK_BASE_URL` | `[REQUIRED]` | Public HTTPS base URL (e.g. `https://api.revenueshield.io`) for Twilio TwiML resolution. |
| `SMTP_HOST` | `[REQUIRED]` | Outbound SMTP relay host (e.g. `smtp.sendgrid.net`, `smtp.mailgun.org`). |
| `SMTP_PORT` | `[REQUIRED]` | Outbound SMTP port (`587` for TLS, `465` for SSL). |
| `SMTP_USER` | `[REQUIRED]` | SMTP authentication username. |
| `SMTP_PASSWORD` | `[REQUIRED]` | SMTP authentication password / API key. |
| `SMTP_FROM_EMAIL` | `[REQUIRED]` | From address (e.g. `billing-recovery@yourdomain.com`). |
| `SMTP_FROM_NAME` | `[OPTIONAL]` | Display name (defaults to `RevenueShield Recovery`). |
| `ATTRIBUTION_WINDOW_HOURS` | `[OPTIONAL]` | Recovery attribution window (default: `72` hours). |
| `RETRAINING_SCHEDULE_THRESHOLD` | `[OPTIONAL]` | Minimum new training samples before triggering retraining (default: `50`). |
| `MAX_VOICE_ATTEMPTS` | `[OPTIONAL]` | Voice call cap per case (default: `3`). |
| `VOICE_COOLDOWN_MINUTES` | `[OPTIONAL]` | Cooldown period between call attempts (default: `60` minutes). |
| `DEFAULT_TIMEZONE` | `[OPTIONAL]` | Default timezone for quiet hours evaluation (default: `Asia/Kolkata`). |

---

## 3. Database Deployment & Migration Runbook

### Production Sequence:
1. **Provision Managed PostgreSQL 16+** (AWS RDS, GCP Cloud SQL, Supabase, Neon).
2. **Configure Connection**:
   ```bash
   export DATABASE_URL="postgresql+psycopg2://<user>:<password>@<host>:5432/<database>"
   ```
3. **Execute Alembic Migrations**:
   ```bash
   cd backend
   alembic upgrade head
   ```
4. **Verify Schema Version**:
   ```bash
   alembic current
   ```

---

## 4. Production Startup Commands

### Native Python / VM Startup:
```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 4
```

### Docker Container Startup:
```bash
docker build -t revenueshield-backend:latest ./backend
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  --name revenueshield \
  revenueshield-backend:latest
```

---

## 5. Webhook Endpoints & Security Checklist

All production webhook endpoints require **HTTPS** and strict signature validation:

| Gateway / Provider | Endpoint Path | Method | Auth / Signature Verification |
| :--- | :--- | :--- | :--- |
| **Razorpay Events** | `/webhooks/razorpay` | `POST` | HMAC-SHA256 signature verification using `RAZORPAY_WEBHOOK_SECRET` on header `X-Razorpay-Signature`. |
| **Twilio Voice Inbound/Gather** | `/webhooks/twilio/voice/{call_id}/gather` | `POST` | Voice call session verification & TwiML response. |
| **Twilio Status Callback** | `/webhooks/twilio/voice/{call_id}/status` | `POST` | Provider call status tracking & call duration ledgering. |
| **Twilio WhatsApp Events** | `/webhooks/twilio/whatsapp` | `POST` | WhatsApp status delivery callbacks. |

---

## 6. Health & Readiness Probes

### 1. Liveness Probe (`GET /health`):
- **Purpose**: Verifies the web worker process is running and accepting HTTP requests.
- **Expected Response**: `{"status": "ok"}` (HTTP 200).

### 2. Readiness Probe (`GET /health/ready`):
- **Purpose**: Kubernetes/Docker readiness check verifying database connectivity and ML model availability without leaking credentials.
- **Expected Response**:
  ```json
  {
    "status": "ready",
    "database": "connected",
    "model_status": "active"
  }
  ```
- **Error Response**: HTTP 503 if database is unreachable.

---

## 7. Security Hardening & Zero-Leakage Policy

1. **CORS Restrictions**: `ALLOWED_ORIGINS` rejects unknown domains. Wildcard `*` is disabled when cookies/credentials are supported.
2. **Log Sanitization**: Logs output only safe business IDs (`case_id`, `event_id`, `call_id`). Secrets (`RAZORPAY_KEY_SECRET`, `TWILIO_AUTH_TOKEN`, `SMTP_PASSWORD`, `INTERNAL_API_SECRET`) are never logged.
3. **Production Exception Masking**: Uncaught 500 exceptions log tracebacks internally and return generic operational reference JSON to callers.
4. **Test Endpoint Protection**: `/voice/test-call` is restricted behind `X-Internal-Secret` in `ENVIRONMENT=production`.

---

## 8. Rollback & Disaster Recovery Procedures

1. **Immediate Service Rollback**:
   - Revert container image tag to previous stable build.
2. **Database Rollback**:
   ```bash
   alembic downgrade -1
   ```
3. **Emergency Circuit Breaker**:
   - Set `EXECUTION_MODE=dry_run` to immediately pause automated live payment retries and live outbound calls while preserving webhook ingestion.
