# MEMO — what we built, what we cut, what more time would buy

## What we built (v2, on top of the v1 recovery platform)

- A **measurement layer** that makes "money recovered" a defensible number: hash-based holdout per case, an
  append-only ledger keyed by gateway references, settlement reconciliation, and a batch scorecard with lift,
  p-value, bootstrap CI, cost per recovered rupee, compliance violations and audit coverage.
- **All four leak surfaces** the track names: payment failures, subscription mandates (with RBI-compliant retry
  sequencing and re-authorisation on halt), checkout abandonment (immediate and swept), and overdue B2B
  receivables (import, ageing, dunning ladder, escalation with approval).
- **Payment degradation diagnosis**: a bank x method failure matrix against a trailing baseline, incidents
  with a state machine, cases held without consuming touches, systemic diagnosis, jittered release.
- **Compliance v2**: consent ledger with opt-out keywords and voice intents, TRAI DND, RBI calling window,
  holidays, cross-channel frequency caps, human handoff, and a hash-chained audit log with verification.
- **The Recovery Agent**: LLM planner (GPT-4o or Claude behind one provider interface and one prompt) over a
  dossier with precomputed verdicts, 15 policy-gated tools,
  deterministic validators, negotiation envelope, approval queue, traces with cost, and a deterministic
  fallback planner behind a circuit breaker.
- **A replay harness** that drives the real pipeline on a virtual clock with a parametric customer model and
  chaos (duplicate webhooks, LLM outage), producing the scorecard and a Markdown report.
- **Production ops**: transactional outbox and worker, retries and circuit breakers, JSON logs with request
  ids and PII masking, Prometheus metrics, optional OpenTelemetry, compose with a worker, Makefile, lint.
- **Command Center v2** in Next.js with a validated palette in both themes.
- **Documentation**: architecture, compliance mapping, agent design, measurement method, runbook, pitch,
  a phased plan, a decision log with alternatives, and eight postmortems.

## What we cut, deliberately

- Razorpay's official MCP server as a tool transport: kept as a documented, feature-flag-shaped option
  (D-005) rather than a dependency on a Go binary; the REST client already exists and is tested.
- Per-customer randomisation: per-case keeps n high for the demo; the caveat is printed on the scorecard.
- LLM-driven voice conversations: the voice agent stays deterministic (regex intents + state machine) because
  a phone call is the highest-risk channel and the deterministic version is fully tested; the LLM decides
  *whether* to call, not what to say.
- A dedicated settlement webhook flow: `settlement.processed` is audited, but reconciliation uses the recon
  API/job because the webhook entity carries no payment ids.
- Deleting the zip-extraction artefacts (`__MACOSX`, nested folder): destructive, left for the owner.

## What more time would address

1. Per-customer randomisation and sequential testing (always-valid p-values) for continuous experiments.
2. Learn the behaviour-model parameters from real outcomes and feed them back into the ML action model.
3. Real DLT/DND scrubbing provider behind the `DndRegistry` interface.
4. Multi-tenant merchants (tenant id on every table; Razorpay Partner/OAuth onboarding).
5. Legal-notice drafting for receivables as an approval-only tool with a merchant-supplied template.
6. Voice: LLM-drafted call scripts validated the same way as messages.
7. Playwright smoke tests for the Command Center; a Grafana dashboard JSON for the exposed metrics.

## One decision worth defending

**The holdout arm (D-002, D-009).** A reasonable alternative is attribution windows: cheaper, no customer is
"left untreated", and every other dunning tool does it. We chose to deliberately not contact one in ten cases.
The cost is real - about a tenth of recoverable revenue is measured rather than pursued - but it is the only
way to answer the track's actual question ("show measured money recovered") with a number a CFO would sign.
It also forced the strongest engineering property in the system: every code path that can touch a customer
had to be enumerated and guarded, and the scorecard now counts any leak as a violation. Attribution windows
would have produced a bigger number and a weaker system.
