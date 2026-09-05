# Five-minute pitch (timed)

**0:00-0:30 - The problem, in rupees.**
"Every Razorpay merchant loses money in four places: payments that fail, subscription mandates that stop
charging, checkouts that get abandoned, and invoices that go overdue. Most tools send a reminder and call
whatever comes back 'recovered'. I wanted a system that could prove how much money it actually caused to
come back, and stay inside the rules while doing it."

**0:30-1:30 - The bar: measured money.** *(Overview page)*
"RevenueShield assigns every case to treatment or a fifteen-percent holdout with a hash, so it can never
cheat. In this 1,000-case replay, treatment recovered 43% of cases against 23% in the holdout: a twenty-point
lift, p below one in a million. That is Rs 4.4 million of incremental recovery with a confidence interval,
reconciled to Razorpay settlements, not just captures. The lift holds on every one of the four surfaces. Zero policy violations. Every executed action has an audit row, and the audit log is
hash-chained."

**1:30-2:30 - The agent, bounded.** *(Case detail -> Run recovery agent)*
"Where judgment helps, an LLM plans: it reads a dossier that already says which actions are blocked and why,
uses a handful of tools, and submits a structured plan. Where correctness matters, code decides: the policy
engine, the consent ledger, RBI's 8-to-7 calling window, TRAI DND, three touches a week, the negotiation
envelope, and a validator that rejects intimidating or wrong-amount messages and falls back to the approved
template. High-value calls and escalations wait in an approval queue. If the model is down, a circuit breaker
opens and the deterministic planner takes over - you can see 354 degraded runs in this batch."

**2:30-3:30 - Payment degradation.** *(Degradation page)*
"On day two the replay injects a fifteen-minute HDFC-UPI outage. The monitor sees the failure rate spike
against the seven-day baseline, opens an incident, and holds every affected case: no retries, no messages,
no touch consumed. New failures in that cell are diagnosed as systemic, not the customer's fault. When the
issuer recovers, plans are released with jitter so we don't storm the bank."

**3:30-4:15 - Compliance and audit.** *(Approvals, Audit pages)*
"A customer who replies STOP, says 'do not call me' on a call, or turns out to be a wrong number is written
to a consent ledger and every channel stops immediately - that path is covered by a real Twilio form-body
test, which is how I found that the v1 inbound webhook could never have worked in production. Edit one
historical audit row and the chain verifier points at it."

**4:15-5:00 - Build quality and what broke.**
"406 tests, lint clean, Postgres-backed job outbox with retries, JSON logs with request ids and PII masking,
Prometheus metrics, one `docker compose up`. Eight postmortems are in the repo: the holdout made a tenth of
the legacy tests flaky by design; a silent rollback hid a real bug behind a clean scorecard; static analysis
found three more NameErrors in untested branches. The measurement harness is allowed to fail loudly now,
because a harness that recovers from errors stops measuring the system and starts measuring itself."

**Close.** "Find revenue that's slipping away and win it back - and be able to prove it."
