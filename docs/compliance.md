# Compliance: rule to code

Recovery outreach in India sits under several regimes. This table maps each rule RevenueShield enforces to
the code that enforces it and the test that proves it. Every block is written to `contact_attempts` with the
rule name and surfaces on the scorecard; every decision is in the hash-chained audit log.

| Regime / expectation | Rule in RevenueShield | Code | Test |
|---|---|---|---|
| RBI Fair Practices Code for recovery agents: contact only between 08:00 and 19:00 | Voice window 08:00-19:00 customer-local (`CONTACT_VOICE_*_HOUR`); PolicyEngine mirrors it | `compliance/contact_policy.py`, `decision/policy.py` RULE 3 | `test_compliance.py::test_contact_windows_follow_customer_timezone`, `test_policy_engine.py` |
| RBI FPC: no intimidation, harassment, misrepresentation | `MessageValidator` rejects threatening vocabulary and promises; agent falls back to the approved template | `agent/validators.py` | `test_agent.py::test_message_validator_rules`, `test_invalid_llm_message_falls_back_to_template` |
| RBI FPC: identify yourself, respect requests to stop | Opt-out keywords (STOP, "do not call", Hinglish variants) and voice intents write the consent ledger and stop outreach immediately | `compliance/consent.py`, voice conversation manager, inbound WhatsApp webhook | `test_inbound_stop_via_api_pauses_plan`, `test_whatsapp_inbound_webhook_honours_stop`, `test_voice_dispute_and_wrong_number_intents_trigger_handoff` |
| TRAI: Do Not Disturb registry | Pluggable `DndRegistry`; voice and SMS blocked for registered numbers; WhatsApp transactional permitted | `compliance/dnd.py` | `test_trai_dnd_registry_blocks_voice_not_whatsapp` |
| Reasonable contact frequency | 3 touches per rolling 7 days across all channels, 1 voice call per 3 days, 20h minimum gap; per-surface touch caps (checkout 2, receivables 6) | `contact_policy.py`, `domain/surfaces.py`, PolicyEngine RULE 0b | `test_cross_channel_frequency_caps`, `test_surfaces.py::test_policy_enforces_surface_profiles` |
| No contact on national holidays (voice) | `CONTACT_HOLIDAYS` list, customer-local date | `contact_policy.py` | `test_no_voice_on_national_holiday` |
| RBI recurring-payment framework: pre-debit notification >= 24h, limited retries | Mandate retry sequencer schedules `notify_by = scheduled_at - 24h`, never debits without notice, caps at 3 attempts, salary-day alignment, holds during issuer degradation | `detectors/mandates.py` | `test_surfaces.py::test_mandate_retry_sequencer_*`, `test_subscription_pending_then_charged`, `test_mandate_retry_cap` |
| Halted mandate cannot be retried | `subscription.halted` cancels retries and switches the playbook to re-authorisation | `detectors/mandates.py` | `test_subscription_halted_switches_to_reauthorisation` |
| Promise-to-pay must pause dunning | Active PTP blocks all outreach in PolicyEngine, ExecutionGuard and channel services | `decision/policy.py` RULE 4 | v1 suites + `test_agent_tool_loop_with_scripted_calls_and_promise` |
| Stop the moment the customer pays | `OutcomeEngine` cancels plans, interventions, queued messages; scorecard counts any post-recovery outreach as a violation | `outcomes/engine.py`, `analytics/scorecard.py` | `test_scorecard_flags_outreach_on_holdout_as_violation`, replay `policy_violations == 0` |
| Disputes and identity doubt go to a human | `HandoffService.freeze` on dispute / human request / wrong number; every channel blocked until an operator releases | `compliance/handoff.py` | `test_handoff_freezes_and_release_resumes` |
| Customer data minimisation in logs | PII masking filter on every log line; masked contacts in dossiers and APIs | `observability/logging.py` | `test_observability.py::test_mask_pii` |
| Auditability | Hash-chained audit log, `GET /audit/verify` | `audit/chain.py` | `test_audit_chain_verifies_and_detects_tampering` |
| Autonomy limits | Approvals for high-value calls, merchant escalation, waivers, closures; SLA expiry | `agent/approvals.py` | `test_high_value_voice_requires_approval_then_executes_on_approve`, `test_approval_expiry` |

Configuration lives in `backend/app/core/config.py` (section "Compliance v2"); every threshold is an environment
variable so a merchant's legal team can tighten it without a deploy.
