# POSTMORTEMS — what broke while building RevenueShield v2, and how it was fixed

> Written contemporaneously (judging rubric: "Failure Recovery"). Each entry has the symptom, the
> evidence, the root cause, the fix, and what now prevents a repeat.

---

## PM-001 — Introducing a holdout arm made 1-in-10 legacy tests flaky (2026-09-05)

**Symptom.** After wiring the experiment assigner into webhook ingestion, the full suite went
334 passed / 1 failed, but the failing test changed between runs
(`test_decision_integration.py::test_get_recommendation_api_endpoint` failed, then passed alone).

**Evidence.**
```
assert data["status"] == "APPROVED"
E   AssertionError: assert 'BLOCKED' == 'APPROVED'
```
and, in an earlier run, an audit-ordering assertion:
```
E   AssertionError: assert 'EXPERIMENT_ARM_ASSIGNED' == 'RECOVERY_CASE_OPENED'
```

**Root cause.** Arm assignment is a hash of the case id, and the legacy tests create cases with
random ids. Roughly 10% of runs put the case in HOLDOUT, where the PolicyEngine correctly blocks
every intervention with `HOLDOUT_ARM_OBSERVE_ONLY`. The system behaved as designed; the tests
encoded the v1 assumption that every case is treated. Separately, the assignment audit row was
written before the "case opened" audit row, which a test relied on being first.

**Fix.**
1. `tests/conftest.py` sets `EXPERIMENTS_ENABLED = False` for the session; measurement tests
   opt in with a fixture. Production default stays on (10% holdout).
2. Assignment moved after the `RECOVERY_CASE_OPENED` audit row so the trail reads in causal order.

**Prevention.** The scorecard test seeds arms explicitly (`experiment_arm="HOLDOUT"`) instead of
relying on the hash, and the assigner has its own distribution test (6,000 ids, 8-12% tolerance),
so randomness is tested where it belongs and nowhere else.

**Lesson worth telling judges.** A control group is a feature that deliberately makes the system
*do nothing* for some customers. Every code path that can touch a customer needed an explicit
guard (policy engine, execution guard, scheduler, WhatsApp, voice, email, payment-link
intervention), and the scorecard now counts any leak as a policy violation, so the invariant is
measured, not assumed.

---

## PM-002 — Twilio webhooks could never have parsed a form body in production (2026-09-05)

**Symptom.** The first test that posted a real `application/x-www-form-urlencoded` body to the
inbound WhatsApp webhook (a customer replying `STOP`) failed inside Starlette:
```
AssertionError: The `python-multipart` library must be installed to use form parsing.
```

**Root cause.** Every Twilio callback in v1 (voice `<Gather>` results, call status, inbound
WhatsApp) reads `await request.form()`, but `python-multipart` was not in `requirements.txt`. The
316 v1 tests never exercised form parsing through the HTTP layer: they called the services directly
or posted JSON. The code was correct; the deployable artefact was not.

**Fix.** `python-multipart>=0.0.9` added to `requirements.txt`; the compliance suite now posts a
real form body to `/webhooks/whatsapp/inbound` and asserts the opt-out is honoured.

**Prevention.** Webhook contract tests must go through the FastAPI TestClient with the same
content type the provider sends. This is now the pattern for every new inbound endpoint.

**Lesson worth telling judges.** "Tests pass" is not "it deploys". The compliance work (a customer
replying STOP must stop everything, immediately) forced the first end-to-end test of a provider
callback, and that is what found the missing dependency.

---

## PM-003 — The v1 inbound WhatsApp endpoint raised NameError on every request (2026-09-05)

**Symptom.** After PM-002 was fixed, the same test failed one line later:
```
app/api/communications.py:283: NameError: name 'Customer' is not defined
```

**Root cause.** `receive_whatsapp_inbound_message` queried `Customer` without importing it. Because
no test had ever reached the handler (see PM-002), a Twilio inbound message in production would
have returned HTTP 500 and the customer's reply - including `STOP` - would have been dropped.

**Fix.** Import added; the endpoint is now covered by a contract test that posts a real Twilio form
body and asserts the unsubscribe reply and the consent-ledger write.

**Prevention.** Every router module is imported at app start, so an unused-name check catches this
class of bug statically. `ruff`/`pyflakes` is added to the dev workflow in Phase 7.

**Lesson worth telling judges.** An opt-out that is silently dropped is the worst compliance
failure a recovery system can have. It was found only because compliance became a first-class,
tested feature rather than a checkbox.

---

## PM-004 — Circular import only reachable from the replay CLI (2026-09-05)

**Symptom.** The full test suite was green, but the first CLI run of the replay harness died at import:
```
ImportError: cannot import name 'ReceivablesDetector' from partially initialized module
'app.detectors.receivables' (most likely due to a circular import)
```

**Root cause.** `app/services/__init__.py` imports `EventProcessor`, which imported the detectors at module
level; the detectors import `CustomerService` from the services package. Tests always import
`app.main` first, which happens to resolve the cycle in a working order. The CLI imported
`app.simulation.replay` first and hit it the other way round.

**Fix.** Detector imports inside `EventProcessor.process_normalized_event` and the `CustomerService`
import inside `ReceivablesDetector.import_rows` became function-level imports.

**Prevention.** A smoke test now imports the entry-point modules in the CLI's order
(`app.simulation.replay`, `app.detectors.receivables`, `app.api.surfaces`) and the `make replay`
target runs in CI.

---

## PM-005 — A silent rollback hid a real defect behind a clean scorecard (2026-09-05)

**Symptom.** The first end-to-end replay produced a good-looking scorecard while the log showed
`UNIQUE constraint failed: recovery_plan_steps.recovery_plan_id, recovery_plan_steps.step_number`
for several plans.

**Root cause.** Two defects, one masking the other. (a) In the agent-driven scheduler branch a
WAIT/NO_ACTION step did not advance the plan counter, so the next evaluation reused the same step
number. (b) The harness caught the exception, rolled the session back and carried on - which
discarded the failing case and reported on the survivors.

**Fix.** (a) Step numbers are derived from the plan's existing steps; touches and non-touches are
counted separately. (b) The harness now fails loudly with the plan id (D-029).

**Lesson worth telling judges.** A measurement harness must be allowed to fail. The moment it is
allowed to "recover" from an error it stops measuring the system and starts measuring itself.

---

## PM-006 — Real wall-clock timestamps inside a virtual-clock replay created 18 false compliance violations (2026-09-05)

**Symptom.** The end-to-end replay reported `policy_violations = 18`, all `OUTREACH_AFTER_RECOVERY`.

**Root cause.** The email service stamped `Communication.sent_at` with `datetime.now()` while every
other component used the replay's virtual clock, so each email on a later-recovered case looked as
if it had been sent after recovery. The violation detector was right; the timestamp was wrong.

**Fix.** `reference_time` is threaded through the email service and its callers (scheduler, agent
toolbox); the contact policy and ledger use the same clock.

**Prevention.** Any service that writes a customer-facing timestamp accepts `reference_time`; the
replay test asserts `policy_violations == 0` and prints the rule breakdown when it is not.

---

## PM-007 — Static analysis found three more latent NameErrors in v1 (2026-09-05)

**Symptom.** Adding `ruff` with only correctness rules (`F821`, `F811`, `E9`) to the repository reported:
```
app/api/communications.py: F821 Undefined name `RecoveryMessageGenerator`
app/services/customer_service.py: F821 Undefined name `datetime` / `timezone`
app/services/voice_recovery_service.py: F811 Redefinition of `handle_test_voice_gather_response`
```

**Root cause.** Untested branches: the inbound-WhatsApp reply path (see PM-003), the anonymous-customer
fallback (a webhook with neither email nor phone would have crashed), and a copy-pasted method that
silently shadowed its twin.

**Fix.** Imports added, dead duplicate removed, 172 unused imports pruned; `make lint` runs the same
rules and is part of the definition of done.

---

## PM-008 — The suite went red at 19:00 IST because the RBI calling window closed (2026-09-05)

**Symptom.** A test that had passed all day failed in the final full run:
`test_high_value_voice_requires_approval_then_executes_on_approve` expected the approved voice call to
execute and found it BLOCKED.

**Root cause.** The approval executed the call through the voice service, which read the wall clock.
The suite ran after 19:00 IST; the RBI Fair Practices window (08:00-19:00) had closed, and the
`ContactPolicy` blocked the call - exactly what it should do to a real customer at that hour. The
test, not the system, was wrong: it fixed every other clock except the one inside the voice service.

**Fix.** `VoiceRecoveryService.start_recovery_call` accepts `reference_time`; the agent toolbox passes
its own clock (the replay harness therefore also evaluates calls on virtual time).

**Prevention.** Every service that evaluates a time-based rule takes a `reference_time`; tests pin it.
This is the third clock-related incident (PM-006, PM-008 and the e2e fix in Phase 4) and the rule is
now written down in `ARCHITECTURE.md`.

**Lesson worth telling judges.** The compliance layer blocked a phone call at 7 pm without anyone
asking it to. That is the behaviour you want; you just have to test it on purpose rather than by
running the suite late in the evening.
