# RevenueShield: the five-minute demo

Operator script for the Track 3 demo. Left column is what to do on screen, right column is what to say.
Everything runs offline against the bundled 1,000-case replay database; no Razorpay, Twilio or LLM key is
needed. With an OpenAI key in `backend/.env` the "Run recovery agent" step plans live with GPT-4o.

## 0. Setup (do this before the call, about 10 minutes on a fresh machine)

```bash
# 1. Backend
cd backend
python -m venv venv
venv\Scripts\activate                # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q            # expect: 406 passed

# 2. Optional: GPT-4o planner. Create backend/.env with:
#    OPENAI_API_KEY=sk-...
#    LLM_PROVIDER=auto
#    Then confirm the key works (exit code 0):
DATABASE_URL=sqlite:///../reports/replay_demo_1000.db python scripts/smoke_llm_planner.py

# 3. API on the demo database (terminal 1, leave running)
DATABASE_URL=sqlite:///../reports/replay_demo_1000.db EXPERIMENTS_ENABLED=true python -m uvicorn app.main:app --port 8000

# 4. Command Center (terminal 2, leave running)
cd ../frontend-next
copy .env.example .env.local         # macOS/Linux: cp .env.example .env.local
npm install
npm run dev                          # http://localhost:3000
```

If `reports/replay_demo_1000.db` is missing, regenerate it (about four minutes): `make replay` from the repo root,
or the equivalent command in `docs/demo-runbook.md`.

Open these tabs in order and leave them open: `http://localhost:3000` (Overview), `/scorecard`, `/cases`,
`/degradation`, `/audit`, and `reports/demo_batch_1000.md` in an editor. Set the browser zoom so tables fit.
Pick one treatment case in advance (Cases, filter arm = TREATMENT, status OPEN or IN_PROGRESS, surface
PAYMENT_FAILURE) and keep its URL in the clipboard.

## 1. Script

| Time | Do | Say |
|---|---|---|
| 0:00 | Overview page. Point at the incremental-recovery tile. | "Every Razorpay merchant loses money in four places: failed payments, subscription mandates that stop charging, abandoned checkouts, overdue invoices. Most tools send a reminder and call whatever comes back 'recovered'. RevenueShield proves how much money it actually caused to come back, and stays inside the rules while doing it." |
| 0:30 | Scorecard page. Select batch `demo_batch_1000`. Point at the Arms table, then the lift tile. | "Every case is hashed into treatment or a fifteen-percent holdout, so the system can never grade itself. In this thousand-case batch, treatment recovered 43 percent of cases against 23 percent in the holdout: a twenty-point lift, p below one in a million, Rs 44 lakh of incremental recovery with a confidence interval, reconciled to settlements rather than captures." |
| 1:00 | Scroll to "Lift by surface". Then the Compliance and Audit panels. | "The lift holds on all four surfaces, not just the easy one. Zero policy violations, zero contacts to holdout customers, zero messages after money landed. Every executed action has an audit row and the log is hash-chained." |
| 1:30 | Cases page. Filter arm = HOLDOUT, open one. Show the verdicts list. | "Here is what 'never cheat' looks like: on a holdout case every action is blocked by one rule, observe only. The customer still pays organically or not, and that is the baseline." |
| 2:00 | Paste the prepared treatment case URL. Press **Run recovery agent**. Expand the trace. | "Where judgment helps, an LLM plans. It reads a dossier that already states which actions are blocked and why, calls a tool or two, and must submit a structured plan. Where correctness matters, code decides: the policy engine, the consent ledger, RBI's eight-to-seven calling window, TRAI DND, three touches a week, the negotiation envelope, and a validator that rejects intimidating or wrong-amount text. If the model is down or refuses, a circuit breaker opens and the deterministic planner takes over; this batch shows 354 such runs." |
| 2:45 | Approvals page. | "High-value calls, escalations, waivers and closes stop here for a human. The agent proposes; it never spends money or reputation on its own." |
| 3:00 | Degradation page. Point at the ringed HDFC/UPI cell, then the incident row. | "On day two the replay injects a fifteen-minute HDFC UPI outage. The monitor compares each bank-and-method cell to its seven-day baseline, opens an incident, and holds 87 affected cases: no retries, no messages, no touch consumed. Twelve new failures were diagnosed as the bank's problem, not the customer's. When the issuer recovers, plans release with jitter so we don't storm the bank." |
| 3:45 | Audit page. Press **Re-verify now**. | "A customer who replies STOP, says 'do not call me' on a call, or turns out to be a wrong number is written to a consent ledger and every channel stops. Edit one historical audit row and this verifier points at it." |
| 4:15 | Switch to the editor: `reports/demo_batch_1000.md`, then `POSTMORTEMS.md`. | "406 tests, lint clean, Postgres outbox with retries, JSON logs with PII masking, Prometheus metrics, one docker compose up. Eight postmortems are in the repo: the holdout made a tenth of the legacy tests flaky by design; a silent rollback hid a real bug behind a clean scorecard; a test went red after 7 pm because the RBI window closed. The measurement harness fails loudly now, because a harness that recovers from errors stops measuring the system and starts measuring itself." |
| 4:50 | Back to Overview. | "Find revenue that's slipping away and win it back, and be able to prove it." |

## 2. Numbers to have in your head

| Fact | Value | Source |
|---|---|---|
| Cases / holdout | 1,045 cases, 167 in a 15% holdout | `reports/demo_batch_1000.md` |
| Case recovery rate | Treatment 43.3%, holdout 23.4%, +19.9 pts | Scorecard, Arms |
| Significance | z = 4.82, p < 1e-6 | Scorecard, lift |
| Incremental rupees | Rs 43.7 L (95% CI Rs 14.1 L to Rs 67.9 L) | Scorecard, lift |
| Per-surface lift | Payments +22.6, mandates +9.7, checkout +17.4, receivables +27.2 pts | Scorecard, Lift by surface |
| Compliance | 0 violations, 0 holdout contacts, 64 opt-outs honoured | Scorecard, Compliance |
| Audit | 878/878 actions audited, 19,515-row chain intact | Scorecard, Audit; `/audit/verify` |
| Chaos | 111 duplicate webhooks, 0 side effects; 354 runs degraded to rules | Report, Agent section |
| Degradation | 1 HDFC/UPI incident, 87 held, 12 systemic | Degradation page |
| Cost | Rs 106 outreach cost for Rs 68.3 L recovered | Scorecard, Cost |
| Engineering | 406 tests, ruff clean, 20 migrations, 8 postmortems, 39 logged decisions | repo |

## 3. Likely questions

- **"Is the data real?"** The scenarios are generated (seeded, Razorpay-shaped events) because we cannot ship a merchant's payment data. Everything after the webhook is the real code path: HMAC verification, event processor, detectors, policy engine, agent, ledger, scorecard. `make replay-http` posts the same events over HTTP with real signatures. Test mode against a live Razorpay account is documented in `docs/demo-runbook.md` section D.
- **"Why is the rupee lift's CI wide?"** A handful of large receivables dominate rupee totals in either arm. That is why case-rate lift is the primary metric and rupee lift is shown with a bootstrap CI (decision D-038). The 300-case run in `reports/` shows the noise honestly.
- **"What does the LLM actually control?"** Which action, which channel, what words, within a dossier that pre-computes every verdict. It cannot call Razorpay, cannot bypass a block, cannot send an unvalidated message, and cannot approve its own high-value actions. `docs/agent.md`.
- **"Claude or GPT?"** Either, behind one provider interface with one prompt. `LLM_PROVIDER=auto` picks whichever key exists; with neither, a deterministic planner emits the same plan schema. Decision D-039.
- **"How would this run in production?"** Postgres, the outbox worker on a five-minute tick, Razorpay webhooks registered for payment, order, invoice, subscription, refund and settlement events, Twilio for WhatsApp and voice. `ARCHITECTURE.md` section 7 and `docs/deployment.md`.
- **"What broke?"** `POSTMORTEMS.md`, eight entries. Best two: PM-005 (a silent rollback hid a duplicate-step bug behind a clean scorecard) and PM-008 (the suite went red after 19:00 IST because the RBI calling window closed a test's voice call).

## 4. If something goes wrong on the day

- **API will not start**: confirm you are in `backend/` with the venv active and `DATABASE_URL` points at an existing file. `python -c "import app.main"` shows the real error.
- **Command Center shows "Failed to fetch"**: the API is not on port 8000 or `.env.local` has the wrong `NEXT_PUBLIC_API_BASE_URL`. `curl http://127.0.0.1:8000/health/ready` should return JSON.
- **"Run recovery agent" degrades to rules**: the key is missing or invalid. Run the smoke script; the demo still works, the trace simply shows `provider_unavailable` followed by the deterministic plan, which is itself a good story.
- **No time for the UI**: open `reports/demo_batch_1000.md` and walk the same script from the report. Every number in section 2 is in it.
