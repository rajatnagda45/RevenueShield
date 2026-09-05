# RevenueShield v2 — Expansion Blueprint for Razorpay AI Buildathon, Track 3 (AI Revenue Recovery)

> Status: living document. Phases are checked off as they land in code. Decisions with tradeoffs are
> recorded in `decision.md`. Nothing here is committed or pushed until the owner says so.

---

## 0. Why the current build "looks small" and what the judges are actually scoring

**Track 3 problem statement (verbatim from razorpay.com/buildathon):**
"Find revenue that's slipping away and win it back. Build an agent detecting revenue at risk,
determining appropriate interventions, and executing bounded recovery workflows across payment
failures, checkout abandonment, and overdue receivables."

**The Bar (verbatim):** "Don't just identify the problem. Show measured money recovered across a
batch, with compliant escalation, stopping rules, and an audit trail."

**Example directions listed by Razorpay:** payment degradation diagnosis, checkout recovery,
failed-subscription recovery, B2B receivables chasing, mandate retry sequencing, voice-based
recovery, payment commitment tracking.

**Judging criteria (Razorpay's published rubric):**
1. **Problem Taste** - a meaningful, real merchant problem.
2. **Build Quality** - clean repo, execution reliability, code you can trust.
3. **AI Judgment** - use AI where it adds judgment, deterministic code where it must be correct.
4. **Failure Recovery** - show what broke while building and how it was fixed.

Plus: public repo, README, 5-minute pitch video, architecture walkthrough. Judges prefer a
complete working project over an ambitious half-finished one.

### Honest gap analysis of RevenueShield v1 (what exists today)

| Area | v1 today | Gap vs. the Bar |
|---|---|---|
| Surfaces covered | Payment failures (subscription/one-off) only | Track explicitly names **checkout abandonment** and **overdue receivables**; also **mandate retry sequencing** |
| "Measured money recovered across a batch" | Dashboard sums `RecoveryOutcome.amount_recovered`; attribution is a time-window heuristic | No **holdout/control group**, so "recovered" cannot be distinguished from "would have paid anyway". No batch-level scorecard, no confidence interval, no reconciliation to **settlements** |
| Agent | Zero LLM usage. Regex intent extraction, string templates, rule/ML scoring | Track title is *AI* Revenue Recovery: "Build an agent". No planner, no bounded tool use, no negotiation, no human-in-the-loop approvals, no agent trace |
| Payment degradation diagnosis | Per-payment root cause only | No portfolio-level detection of issuer/PSP outages; system would spam customers during an HDFC UPI outage |
| Compliance | Quiet hours 20:00-08:00, 3-attempt cap per channel, PTP pause | RBI Fair Practices Code for recovery calls is **08:00-19:00**; caps are per-channel not per-customer; no consent/opt-out ledger; no DND registry check; audit log is not tamper-evident |
| Failure Recovery | Not documented | Need `POSTMORTEMS.md` + chaos tests (webhook storms, provider 5xx, LLM outage) |
| Frontend | Vanilla JS single page | Fine for KPIs, but cannot show agent traces, approval queue, experiment scorecard, degradation heatmap |
| Ops | Manual scripts, no scheduler, no metrics | No job queue, no OTel/Prometheus, no circuit breakers, no backoff on Razorpay/Twilio calls |

**Thesis of v2:** keep every deterministic engine that is already correct (diagnosis rules,
policy engine, execution guard, outcome engine, ML NBA) and wrap them in four things the judges
are explicitly asking for: **(a)** all three leak surfaces + mandates, **(b)** a measurement layer
that proves incremental money (holdout + settlement reconciliation), **(c)** a real bounded
LLM agent with human approvals and full traces, **(d)** production ops + a failure-recovery
narrative. The demo becomes: *"one command replays 1,000 Razorpay-shaped events through the
system; the scorecard shows Rs X recovered vs Rs Y in the holdout, lift with a confidence interval,
zero policy violations, 100% audit coverage, and here is the agent's reasoning for any case."*

---

## 1. Product positioning (the pitch in one paragraph)

RevenueShield is an **autonomous, bounded revenue-recovery agent for Razorpay merchants**. It
watches every leak surface a merchant has on Razorpay (failed payments, halted subscription
mandates, abandoned checkouts/orders, overdue invoices), diagnoses whether the leak is
*idiosyncratic* (this customer) or *systemic* (this bank is down), and runs a recovery plan under
hard policy rails: RBI/TRAI-compliant contact windows, per-customer frequency caps, consent and
DND checks, promise-to-pay pauses, and instant stop on capture. An LLM agent handles the parts
that need judgment - reading the customer's history, choosing and personalising the intervention,
negotiating partial/instalment payments inside a merchant-set envelope, deciding when to hand off
to a human - while every irreversible action passes through deterministic validators. Every case is
randomly assigned to treatment or holdout, so the merchant sees **incremental** rupees recovered,
reconciled against Razorpay settlements, not vanity attribution.

---

## 2. Target architecture (v2)

```mermaid
flowchart LR
  subgraph Razorpay
    WH[Webhooks: payment.*, order.*, invoice.*, subscription.*, payment_link.*, settlement.*]
    API[REST: Payments, Orders, Invoices, Subscriptions, Payment Links, Settlements]
    MCP[Razorpay MCP Server - optional tool transport]
  end
  WH --> ING[Ingestion: HMAC verify, normalise, idempotent Event store]
  ING --> DET[Leak Detector: opens RecoveryCase per surface]
  DET --> EXP[Experiment Assigner: treatment / holdout]
  DET --> DIAG[Diagnosis: per-payment root cause]
  DET --> DEG[Degradation Monitor: bank x method health]
  EXP -->|treatment| AGENT[Recovery Agent - LLM planner with bounded tools]
  EXP -->|holdout| OBS[Observe only]
  DIAG --> AGENT
  DEG --> POL
  AGENT --> POL[Policy Engine v2: RBI hours, caps, consent, DND, PTP, degradation hold]
  POL --> APPR{Needs human approval?}
  APPR -->|yes| QUEUE[Approval Queue]
  APPR -->|no| EXEC[Execution: retry, payment link, WhatsApp, email, voice, plan offer]
  QUEUE --> EXEC
  EXEC --> API
  EXEC --> TW[Twilio Voice/WhatsApp, SMTP]
  API --> WH
  WH --> OUT[Outcome Engine: capture -> RECOVERED, stop everything]
  OUT --> LEDGER[Recovery Ledger: reconciled to settlements]
  LEDGER --> SCORE[Batch Scorecard: lift, CI, cost, violations=0]
  AGENT --> TRACE[Agent Trace store]
  POL --> AUDIT[Hash-chained Audit Log]
  EXEC --> AUDIT
  OUT --> LEARN[Learning loop: point-in-time features, retrain]
```

**Stack:** FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL (SQLite for tests), scikit-learn,
Anthropic Python SDK (Claude) with provider abstraction and a deterministic `NullLLM` fallback,
Twilio, httpx. Frontend: Next.js 15 + TypeScript + Tailwind. Ops: Postgres-backed job outbox +
worker process, OpenTelemetry traces, Prometheus metrics, structured JSON logs, Docker Compose.

---

## 3. Phased build plan

Each phase ships with: migrations (idempotent), typed services, API endpoints, unit + integration
tests, and a doc page. Baseline before any change: 316 tests passing on Windows venv.

### Phase 0 - Baseline & repo hygiene
- [x] Windows-native venv (`backend/venv`), `pip install -r requirements.txt`, run full suite.
- [x] Write `newplan.md`, `decision.md`.
- [x] Add `POSTMORTEMS.md` (Failure Recovery criterion) - append real incidents as they happen.
- [ ] Remove `__MACOSX` zip artefact and flatten `revenue-resilience/revenue-resilience/` before
      publishing (owner action; noted, not done automatically).

### Phase 1 - Measurement layer: experiments, ledger, batch scorecard  (the Bar)  [DONE]
Files: `app/experiments/assigner.py`, `app/models/experiment_assignment.py`,
`app/models/ledger_entry.py`, `app/ledger/service.py`, `app/analytics/scorecard.py`,
`app/api/scorecard.py`, migration `0015_experiments_and_ledger.py`, tests.
- Every new `RecoveryCase` is deterministically hashed (case id + experiment salt) into
  `TREATMENT` or `HOLDOUT` (default holdout 10%, configurable per surface). Holdout cases get a
  plan whose only step is `OBSERVE`; no outreach, no retries. Stopping rules still apply.
- `RecoveryLedger`: append-only money movements per case: `AT_RISK`, `RECOVERED_CAPTURE`,
  `RECOVERED_PARTIAL`, `REFUNDED`, `SETTLED`, `WRITTEN_OFF`. Settlement reconciliation via
  `settlement.processed` webhook / `GET /settlements/recon` marks recovered rupees as
  **settled** (money in the merchant's bank, not just captured).
- `BatchScorecard`: for a batch/time window: at-risk, recovered (captured / settled), recovery
  rate treatment vs holdout, **absolute and relative lift**, two-proportion z-test p-value and
  bootstrap 95% CI on incremental rupees, cost per recovered rupee (channel unit costs), policy
  violations (must be 0), audit coverage (every executed action has a policy decision id and an
  audit row), time-to-recovery percentiles, breakdown by surface / root cause / channel.
- API: `GET /scorecard?batch_id=...|window=...`, `GET /ledger/case/{id}`, `GET /experiments`.

### Phase 2 - Three leak surfaces + mandates  [DONE]
Files: `app/integrations/razorpay/order_adapter.py`, `invoice_adapter.py`,
`subscription_adapter.py`, `app/detectors/checkout_abandonment.py`, `receivables.py`,
`mandates.py`, `app/models/order.py`, `app/models/mandate.py`, migration `0016_surfaces.py`,
scheduler sweeps, tests.
- **Checkout abandonment:** `order.created` / `payment.failed(user_cancelled)` with no
  `order.paid` within N minutes opens `CHECKOUT_ABANDONMENT` case; recovery = payment link with
  the same order amount (Razorpay Payment Link `reference_id = order_id`), single gentle nudge,
  hard cap 2 touches, auto-close on `order.paid`.
- **Overdue receivables (B2B):** Razorpay Invoices (`invoice.expired`, `invoice.partially_paid`)
  plus manual/CSV AR import open `RECEIVABLE_OVERDUE` cases with an ageing bucket (1-7, 8-30,
  31-60, 60+ days). Dunning ladder: reminder -> statement + link -> PTP call -> escalate to
  merchant finance (approval) -> legal-notice draft (approval, never auto-sent).
- **Subscription mandates:** `subscription.pending` / `subscription.halted` / `subscription.charged`
  open/close `SUBSCRIPTION_MANDATE_FAILURE` cases. Mandate retry sequencer respects Razorpay's
  auto-retry (1 + 3) and RBI e-mandate rules: pre-debit notification >= 24h, retry not during
  degraded issuer windows, salary-day-aware retry scheduling (1st-5th / 28th-31st weighting).
- Unified `LeakSurface` enum drives diagnosis, policy, agent prompts and scorecard breakdowns.

### Phase 3 - Payment degradation monitor  [DONE]
Files: `app/degradation/monitor.py`, `app/models/degradation_incident.py`, migration
`0017_degradation.py`, `app/api/degradation.py`, tests.
- Rolling 15-min / 60-min failure rate per `(bank, method, error_source)` vs a 7-day baseline;
  incident opens when rate > baseline + k*sigma with n >= 20 (configurable).
- Incident states: `SUSPECTED -> CONFIRMED -> RECOVERING -> CLOSED`.
- Effects: PolicyEngine blocks `RETRY_PAYMENT` and customer outreach for affected cases with
  blocking rule `ISSUER_DEGRADED_HOLD`; cases are tagged `systemic=true`; retries are rescheduled
  to incident close + jitter; merchant gets one alert, not 400 customer pings.
- Diagnosis gets a new category `SYSTEMIC_ISSUER_DEGRADATION` with the incident id as evidence.

### Phase 4 - Compliance v2 + tamper-evident audit  [DONE]
Files: `app/compliance/contact_policy.py`, `consent.py`, `dnd.py`, `app/models/consent.py`,
`app/models/contact_attempt.py`, `app/audit/chain.py`, migration `0018_compliance.py`, tests.
- Unified `ContactPolicy`: per-customer caps across all channels (default 3 touches / 7 days,
  1 voice call / 3 days), RBI recovery-call window **08:00-19:00 local**, WhatsApp/SMS quiet
  hours 21:00-08:00, no contact on national holidays list, min 24h gap between voice attempts.
- `ConsentLedger`: opt-in/opt-out per channel with source and timestamp; `STOP`/`UNSUBSCRIBE`
  keywords and "do not call me" voice intent write an opt-out immediately and cancel all pending
  steps. Simulated TRAI DND registry check interface (pluggable).
- Dispute / already-paid / harassment complaint -> `HUMAN_HANDOFF` and freeze.
- Hash-chained audit log: each row stores `prev_hash` and `row_hash = sha256(prev_hash || canonical
  json)`. `GET /audit/verify` recomputes the chain and returns first broken link (if any).
- `ComplianceReport` in scorecard: violations by rule, always expected 0; tests that try to
  violate each rule and assert the block.

### Phase 5 - The Recovery Agent (LLM, bounded tools, approvals, traces)  [DONE]
Files: `app/agent/llm.py`, `provider_anthropic.py`, `provider_null.py`, `tools.py`,
`planner.py`, `validators.py`, `runner.py`, `traces.py`, `app/models/agent_run.py`,
`app/models/approval.py`, `app/api/agent.py`, `app/api/approvals.py`, migration
`0019_agent.py`, tests with the `NullLLM` provider (deterministic, offline).
- **Provider abstraction:** `LLMProvider.plan(dossier, tools) -> AgentPlan`. Anthropic provider
  uses the official SDK (`claude-opus-5` for planning/negotiation, `claude-haiku-4-5` for message
  drafting and eval judging), adaptive thinking, structured outputs for the plan schema, prompt
  caching on the stable system prompt and tool list. `NullLLM` reproduces the rule-based NBA so
  tests and demos run with no API key. Provider is env-selected (`LLM_PROVIDER=anthropic|null`).
- **Bounded tool surface** (every tool is a thin, typed wrapper around an existing deterministic
  service; the agent never touches Razorpay directly): `get_case_dossier`, `get_customer_history`,
  `check_policy(action)`, `propose_action(action, rationale)`, `draft_message(channel, tone,
  language)`, `offer_payment_plan(installments, first_amount)`, `schedule_retry(at)`,
  `request_human_approval(reason)`, `record_promise_to_pay(date, amount)`, `handoff_to_human(reason)`,
  `close_case(reason)`. Tool calls are executed by the runner only after `PolicyEngine` and
  `ExecutionGuard` approve; a rejected call is fed back to the model as `is_error` with the rule.
- **Negotiation envelope** (merchant-configurable): max instalments 3, min first payment 30%,
  max due-date extension 14 days, goodwill waiver 0% by default (any waiver -> approval).
  Partial/instalment offers are implemented with Razorpay Payment Links `accept_partial=true`,
  `first_min_partial_amount`.
- **Deterministic validators on LLM output:** amounts must equal ledger amounts, no failure codes
  or internal scores in customer text, language in allowed set, no promises the policy forbids,
  max message length, mandatory opt-out footer on WhatsApp/SMS. Any violation -> regenerate once
  with the violation as feedback, then fall back to the template.
- **Approval queue:** actions above thresholds (amount >= Rs 50,000 voice call, any waiver, legal
  notice, B2B escalation) create `Approval` rows; operators approve/reject in the dashboard with
  a reason; SLA timer; expired approvals auto-reject.
- **Agent traces:** every run stores the dossier hash, prompt/version, each tool call, the policy
  verdict, tokens and cost, and the final plan; viewable per case in the dashboard.
- **Failure behaviour:** LLM timeout/429/5xx -> circuit breaker opens for 60s and the runner uses
  `NullLLM` (rule-based). This is recorded in the trace as `degraded_to_rules=true`.

### Phase 6 - Batch replay harness + customer simulator (the demo engine)  [DONE]
Files: `app/simulation/scenarios.py`, `customer_model.py`, `replay.py`, `report.py`,
`scripts/replay_batch.py`, `app/api/simulation.py`, `tests/test_replay_harness.py`.
- Generates N Razorpay-shaped events across all surfaces with realistic distributions (root cause
  mix from real Razorpay error taxonomy, banks, methods, amounts, segments, time-of-day).
- A parametric customer behaviour model responds to interventions: base pay probability by root
  cause and segment, channel uplift, fatigue penalty, PTP keep-rate, opt-out probability. Seeded
  RNG -> reproducible.
- Drives the real pipeline (webhook endpoint -> cases -> agent -> execution in dry-run -> synthetic
  `payment.captured` webhooks) with a virtual clock so 14 days replay in seconds.
- Output: `BatchScorecard` JSON + Markdown report + a chaos summary (injected duplicate webhooks,
  provider 5xx, LLM outage) proving idempotency and graceful degradation.

### Phase 7 - Production ops  [DONE]
Files: `app/jobs/outbox.py`, `worker.py`, `app/models/job.py`, `app/observability/otel.py`,
`metrics.py`, `logging.py`, `app/integrations/http_resilience.py`, migration `0020_jobs.py`,
`scripts/run_worker.py`.
- Postgres-backed transactional outbox + `SELECT ... FOR UPDATE SKIP LOCKED` worker (no Redis).
  Jobs: plan evaluation ticks, mandate retries, PTP follow-ups, degradation sweeps, approval expiry.
- Exponential backoff with jitter + circuit breakers for Razorpay, Twilio, SMTP, LLM.
- OpenTelemetry tracing (FastAPI + SQLAlchemy instrumentation), `/metrics` Prometheus endpoint,
  structured JSON logs with request ids and case ids, PII masking.
- Compose: `postgres`, `backend`, `worker`, `frontend`; `make demo` target.

### Phase 8 - Command Center v2 (Next.js)  [DONE]
Dir: `frontend-next/` (kept separate from the legacy vanilla portal until parity).
Pages: Overview (KPIs + lift), Scorecard (treatment vs holdout, CI, per-surface), Cases (dossier
+ timeline + agent trace), Approvals (queue with approve/reject), Degradation (bank x method
heatmap + incidents), Ledger (reconciliation to settlements), Audit (chain verify), Replay
(run a batch from the UI and watch it fill).

### Phase 9 - Story & submission assets  [DONE]
- `README.md` rewrite around the Bar; `ARCHITECTURE.md` with diagrams; `POSTMORTEMS.md`;
  `docs/pitch-script.md` (5 minutes, timed); `docs/demo-runbook.md`; `docs/compliance.md`
  (RBI/TRAI references and how each rule maps to code).

---

## 4. Success metrics the demo must show

| Metric | Target in replay of 1,000 events |
|---|---|
| Incremental recovery lift (treatment vs holdout) | >= 20 percentage points, p < 0.01 |
| Policy violations | 0 |
| Audit coverage of executed actions | 100% |
| Duplicate webhook side effects | 0 |
| Cases needing human approval | < 10%, all visible in queue |
| Agent runs degraded to rules during injected LLM outage | 100% of runs in the outage window complete |
| Time to recovery p50 (treatment) | reported |
| Cost per recovered rupee | reported by channel |

---

## 5. Risks and mitigations

- **LLM key not available at demo time** -> `NullLLM` provider makes the entire demo run offline.
- **Razorpay test-mode limits** -> replay uses the real webhook path with dry-run executors; live
  mode is a switch, demonstrated once with a real payment link.
- **Scope creep** -> phases are independent; each leaves the suite green. Frontend v2 is last.
- **Statistical credibility** -> holdout is deterministic by hash, reported with n, CI and p-value;
  small-n warnings displayed.

---

## 6. Progress log
- 2026-09-05: Plan written. Baseline: 314 tests passing on Windows venv.
- 2026-09-05: **Phase 1 done.** Experiment assigner (hash holdout), holdout guards in policy engine, execution
  guard, scheduler (OBSERVE step), WhatsApp/voice/email/payment-link services; append-only ledger with
  AT_RISK / RECOVERED / REFUNDED / SETTLED / COST entries; settlement reconciliation; batch scorecard with
  lift, z-test, bootstrap CI, cost per rupee, compliance violations and audit coverage. `GET /scorecard`,
  `GET /ledger/cases/{id}`, `POST /ledger/reconcile`. 335 tests. PM-001 logged in POSTMORTEMS.md.
- 2026-09-05: **Phase 2 done.** `LeakSurface` profiles; adapter understands order/invoice/subscription/refund/
  settlement entities; `CaseFactory` bootstraps every case identically; checkout abandonment (immediate on
  user-cancel + sweep), receivables (import, ageing, partial/paid), mandates (pending/halted/charged,
  RBI-compliant retry sequencer with pre-debit notice and salary-day alignment); surface-aware policy
  (retry/voice/escalation/touch caps) and decision scoring; `/surfaces/*` API. 348 tests.
- 2026-09-05: **Phase 3 done.** Degradation monitor: (bank, method) failure matrix vs 7-day baseline,
  z-test with absolute-delta floor, SUSPECTED -> CONFIRMED -> RECOVERING -> CLOSED incidents, affected
  cases held (`ISSUER_DEGRADED_HOLD`, no plan step consumed), new failures diagnosed
  `SYSTEMIC_ISSUER_DEGRADATION`, jittered release on close; `/degradation/*` API. 353 tests.
- 2026-09-05: **Phase 4 done.** Consent ledger (keywords incl. Hinglish, voice intents, operator),
  cross-channel ContactPolicy (RBI call window 08-19, message window 08-21, holidays, TRAI DND registry,
  3 touches / 7 days, 1 call / 3 days, 20h min gap), human handoff freeze from dispute / human request /
  wrong number, contact-attempt registry, hash-chained audit log with `/audit/verify`, compliance section
  in the scorecard. Found and fixed two latent v1 production bugs (PM-002, PM-003). 366 tests.
- 2026-09-05: **Phase 5 done.** Recovery Agent: dossier with precomputed verdicts, 15 policy-gated tools,
  Claude planner (Anthropic SDK, adaptive thinking, cached system prompt, `submit_plan` strict tool) with a
  circuit breaker and deterministic NullLLM fallback, MessageValidator / NegotiationEnvelope / PlanValidator,
  approval queue (voice >= Rs 50k, escalation, waiver, close), agent traces with tokens and cost, optional
  agent-driven scheduler (`AGENT_DRIVES_PLANS`), partial-payment links for instalment offers,
  agent-personalised WhatsApp/email bodies. `/agent/*`, `/approvals/*`. 382 tests.
- 2026-09-05: **Phase 6 done.** Batch replay harness: seeded Razorpay-shaped scenario generator across all four
  surfaces with an issuer-outage episode, parametric customer behaviour model (organic vs uplift, fatigue,
  opt-outs, promise keep-rate, simulated operator), virtual-clock replay through the real adapter/event
  processor/scheduler/agent, chaos (duplicate webhooks, LLM outage window), settlement simulation, scorecard
  + Markdown report. `scripts/replay_batch.py`, `POST /simulation/replay`. 386 tests.
  First real run (300 cases, 10 days, agent on, rules planner): treatment 44.9% vs holdout 18.2% case
  recovery, Rs 2.77M incremental (95% CI 1.22M-3.84M), p = 0.014, 0 violations, chain intact, 27 duplicate
  webhooks with 0 side effects, 77 agent runs degraded to rules in the outage window. Report in `reports/`.
- 2026-09-05: **Phase 7 done.** Transactional outbox + worker (SKIP LOCKED on Postgres, backoff, DEAD, recurring
  idempotent ticks), `scripts/run_worker.py`, `/jobs/*`; shared retry/backoff + circuit breaker (SMTP wrapped,
  LLM breaker moved); JSON logs with request ids and PII masking, `X-Request-ID` middleware, Prometheus
  `/metrics`, optional OpenTelemetry; readiness reports jobs, audit-chain head and LLM circuit state; compose
  gains a `worker` service and runs migrations on start; `Makefile`; ruff correctness lint found and fixed 3
  more latent v1 NameErrors (PM-007). 398 tests, lint clean.
- 2026-09-05: **Phase 8 done.** `frontend-next/` (Next.js 16, TypeScript, Tailwind v4): Overview (incremental
  recovery, arms, violations, audit coverage, what needs a human), Scorecard, Cases + case detail (dossier,
  precomputed verdicts, agent traces, ledger, hash-chained audit trail, run-agent / hand-off actions), Approvals
  queue, Agent runs + tool surface, Degradation matrix (single-hue sequential ramp, status ring on detected
  cells) + incidents, Replay runner, Jobs health, Audit verify. Validated palette in both themes; text always
  in text tokens; every metric also tabular. `GET /v2/cases` + `/v2/cases/{id}` added for the UI. Build + lint clean.
  Live smoke against the demo database confirmed real responses on every page's endpoints.
- 2026-09-05: **Phase 9 done.** README rewritten around the Bar, `ARCHITECTURE.md`, `docs/compliance.md` (rule ->
  code -> test), `docs/agent.md`, `docs/measurement.md`, `docs/demo-runbook.md`, `docs/pitch-script.md` (timed),
  `MEMO.md` (built / cut / next / one decision defended), GitHub Actions CI (lint, tests, import smoke, offline
  replay artefact, frontend lint + build). Replay monitor now on 5-minute slices so the injected outage is
  detected mid-burst and later failures are diagnosed systemic (D-037). 399 tests, lint clean.

- 2026-09-05: **Headline batch.** 1,000 cases (1,045 after mandate/receivable expansion), 15% holdout, seed 7,
  agent on with the deterministic planner, 212 s wall clock: treatment 43.3% vs holdout 23.4% case recovery,
  +19.9 pts (relative 85%), z = 4.82, p < 1e-6, Rs 43.7 L incremental (95% CI Rs 14.1 L to Rs 67.9 L), positive
  lift on all four surfaces, 0 violations, 100% audit coverage, 19,515-row chain intact, 111 duplicate webhooks
  with 0 side effects, 1 HDFC/UPI incident holding 87 cases with 12 systemic diagnoses, 354 agent runs degraded
  to rules in the LLM outage window. The earlier 300-case / 10% holdout run (seed 7) showed a negative
  rupee-weighted lift because three large receivables fell into the 31-case holdout while case-rate lift stayed
  positive; recorded as D-038, the scorecard now names case-rate lift as primary and reports per-surface lift.
  `make replay` defaults to the 1,000-case run. README rewritten around the numbers. PM-008 (suite red after
  19:00 IST because the RBI window closed a test's voice call) fixed by threading `reference_time` into the
  voice service. 399 tests, lint clean, frontend build clean.

- 2026-09-05: **OpenAI provider.** Owner has an OpenAI key, not Anthropic. Added `app/agent/providers/openai_provider.py`
  (GPT-4o via the official OpenAI SDK, function calling, `tool_choice="required"`, tool-role replay, refusal and
  content-filter mapped to the breaker), moved the system prompt and `submit_plan` schema to
  `providers/prompt.py` so both LLM providers share them, `LLM_PROVIDER=auto` now resolves Claude -> GPT -> rules,
  new settings `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_MODEL_PLANNER_OPENAI=gpt-4o`, readiness reports the
  effective provider and key presence, cost table has OpenAI prices, replay `--provider openai`, UI option,
  compose env, `scripts/smoke_llm_planner.py` to verify a key with one dry-run plan. 7 new tests (D-039).

## 7. Definition of done (final status)

- [x] Measured money recovered across a batch: holdout experiment, ledger, settlement recon, scorecard with
      lift / CI / p-value, replay report in `reports/`.
- [x] Compliant escalation: RBI window, TRAI DND, consent ledger, frequency caps, surface profiles, handoff,
      approval queue, mandate pre-debit notice.
- [x] Stopping rules: capture, PTP, opt-out, handoff, degradation hold, touch caps; post-recovery outreach is
      counted as a violation and is 0 in the replay.
- [x] Audit trail: hash-chained audit log with verification; 100% coverage of executed actions.
- [x] Agent: Claude planner with bounded tools, deterministic validators, fallback planner, traces.
- [x] All four surfaces + degradation diagnosis.
- [x] Production scaffolding: outbox + worker, retries + breakers, JSON logs, metrics, OTel hook, compose, CI.
- [x] Command Center v2.
- [x] Docs, postmortems (7), decision log (37), memo.
- [ ] Owner actions before publishing: flatten the zip folder layout, remove `__MACOSX`, `git init`, commit in
      the phase order of this file, record the 5-minute video with `docs/pitch-script.md`.
