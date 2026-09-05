# Batch replay report - `demo_batch_300`

Seed 7 | 345 cases over 10 virtual days | holdout 10.0% | agent on (null) | wall clock 73.12s

## Money recovered (the bar)

| Arm | Cases | Recovered cases | Case recovery rate | At risk | Recovered | Settled |
|---|---:|---:|---:|---:|---:|---:|
| Treatment | 314 | 138 | 44.0% | Rs 5,279,067.41 | Rs 2,154,387.95 | Rs 2,154,387.95 |
| Holdout | 31 | 10 | 32.3% | Rs 1,122,308.32 | Rs 752,574.54 | Rs 752,574.54 |

**Incremental recovery attributable to the system: Rs -1,385,755.20** (95% CI Rs -3,152,236.73 to Rs 2,124,660.98); lift +11.7 pts (relative 36%), z = 1.2547, p = 0.209585.

## Compliance and audit

- Policy violations: **0** (holdout outreach 0, outreach after recovery 0)
- Policy blocks enforced: 0 plan steps; contact-policy blocks by rule: {}
- Audit coverage of executed actions: **100%** (314/314), 6621 audit rows, hash chain intact
- Duplicate webhooks injected: 30, side effects: **0**; opt-outs honoured: 24

## Payment degradation

- Incidents detected: 1 ['HDFC/UPI'] · status {'CLOSED': 1} · cases held 60 · failures diagnosed systemic (no customer contact): 13

## Agent

- Runs: 881; degraded to rules during the simulated LLM outage: 155 (refusals 155); approvals: {'APPROVED': 6, 'REJECTED': 1}
- Actions chosen: {'SEND_EMAIL': 780, 'NO_ACTION': 94, 'ESCALATE_TO_MERCHANT': 7}
- Tokens in/out: 0/0; cost USD 0.0

## Cost and timing

- Outreach cost: Rs 39.00; cost per recovered rupee: 1.8e-05; ROI multiple: 55240.72
- Time to recovery (hours): {'p50': 6.8, 'p90': 28.0}
- Settled: Rs 2,906,962.49 across 148 payments

## By surface

| Surface | Cases | Recovered | Rate | At risk | Recovered |
|---|---:|---:|---:|---:|---:|
| SUBSCRIPTION_MANDATE_FAILURE | 70 | 33 | 47.1% | Rs 525,821.15 | Rs 188,760.03 |
| PAYMENT_FAILURE | 204 | 87 | 42.6% | Rs 1,277,819.88 | Rs 635,797.13 |
| CHECKOUT_ABANDONMENT | 42 | 16 | 38.1% | Rs 122,741.08 | Rs 30,730.66 |
| RECEIVABLE_OVERDUE | 29 | 12 | 41.4% | Rs 4,474,993.62 | Rs 2,051,674.67 |

## By root cause

| Root cause | Cases | Recovered | Rate |
|---|---:|---:|---:|
| USER_FRICTION | 69 | 31 | 44.9% |
| AUTHENTICATION_FAILURE | 30 | 18 | 60.0% |
| INSUFFICIENT_FUNDS | 78 | 36 | 46.2% |
| BANK_TECHNICAL_FAILURE | 52 | 26 | 50.0% |
| BANK_DECLINE | 31 | 9 | 29.0% |
| POSSIBLE_FRAUD_OR_SECURITY | 5 | 0 | 0.0% |
| RECEIVABLE_OVERDUE | 29 | 12 | 41.4% |
| PAYMENT_METHOD_FAILURE | 38 | 13 | 34.2% |
| SYSTEMIC_ISSUER_DEGRADATION | 13 | 3 | 23.1% |

_Methodology:_ assignment: sha256(experiment_key:salt:case_id) bucket in [0,10000); bucket < holdout_bps -> HOLDOUT; recovered: ledger RECOVERED_CAPTURE + RECOVERED_PARTIAL - REFUNDED, from verified gateway webhooks; settled: ledger SETTLED entries from Razorpay settlement reconciliation; lift_test: two-proportion pooled z-test on case recovery rate; bootstrap (seeded) CI on rupee-weighted rate difference
