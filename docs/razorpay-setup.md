# Razorpay Test Mode Setup & Webhook Configuration Guide

This document provides step-by-step instructions for configuring **Razorpay Test Mode** to receive live test webhooks in the Revenue Recovery AI platform.

---

## 1. Switch to Razorpay Test Mode

1. Log into your [Razorpay Dashboard](https://dashboard.razorpay.com/).
2. On the top-right or left navigation bar, ensure the environment toggle is set to **Test Mode** (indicated by an orange badge or test indicator).
   > [!NOTE]
   > Never use Live Mode API keys or webhook secrets during local development or automated testing.

---

## 2. Generate API Keys (Key ID & Key Secret)

1. Navigate to **Settings** ➔ **API Keys** in the Razorpay Dashboard.
2. Click **Generate Test Key** (or **Regenerate Key** if you already have one).
3. Copy:
   - `Key ID` (starts with `rzp_test_...`)
   - `Key Secret`
4. Store these values securely in your `backend/.env` file:
   ```env
   RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxxx
   RAZORPAY_KEY_SECRET=yyyyyyyyyyyyyyyyyyyyyyyy
   ```

---

## 3. Set Up Local Webhook Forwarding (HTTPS Tunnel)

Razorpay delivers webhooks over the public internet to HTTPS URLs. Since local development runs on `http://127.0.0.1:8000`, you must expose your local port via an HTTPS tunnel.

You can use any tunneling tool such as **ngrok** or **Cloudflare Tunnels**:

### Option A: Using ngrok
```bash
ngrok http 8000
```
Copy the generated public HTTPS URL (e.g. `https://abcd-1234.ngrok-free.app`).

### Option B: Using Cloudflare Tunnel
```bash
cloudflared tunnel --url http://127.0.0.1:8000
```
Copy the generated trycloudflare HTTPS URL (e.g. `https://xxxx.trycloudflare.com`).

---

## 4. Configure Webhooks in Razorpay Dashboard

1. In the Razorpay Dashboard (Test Mode), navigate to **Settings** ➔ **Webhooks**.
2. Click **+ Add New Webhook**.
3. Fill in the webhook parameters:
   - **Webhook URL**: `<YOUR_TUNNEL_URL>/webhooks/razorpay` (e.g. `https://abcd-1234.ngrok-free.app/webhooks/razorpay`)
   - **Secret**: Enter a strong random secret token (e.g. `my_secure_dev_webhook_secret_99`).
   - **Alert Email**: Your email address for notification if webhook delivery fails.
   - **Active Events**: Check the following required events:
     - `payment.failed`
     - `payment.captured`
4. Click **Save** / **Create Webhook**.
5. Copy the Secret you entered and add it to `backend/.env`:
   ```env
   RAZORPAY_WEBHOOK_SECRET=my_secure_dev_webhook_secret_99
   ```

---

## 5. End-to-End Verification Test Flow

### Triggering Test Payment Failures & Successes

1. In the Razorpay Dashboard, go to **Payment Links** ➔ **Create Payment Link** or use the Razorpay Standard Checkout test page.
2. Complete a test transaction using Razorpay's test cards:
   - **Simulate Failure**: Use a test card configured for failure (e.g. invalid OTP or insufficient balance card) to trigger `payment.failed`.
   - **Simulate Recovery**: Complete payment via a valid test card (e.g. `4111 1111 1111 1111`, any future expiry, CVV `123`) to trigger `payment.captured`.
3. Check your FastAPI console output:
   - Ingested event with status `200 OK`.
   - Database creates or transitions `RecoveryCase` from `OPEN` ➔ `RECOVERED`.

---

## 6. Security Best Practices

> [!CAUTION]
> - Never commit `.env` containing live or test secrets to version control.
> - Never log `RAZORPAY_KEY_SECRET` or `RAZORPAY_WEBHOOK_SECRET`.
> - Always verify HMAC-SHA256 signatures before processing payloads.
