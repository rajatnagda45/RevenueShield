# Measurement: how "money recovered" is computed

## The problem with attribution

A customer whose payment failed on Monday and who paid on Wednesday after our reminder may have paid on
Wednesday anyway. Attribution windows ("paid within 72h of our message") count that as a win. A control
group does not.

## Design

1. **Assignment.** When a case opens, `sha256(experiment_key:salt:case_id)` is bucketed into [0, 10000).
   Buckets below `HOLDOUT_PERCENT × 100` are HOLDOUT (default 10%, overridable per surface). The assignment is
   stored with its inputs and audited, so anyone can recompute it.
2. **Holdout behaviour.** Holdout cases get a plan with one `OBSERVE` step and are refused by every code path
   that can touch a customer: PolicyEngine (`HOLDOUT_ARM_OBSERVE_ONLY`), ExecutionGuard, the scheduler, and the
   WhatsApp, voice, email and payment-link services. Stopping rules on capture still apply, so both arms follow
   the same data path.
3. **Ledger.** Money is recorded as append-only entries keyed by gateway references: `AT_RISK` at open,
   `RECOVERED_CAPTURE` / `RECOVERED_PARTIAL` from verified capture webhooks, `REFUNDED`, `SETTLED` from Razorpay
   settlement reconciliation, `COST` per outreach, `WRITTEN_OFF` on close.
4. **Scorecard.** For a batch or window:
   - per arm: cases, recovered cases, case recovery rate with Wilson 95% CI, rupees at risk / recovered /
     settled, rupee-weighted rate, settlement coverage;
   - lift: absolute and relative case-rate lift, two-proportion pooled z-test p-value, bootstrap (seeded,
     2,000 resamples) 95% CI on the rupee-weighted rate difference, incremental rupees =
     rate difference × treatment rupees at risk;
   - cost per recovered rupee and ROI multiple by channel unit costs;
   - compliance: outreach on holdout cases, executions despite a policy block, outreach after recovery,
     contact-policy blocks by rule, audit-chain verification;
   - audit coverage: fraction of executed actions with audit rows;
   - time-to-recovery percentiles for attributable recoveries;
   - breakdowns by surface, root cause, status and arm;
   - warnings for small arms (< 30 cases).

## Caveats we state on the scorecard

- Per-case (not per-customer) randomisation keeps n high in a demo but allows spillover if one customer has
  several cases; production should randomise per customer once volume allows.
- The behaviour model in the replay harness is an opinion, not a measurement: parameters live in
  `simulation/customer_model.py` for a merchant to replace with observed rates.
- Captured is not settled; both are shown.
