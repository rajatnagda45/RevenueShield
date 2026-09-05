# The Recovery Agent

## What it is

One evaluation of one case: read the dossier, decide the single next move, hand it to deterministic code.
The planner is an LLM behind a small provider interface: Claude (`claude-opus-5` through the official
Anthropic SDK) or GPT (`gpt-4o` through the official OpenAI SDK, Chat Completions with function calling),
plus a deterministic "Null" planner that emits the identical plan schema from the rule/ML recommendation.
Both LLM providers share one system prompt and one `submit_plan` schema (`app/agent/providers/prompt.py`).
`LLM_PROVIDER=auto` picks Claude if `ANTHROPIC_API_KEY` is set, else GPT-4o if `OPENAI_API_KEY` is set, else
rules. The runner, gating, validation, approvals and traces are the same for all three, so the merchant can
run the system with no LLM key and switch one on later without a code change.

Provider differences that matter: the OpenAI provider uses `tool_choice="required"` so GPT-4o cannot end a
turn in prose (which would end the run with no plan and fall back to rules); tool results go back as `tool`
role messages keyed by `tool_call_id`; a `refusal` or `content_filter` finish is treated like an outage and
opens the same circuit breaker. Cost accounting uses OpenAI list prices for `gpt-4o*` and `gpt-4.1*`.

Check a key before a demo: `python scripts/smoke_llm_planner.py` runs one dry-run agent evaluation on an open
case and exits non-zero if the planner was unavailable or degraded.

## Dossier

`DossierBuilder.build` assembles what the planner may know:

- case: surface and its bounded profile, outstanding amount from the ledger, age, arm, touches used,
  previous steps, degradation hold, human handoff, active promise-to-pay, active payment link;
- customer: first name, segment, language, timezone, masked contact, consent per channel, last 30 days of
  touches, historical payment features;
- diagnosis and the rule/ML recommendation with expected value;
- constraints: the negotiation envelope, approval thresholds, and a **precomputed verdict for every agent
  action** (allowed or blocked, with the rule) so the model is told what it may not do before it decides.

The dossier hash is stored on the run so a trace can be reproduced.

## Tools (all thin wrappers over existing services)

| Tool | Mutating | Gate |
|---|---|---|
| `get_case_dossier`, `get_customer_history`, `check_policy`, `draft_message_template` | no | — |
| `send_whatsapp`, `send_email`, `send_payment_link` | yes | holdout/handoff, PolicyEngine, ContactPolicy, channel service |
| `offer_payment_plan` | yes | NegotiationEnvelope + the above; creates a Razorpay partial-payment link |
| `start_voice_call` | yes | + RBI window, 1 call / 3 days; approval above Rs 50,000 |
| `schedule_retry`, `wait` | yes | plan timing only |
| `record_promise_to_pay` | yes | PromiseToPayService validation |
| `handoff_to_human` | yes | always allowed |
| `escalate_to_merchant`, `close_case` | yes | always / above Rs 1,000 require approval |

A blocked call returns to the planner as an error result with the rule name; nothing is raised, nothing
executes.

## Plan and validation

The planner ends by calling `submit_plan` with a strict `AgentPlan` schema (action, rationale, confidence,
optional message / payment plan / promise / wait hours / handoff reason / approval flag). Then:

1. `PlanValidator` checks structure and rationale length.
2. `MessageValidator` checks any customer text: amount must equal the outstanding amount or an approved
   instalment, no internal vocabulary (error codes, scores, experiments), no intimidation (RBI FPC), no
   impossible promises, length limits, opt-out footer on WhatsApp/SMS, `{payment_link}` placeholder kept.
   A failing message is replaced by the approved template; the trace records why.
3. `NegotiationEnvelope` checks instalment offers (max 3, first >= 30%, extension <= 14 days, waiver 0%).
4. `PlanExecutor.needs_approval` decides whether a human must approve; otherwise the action executes through
   the same toolbox.

## Approvals

`Approval` rows carry the full plan. Operators approve or reject from the Command Center (or API). Approval
re-executes the plan through the gated toolbox at decision time, so a case that recovered or opted out while
waiting is blocked, not actioned. Approvals expire after `APPROVAL_SLA_HOURS`.

## Failure behaviour

Any transport error, 429/5xx, timeout or refusal opens a circuit breaker for `LLM_CIRCUIT_BREAKER_SECONDS`
and the run continues with the Null planner; `degraded_to_rules=true` is written on the run and counted on
the scorecard. If the planner never submits a plan within `LLM_MAX_TURNS`, a fallback plan is produced the
same way.

## Cost

Each run records input/output tokens and a list-price cost estimate. With Claude, prompt caching is applied to
the system prompt and the tool list; with GPT-4o, OpenAI's automatic prompt caching applies to the identical
prefix and the cached count is recorded in the trace. The dossier is the only volatile part of the request.

## Evaluating the agent

The replay harness runs the same batch with the agent on or off (`--agent`) and with either planner
(`--provider anthropic|openai|null`), so the scorecard becomes an A/B of planners on identical scenarios. The
`message_rejected_fallback_to_template` trace events are the signal for prompt tuning.
