# Policy & Compliance Engine Architecture

This document describes the regulatory rules, customer quiet hours, attempt caps, active intervention guards, and stopping rules implemented in **PolicyEngine** (`policy_engine_v1`).

---

## 1. Role of the Policy Engine

The Policy Engine sits between the **Recovery Decision Engine** and the **Execution Layer**. While the Decision Engine suggests what action has the highest probability of recovering revenue, the Policy Engine strictly evaluates:
> *"Is this action allowed right now under our regulatory, customer protection, and safety constraints?"*

The Policy Engine is **independent** of the decision scoring logic and operates deterministically.

---

## 2. Hard Compliance & Safety Rules

| Rule Identifier | Policy Description | Behavior when Violated |
| :--- | :--- | :--- |
| `CASE_ALREADY_RECOVERED_OR_CLOSED` | Never contact a customer or attempt payment if the case is already `RECOVERED`, `CLOSED`, or `RESOLVED`. | Action is `BLOCKED`. |
| `MAX_RETRIES_EXCEEDED` | Maximum payment gateway retry attempts are strictly capped at 3 attempts. | `RETRY_PAYMENT` is `BLOCKED`. |
| `QUIET_HOURS_VOICE_PROHIBITED` | Voice calls are prohibited during night/early morning hours (20:00 to 08:00 local time). | `VOICE_OUTREACH` is `BLOCKED`. |
| `PROMISE_TO_PAY_ACTIVE` | If customer has an active Promise-to-Pay agreement, pause routine outreach and retries. | Automated outreach is `BLOCKED`. |
| `ACTIVE_INTERVENTION_EXISTS` | Prevents multiple conflicting outreach interventions from executing in parallel. | Parallel action is `BLOCKED`. |

---

## 3. Stopping Rule (Payment Captured Lifecycle)

When a `payment.captured` webhook is ingested:
1. The corresponding `RecoveryCase` transitions to `RECOVERED`.
2. Any existing `RecoveryAction` records in status `RECOMMENDED`, `APPROVED`, or `PLANNED` are immediately marked `CANCELLED`.
3. An `AuditLog` entry with action `RECOVERY_ACTION_CANCELLED` is recorded.
4. No further outreach or automated payment retries can be dispatched for this case.

---

## 4. Structured Policy Result Schema

The Policy Engine returns a structured response embedded in the database and API:
```json
{
  "allowed": false,
  "reason": "Voice outreach is prohibited during quiet hours (20:00 - 08:00). Current evaluation hour: 21:00.",
  "blocking_rule": "QUIET_HOURS_VOICE_PROHIBITED",
  "evaluated_at": "2026-08-21T19:30:00.000000+00:00"
}
```

If all rules pass:
```json
{
  "allowed": true,
  "reason": "Action satisfies all current recovery policies and compliance constraints.",
  "blocking_rule": null,
  "evaluated_at": "2026-08-21T19:30:00.000000+00:00"
}
```
