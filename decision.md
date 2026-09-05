# Decision Log — RevenueShield v2

Format: `D-NNN | date | decision` followed by alternatives and rationale. Newest at the bottom.

---

**D-001 | 2026-09-05 | Extend the existing FastAPI/SQLAlchemy codebase instead of rewriting.**
Alternatives: rewrite in TypeScript/Mastra; start a fresh Python service.
Rationale: 21k lines and 316 passing tests already implement diagnosis, policy, execution,
outcomes and ML correctly. Judges reward build quality and reliability; a rewrite risks both.

**D-002 | 2026-09-05 | Measure recovery with a deterministic hash-based holdout per case (default 10%).**
Alternatives: attribution windows only (v1); per-customer randomisation; no control group.
Rationale: the Bar says "measured money recovered across a batch". Only a control group turns
"captured after we called" into "incremental rupees". Hashing (case id + salt) is reproducible,
auditable, and needs no external randomness. Holdout is per case, not per customer, to keep
n high in a demo batch; the tradeoff (customer-level spillover) is documented in the scorecard.

**D-003 | 2026-09-05 | "Money recovered" is reported at two levels: captured and settled.**
Alternatives: captured only.
Rationale: captured money can be refunded or fail settlement; merchants and judges trust settled.
Settlement reconciliation consumes `settlement.processed` webhooks and the settlements recon API.

**D-004 | 2026-09-05 | LLM provider abstraction: Anthropic Claude default, deterministic NullLLM fallback.**
Alternatives: Gemini free tier; no LLM at all.
Rationale: the "AI Judgment" criterion needs a real agent; the deterministic fallback keeps tests
offline, the demo key-free, and gives graceful degradation under rate limits or outages.
Provider is swappable through one interface so Gemini/OpenAI adapters are additive.

**D-005 | 2026-09-05 | The agent never calls Razorpay/Twilio directly.**
Tools are thin typed wrappers over existing services; every mutating tool call passes
PolicyEngine + ExecutionGuard first.
Alternatives: hand the model the Razorpay MCP server tools directly.
Rationale: bounded autonomy is the whole point of the track; deterministic validators must own
correctness. Razorpay's official MCP server is kept as an optional transport adapter for the
Payment Link and fetch tools (feature flag), so the pitch can mention native MCP support without
making the core path depend on a Go binary.

**D-006 | 2026-09-05 | Recovery-call window tightened to 08:00-19:00 customer-local; WhatsApp/SMS quiet hours 21:00-08:00.**
Alternatives: keep v1's 20:00-08:00 for everything.
Rationale: RBI Fair Practices Code for recovery agents limits calls to 08:00-19:00. Matching the
regulation the merchant is actually bound by is a visible Problem Taste signal. Configurable.

**D-007 | 2026-09-05 | Job scheduling via a Postgres transactional outbox + SKIP LOCKED worker; no Redis/Celery.**
Alternatives: Celery+Redis; APScheduler in-process.
Rationale: one fewer service for judges to run, the job row commits in the same transaction as
the business write, and it survives worker restarts.

**D-008 | 2026-09-05 | Frontend v2 is a separate Next.js app in `frontend-next/`; the vanilla portal stays until parity.**
Alternatives: keep extending vanilla JS.
Rationale: agent traces, approval queue and scorecard need real components/routing; a parallel
app avoids breaking the existing `/portal` demo during the build.

**D-009 | 2026-09-05 | Holdout cases still get a RecoveryPlan whose only step is OBSERVE.**
Alternatives: create no plan for holdout cases.
Rationale: identical data path for both arms means the scorecard compares like with like and
the audit trail shows the deliberate non-intervention. Stopping rules on capture still apply.

**D-010 | 2026-09-05 | Failure-recovery narrative captured in POSTMORTEMS.md as incidents happen.**
Alternatives: write it retrospectively at the end.
Rationale: the rubric asks for what broke and how it was fixed; contemporaneous notes with the
failing test or log excerpt are more credible than a retrospective.

**D-011 | 2026-09-05 | Nothing deleted or moved in the owner's folder layout.**
`__MACOSX/` and the nested `revenue-resilience/revenue-resilience/` are zip-extraction artefacts.
Rationale: destructive; flagged in newplan.md Phase 0 for the owner to do before publishing.

**D-012 | 2026-09-05 | Windows venv at `backend/venv` (Python 3.10) instead of replacing `backend/.venv`.**
Rationale: the checked-in `.venv` is a macOS venv (darwin `.so` files) and cannot run here;
deleting it would destroy the owner's Mac environment. Both directories are gitignored.

**D-013 | 2026-09-05 | One `CaseFactory` opens every case, regardless of surface.**
Alternatives: keep the payment-failure inline sequence and duplicate it per detector.
Rationale: the bootstrap (audit, arm assignment, AT_RISK ledger, diagnosis, recommendation,
learning snapshot, bounded plan) is the invariant the judges will inspect; one code path means
one place to get it right and one place to test.

**D-014 | 2026-09-05 | Checkout abandonment is detected two ways: immediately from a user-cancelled
`payment.failed` with an order id, and by a sweep over quiet orders.**
Alternatives: sweep only; rely on Razorpay `order.paid` absence only.
Rationale: Razorpay emits no "order abandoned" webhook. The immediate path gives a fast nudge for
the clearest signal; the sweep catches silent drop-offs registered via `order.*` webhooks or the
merchant API. Both share one detector so the case shape is identical.

**D-015 | 2026-09-05 | Mandate retries are scheduled by our sequencer but executed by the gateway.**
Alternatives: call Razorpay to force a charge; do nothing until `subscription.halted`.
Rationale: Razorpay retries pending subscriptions itself and RBI requires a 24-hour pre-debit
notice. Our value is compliant timing (notice deadline, salary-day alignment, degradation hold,
attempt cap) and switching the playbook to re-authorisation on `halted`, when retries cannot work.

**D-016 | 2026-09-05 | Degradation detection uses a pooled z-test with an absolute-delta floor and
a 5% baseline floor, cell baseline when n>=50 else the global baseline.**
Alternatives: fixed failure-rate threshold; EWMA; per-cell only.
Rationale: a pure threshold alarms on small cells and misses gradual outages; the floors stop
zero-variance alarms when a bank has a near-perfect week. Every parameter is a setting so it can be
tuned from the health matrix. State machine (SUSPECTED -> CONFIRMED -> RECOVERING -> CLOSED) avoids
flapping and lets the merchant see one incident, not one alert per window.

**D-017 | 2026-09-05 | Held cases do not consume a plan step and are released with jitter.**
Rationale: an outage is not the customer's fault; it must not burn one of their three touches.
Jittered release avoids a retry storm on the recovering issuer.

**D-018 | 2026-09-05 | The cross-channel ContactPolicy runs *after* each channel's own checks.**
Alternatives: run it first.
Rationale: channel rules (cooldown, per-channel caps) keep their names and semantics for existing
consumers and tests; the cross-channel policy adds the regulator's view (consent, DND, RBI window,
holidays, total touches, min gap) on top. Both are enforced; the stricter one always wins.

**D-019 | 2026-09-05 | Consent is an append-only ledger with the latest record authoritative, and it
mirrors onto the legacy customer flags.**
Rationale: auditors ask "when and how did this customer opt out"; a boolean cannot answer that. The
mirror keeps v1 code paths consistent without rewriting them.

**D-020 | 2026-09-05 | Audit rows are hash-chained globally with a unique sequence, via a
`before_flush` hook.**
Alternatives: per-case chains; external append-only log; no chain.
Rationale: a hook means no service can forget it; a global chain proves ordering across cases; the
unique sequence turns a concurrent-writer race into a commit failure instead of a silent fork.
Verification recomputes the chain and reports the first break. Limitation recorded: high write
concurrency would serialise on the sequence; acceptable at merchant scale, revisit with a
partitioned chain if it ever matters.

**D-021 | 2026-09-05 | The planner ends every run by calling a `submit_plan` tool with a strict schema,
rather than free text or `output_config.format`.**
Alternatives: parse JSON from text; structured outputs on the final message.
Rationale: one interface serves both tool calls and the final decision, the deterministic Null
planner emits the identical schema, and Claude Fable/Opus 5 do not allow forced tool choice, so
the prompt instructs the model to finish with `submit_plan` and the runner falls back to rules if
it never does. Validation happens after parsing, in code.

**D-022 | 2026-09-05 | Policy verdicts for every agent action are precomputed into the dossier.**
Alternatives: let the model discover blocks by trying tools.
Rationale: fewer wasted tokens and tool calls, and a more auditable trace: the model is told what
it may not do before it decides. Gating still runs at execution time in case state changed.

**D-023 | 2026-09-05 | An LLM-written message that fails validation is replaced by the approved
template, not regenerated in a loop.**
Alternatives: retry with feedback up to N times.
Rationale: bounded cost and latency, and the customer always receives a compliant message. The
trace records `message_rejected_fallback_to_template` with the exact rule violated, which is the
evaluation signal for prompt tuning.

**D-024 | 2026-09-05 | Approvals execute the deferred plan through the same gated toolbox the
agent uses, at approval time, with fresh gating.**
Rationale: the operator approves an intent, not a stale execution; if the case recovered or the
customer opted out while the approval sat in the queue, the action is blocked and the queue shows why.

**D-025 | 2026-09-05 | `AGENT_DRIVES_PLANS` is off by default; the scheduler keeps the v1 NBA path
unless the merchant opts in.**
Rationale: 316 legacy tests pin the NBA behaviour; the replay harness and the demo enable the agent
explicitly, and the scorecard can compare both planners on the same batch.

**D-026 | 2026-09-05 | The replay drives the real pipeline, not a model of it.**
Alternatives: a standalone Monte-Carlo simulation of recovery outcomes.
Rationale: the scorecard must be produced by the same adapter, event processor, scheduler, agent,
policy engine, ledger and audit chain that run in production, otherwise the number is a story
about the simulator. Only the customer's decision to pay is synthetic, and it is expressed as a
real `payment.captured` / `subscription.charged` / `invoice.paid` / `order.paid` webhook.

**D-027 | 2026-09-05 | Replayed events carry `notes.replay=true`, and only then is case creation
backdated to the event time.**
Rationale: virtual-clock replays need case ages, windows and ledger timestamps to follow the
scenario timeline; production webhooks (which may carry old `created_at` values) must keep using
the database clock so v1 behaviour and tests are unchanged.

**D-028 | 2026-09-05 | The behaviour model separates organic payment from intervention uplift and
adds fatigue and opt-out.**
Rationale: the whole point of the holdout is to measure incremental recovery; a model in which
customers only pay when contacted would make the system look better than any real merchant would
see. Parameters live in one file so a merchant can substitute measured rates.

**D-029 | 2026-09-05 | A failing plan aborts the replay with the plan id instead of rolling back
and continuing.**
Rationale: during Phase 6 a silent rollback produced a clean-looking scorecard on top of a real
defect (duplicate step numbers). A harness that hides defects is worse than no harness.

**D-030 | 2026-09-05 | Recurring work is scheduled by idempotency-keyed ticks, not per-plan timers.**
Alternatives: enqueue one job per plan at its `next_evaluation_at`; APScheduler in-process.
Rationale: a 5-minute `plans.process_due` tick is trivially idempotent across workers (key =
kind + interval bucket), survives restarts, and needs no bookkeeping when plans pause, resume or
are released early by the degradation monitor. One-off jobs are still used where latency matters
(first evaluation of a new case is committed with the case).

**D-031 | 2026-09-05 | Metrics are a facade with a no-op fallback; OpenTelemetry is optional.**
Rationale: the base install stays small and the code never fails on a missing observability
dependency; `pip install -r requirements-observability.txt` + `OTEL_EXPORTER_OTLP_ENDPOINT`
turns on traces without a code change.

**D-032 | 2026-09-05 | Lint is limited to correctness rules.**
Alternatives: a full style ruleset.
Rationale: F821/F811/E9/F401 found four real production bugs (PM-003, PM-007) with zero noise;
style rules on a 30k-line codebase would have buried them.

**D-033 | 2026-09-05 | The worker re-applies the claim after a handler rollback.**
Rationale: when a handler shares the worker's transaction, a rollback also undoes the claim
(attempt counter, lock). Losing the attempt count would make a poison job retry forever; the
worker restores the claim before recording the failure so backoff and DEAD still apply.

**D-034 | 2026-09-05 | Command Center v2 is a thin client over the public API with no server-side state.**
Alternatives: Next.js route handlers proxying the API; server components with a DB connection.
Rationale: every number on screen is fetched from an endpoint a judge can `curl`; the same
endpoints back the replay report and the tests, so there is one source of truth for the scorecard.

**D-035 | 2026-09-05 | No charting library; stat tiles, thin meters and tables built from the validated palette.**
Alternatives: Recharts / Chart.js.
Rationale: the dashboard's job is a handful of headline numbers and comparisons (treatment vs
holdout, matrix cells). Tiles, two-series meters with a legend, and a single-hue sequential matrix
cover it with zero dependencies, pass the palette validator in both themes, and keep every metric
in a table for accessibility.

**D-036 | 2026-09-05 | The replay runs the degradation monitor on a 15-minute sub-grid, not only at plan ticks.**
Rationale: the first demo run never observed the injected 15-minute issuer outage because the
monitor sampled every 12 hours; failures inside the burst were diagnosed as customer problems.
Ingestion and monitoring now advance in monitor-window slices, so the outage is detected while it
happens and later failures in that cell are tagged systemic - the behaviour production would show
with the worker's 5-minute tick.

**D-037 | 2026-09-05 | The replay's monitor sub-grid is one third of the detection window (about 5 minutes).**
Rationale: with 15-minute slices the whole injected outage landed in one slice before the monitor
ran, so the incident opened but no failure inside it could be tagged systemic. Five-minute slices
match the worker's production tick and let the second half of a burst be diagnosed correctly, which
is the behaviour the demo needs to show.

**D-038 | 2026-09-05 | Case-rate lift is the primary metric; the headline batch is 1,000 cases with a 15% holdout.**
Alternatives: rupee-weighted lift as the headline; keep the 300-case, 10% holdout demo.
Rationale: the 300-case run put three large receivables into a 31-case holdout and the rupee-weighted lift
went negative while the case-rate lift stayed positive. Rupee lift is dominated by a handful of invoices in
either arm at this sample size. The scorecard now states the primary metric explicitly, reports rupee lift
with its bootstrap CI, and adds per-surface treatment-vs-holdout lift so a merchant can see the effect on
each leak type. `make replay` defaults to 1,000 cases and a 15% holdout (about four minutes), which gives
a p-value below 1e-6 and a positive lift on every surface. The small run is kept in `reports/` as an honest
example of the noise the metric choice protects against.

**D-039 | 2026-09-05 | Add an OpenAI (GPT-4o) planner provider behind the same interface; `auto` prefers whichever key exists.**
Alternatives: keep Anthropic-only; swap to OpenAI-only; route through a third-party abstraction layer.
Rationale: the owner has an OpenAI key, not an Anthropic one, so the demo would otherwise run on the rules
planner. The provider seam already existed (`LLMProvider.plan(dossier, tools, transcript) -> ProviderTurn`),
so a second provider is about 150 lines: Chat Completions with function calling, `tool_choice="required"` so
GPT-4o always ends in a tool call, tool results replayed as `tool` messages, refusals and content filters
mapped to `ProviderUnavailable` so the same breaker and rules fallback apply. The prompt and `submit_plan`
schema moved to a shared module so both providers see identical instructions and a replay with
`--provider openai` vs `--provider anthropic` is a fair A/B. No abstraction library: two official SDKs are
easier to reason about than one wrapper, and the runner never depends on provider-specific shapes. `auto`
resolves Claude first, then OpenAI, then rules; explicit `LLM_PROVIDER=openai` overrides. Cost table gained
OpenAI list prices (longest key first so `gpt-4o-mini` is not priced as `gpt-4o`).
