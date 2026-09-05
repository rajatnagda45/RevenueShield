# RevenueShield — Production Deployment & Operations Guide

This guide details the end-to-end architecture, environment configuration, database migrations, webhook routing, security hardening, and operational runbook for deploying **RevenueShield** to production.

---

## 1. System Architecture Overview

```
                          ┌───────────────────────────┐
                          │   Public Internet / SSL   │
                          └─────────────┬─────────────┘
                                        │
                    ┌───────────────────┴───────────────────┐
                    ▼                                       ▼
     ┌─────────────────────────────┐         ┌─────────────────────────────┐
     │  Razorpay Payment Gateway   │         │    Twilio Voice / Webhook   │
     │   Webhook (HMAC-SHA256)     │         │   Signature (HMAC-SHA1)     │
     └──────────────┬──────────────┘         └──────────────┬──────────────┘
                    │                                       │
                    │ POST /webhooks/razorpay               │ POST /webhooks/twilio/...
                    └───────────────────┬───────────────────┘
                                        ▼
                  ┌───────────────────────────────────────────┐
                  │    Reverse Proxy (Nginx / Cloudflare)     │
                  │   TLS Termination + DDoS + Rate Limit     │
                  └─────────────────────┬─────────────────────┘
                                        │
                                        ▼
                  ┌───────────────────────────────────────────┐
                  │    RevenueShield FastAPI Application      │
                  │  • PolicyEngine Authorization Guard       │
                  │  • Next-Best-Action (NBA) Decision Engine │
                  │  • Twilio Voice Conversation Assistant    │
                  │  • Command Center Dashboard & Analytics   │
                  └─────────────────────┬─────────────────────┘
                                        │
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
         ┌─────────────────────────────┐ ┌─────────────────────────────┐
         │ PostgreSQL Relational Store │ │    Local Model Registry     │
         │  • Immutable Audit Log      │ │  • Scikit-learn Pipelines   │
         │  • Verified Recovery Outcomes│ │  • JSON Calibration Meta   │
         └─────────────────────────────┘ └─────────────────────────────┘
```

---

## 2. Environment Variables & Classification

Configure environment variables securely using a secret manager (AWS Secrets Manager, GCP Secret Manager, or HashiCorp Vault).

### Key Variables:
| Variable | Classification | Description | Production Example |
| :--- | :--- | :--- | :--- |
| `ENVIRONMENT` | **REQUIRED** | Execution environment | `production` |
| `DEBUG` | **REQUIRED** | Debug mode toggle | `False` |
| `DATABASE_URL` | **REQUIRED** | PostgreSQL URI with connection pooling | `postgresql+psycopg2://user:pass@host:5432/db` |
| `ALLOWED_ORIGINS` | **REQUIRED** | Explicit CORS origin whitelist | `https://app.yourdomain.com` |
| `RAZORPAY_KEY_ID` | **REQUIRED** | Razorpay Live API Key ID | `rzp_live_...` |
| `RAZORPAY_KEY_SECRET` | **REQUIRED** | Razorpay Live API Secret | `...` |
| `RAZORPAY_WEBHOOK_SECRET` | **REQUIRED** | Webhook verification secret | `...` |
| `TWILIO_ACCOUNT_SID` | **REQUIRED** | Twilio Account SID | `AC...` |
| `TWILIO_AUTH_TOKEN` | **REQUIRED** | Twilio Primary Auth Token | `...` |
| `TWILIO_PHONE_NUMBER` | **REQUIRED** | Outbound Twilio Phone Number | `+14155550199` |
| `TWILIO_WEBHOOK_BASE_URL` | **REQUIRED** | Public HTTPS API base URL | `https://api.yourdomain.com` |
| `SMTP_HOST` | **REQUIRED** | SMTP Relay Host | `smtp.sendgrid.net` |
| `SMTP_PORT` | **REQUIRED** | SMTP Port | `587` |
| `SMTP_USER` | **REQUIRED** | SMTP Username | `apikey` |
| `SMTP_PASSWORD` | **REQUIRED** | SMTP Password | `...` |
| `INTERNAL_API_SECRET` | **REQUIRED** | 64-char secret for server-to-server auth | `...` |
| `ATTRIBUTION_WINDOW_HOURS` | **OPTIONAL** | Outcome attribution window | `72` |

---

## 3. Database Initialization & Alembic Migrations

1. **Verify Database Connection**:
   ```bash
   psql -h <host> -U <user> -d revenue_recovery -c "SELECT 1;"
   ```

2. **Execute Database Migrations**:
   ```bash
   cd backend
   alembic upgrade head
   ```

3. **Verify Indexes & Table Creation**:
   Ensure tables `recovery_cases`, `recovery_outcomes`, `diagnoses`, `promises_to_pay`, `voice_calls`, `audit_logs`, `recovery_plans`, `recovery_plan_steps` exist.

---

## 4. Razorpay Webhook Configuration

1. In Razorpay Dashboard $\to$ **Settings** $\to$ **Webhooks**:
   - **Webhook URL**: `https://api.yourdomain.com/webhooks/razorpay`
   - **Secret**: Enter a strong random secret and copy to `RAZORPAY_WEBHOOK_SECRET`.
   - **Active Events**:
     - `payment.failed` (triggers Diagnosis & recovery pipeline)
     - `payment.captured` (triggers authoritative recovery & stops plans)
     - `order.paid`
     - `payment_link.paid`
2. **Security & Idempotency**:
   - Signature is verified using HMAC-SHA256 via `X-Razorpay-Signature`.
   - Duplicate events are logged and deduplicated without double-counting recovered funds.

---

## 5. Twilio Voice & Webhook Configuration

1. In Twilio Console $\to$ **Phone Numbers** $\to$ **Active Numbers** $\to$ Select Number:
   - **Voice Configuration**:
     - **A Call Comes In**: Webhook `https://api.yourdomain.com/webhooks/twilio/voice` (HTTP POST)
   - **Status Callback URL**:
     - `https://api.yourdomain.com/webhooks/twilio/status` (HTTP POST)
2. **Speech Recognition TwiML Routing**:
   - Voice agent speaks Amazon Polly English prompt and gathers speech via `<Gather input="speech" action="/webhooks/twilio/voice/{call_id}/gather">`.
   - Multi-turn conversation state machine processes customer responses and captures Promise-to-Pay commitments safely.

---

## 6. Docker Deployment

To launch the complete stack locally or on a single VM:

```bash
docker compose up -d --build
```

Verify running containers:
```bash
docker compose ps
```

---

## 7. Health & Readiness Verification

Test backend probes:

1. **Liveness Check**:
   ```bash
   curl -f https://api.yourdomain.com/health
   # Response: {"status": "ok"}
   ```

2. **Readiness Probe**:
   ```bash
   curl -f https://api.yourdomain.com/health/ready
   # Response: {"status": "ready", "database": "connected", "model_status": "active"}
   ```

3. **Command Center Access**:
   Access the web console at `https://app.yourdomain.com/portal` or `http://localhost:8000/portal`.

---

## 8. Security Hardening Checklist

- [x] CORS whitelist configured with explicit origin URLs.
- [x] No server-side secrets or credentials bundled into frontend code.
- [x] Test trigger endpoints (`/voice/test-call`) protected by `X-Internal-Secret` in production.
- [x] Webhook signatures cryptographically validated on Razorpay (`X-Razorpay-Signature`) and Twilio (`X-Twilio-Signature`).
- [x] Division-by-zero safety in dashboard analytics.
- [x] Sensitive customer credentials (OTP, CVV, passwords) sanitized by voice safety guard.
- [x] Non-root user in Docker container (`appuser`).
- [x] Generic error messages returned in production without leaking internal stack traces.
