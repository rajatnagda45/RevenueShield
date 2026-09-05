# Batch replay report - `demo_batch_1000`

Seed 7 | 1045 cases over 10 virtual days | holdout 15.0% | agent on (null) | wall clock 62.21s

## Money recovered (the bar)

| Arm | Cases | Recovered cases | Case recovery rate | At risk | Recovered | Settled |
|---|---:|---:|---:|---:|---:|---:|
| Treatment | 1499 | 645 | 43.0% | Rs 20,403,302.12 | Rs 11,481,968.22 | Rs 11,082,636.27 |
| Holdout | 270 | 96 | 35.6% | Rs 2,303,800.11 | Rs 690,710.45 | Rs 416,933.21 |

**Incremental recovery attributable to the system: Rs 5,366,068.46** (95% CI Rs 2,221,123.87 to Rs 8,067,730.90); lift +7.5 pts (relative 21%), z = 2.2911, p = 0.021958.

## Compliance and audit

- Policy violations: **104** (holdout outreach 0, outreach after recovery 104)
- Policy blocks enforced: 0 plan steps; contact-policy blocks by rule: {}
- Audit coverage of executed actions: **100%** (1129/1129), 34245 audit rows, hash chain intact
- Duplicate webhooks injected: 106, side effects: **0**; opt-outs honoured: 27

## Payment degradation

- Incidents detected: 2 ['HDFC/UPI', 'HDFC/UPI'] · status {'CLOSED': 2} · cases held 184 · failures diagnosed systemic (no customer contact): 12

## Agent

- Runs: 4587; degraded to rules during the simulated LLM outage: 672 (refusals 318); approvals: {'APPROVED': 34, 'REJECTED': 6}
- Actions chosen: {'SEND_EMAIL': 2618, 'SCHEDULE_RETRY': 1226, 'NO_ACTION': 588, 'WAIT': 115, 'ESCALATE_TO_MERCHANT': 40}
- Tokens in/out: 0/0; cost USD 0.0

## Cost and timing

- Outreach cost: Rs 130.90; cost per recovered rupee: 1.1e-05; ROI multiple: 87715.57
- Time to recovery (hours): {'p50': 7.7, 'p90': 38.29}
- Settled: Rs 12,622,248.60 across 741 payments

## By surface

| Surface | Cases | Recovered | Rate | At risk | Recovered |
|---|---:|---:|---:|---:|---:|
| PAYMENT_FAILURE | 1170 | 424 | 36.2% | Rs 8,350,437.98 | Rs 3,027,777.81 |
| SUBSCRIPTION_MANDATE_FAILURE | 272 | 148 | 54.4% | Rs 2,636,832.79 | Rs 1,630,078.59 |
| CHECKOUT_ABANDONMENT | 220 | 102 | 46.4% | Rs 807,423.34 | Rs 541,885.34 |
| RECEIVABLE_OVERDUE | 107 | 67 | 62.6% | Rs 10,912,408.12 | Rs 6,972,936.93 |

## Lift by surface (treatment vs holdout)

| Surface | Treatment (rec/cases, rate) | Holdout (rec/cases, rate) | Lift |
|---|---:|---:|---:|
| CHECKOUT_ABANDONMENT | 94/189, 49.7% | 8/31, 25.8% | +23.9 pts |
| PAYMENT_FAILURE | 355/982, 36.1% | 69/188, 36.7% | -0.5 pts |
| RECEIVABLE_OVERDUE | 60/94, 63.8% | 7/13, 53.8% | +10.0 pts |
| SUBSCRIPTION_MANDATE_FAILURE | 136/234, 58.1% | 12/38, 31.6% | +26.5 pts |

## By root cause

| Root cause | Cases | Recovered | Rate |
|---|---:|---:|---:|
| AUTHENTICATION_FAILURE | 185 | 81 | 43.8% |
| INSUFFICIENT_FUNDS | 450 | 224 | 49.8% |
| BANK_TECHNICAL_FAILURE | 242 | 110 | 45.5% |
| USER_FRICTION | 337 | 146 | 43.3% |
| PAYMENT_METHOD_FAILURE | 232 | 57 | 24.6% |
| BANK_DECLINE | 154 | 49 | 31.8% |
| RECEIVABLE_OVERDUE | 107 | 67 | 62.6% |
| POSSIBLE_FRAUD_OR_SECURITY | 50 | 1 | 2.0% |
| SYSTEMIC_ISSUER_DEGRADATION | 12 | 6 | 50.0% |

_Methodology:_ assignment: sha256(experiment_key:salt:case_id) bucket in [0,10000); bucket < holdout_bps -> HOLDOUT; recovered: ledger RECOVERED_CAPTURE + RECOVERED_PARTIAL - REFUNDED, from verified gateway webhooks; settled: ledger SETTLED entries from Razorpay settlement reconciliation; lift_test: two-proportion pooled z-test on case recovery rate; bootstrap (seeded) CI on rupee-weighted rate difference; primary_metric: case recovery rate lift (unit-weighted). Rupee-weighted lift is reported but is sensitive to a few large receivables in either arm; read it with its bootstrap CI.
