# RevenueShield Command Center v2

Next.js 16 + TypeScript + Tailwind v4. Talks to the FastAPI backend over HTTP; no server-side state.

```bash
cp .env.example .env.local        # set NEXT_PUBLIC_API_BASE_URL if the API is not on 127.0.0.1:8000
npm install
npm run dev                        # http://localhost:3000
```

Pages

| Route | What it shows |
|---|---|
| `/` | Incremental recovery, treatment vs holdout, violations, audit coverage, what needs a human |
| `/scorecard` | The batch scorecard: arms, lift with CI and p-value, compliance, audit, cost, breakdowns, methodology |
| `/cases`, `/cases/[id]` | Case list across all surfaces; dossier, precomputed policy verdicts, agent runs with traces, ledger, hash-chained audit trail |
| `/approvals` | Human-in-the-loop queue: approve / reject deferred agent actions |
| `/agent` | Agent runs, degradation to rules, cost; the bounded tool surface |
| `/degradation` | Bank x method failure-rate matrix vs baseline; incidents and their history |
| `/replay` | Run a seeded batch replay with chaos knobs and read the report |
| `/jobs` | Outbox health; run one worker cycle inline |
| `/audit` | Verify the audit hash chain |

Design notes: single validated palette in `globals.css` (light and selected dark steps); text always uses text
tokens; status colours are reserved for state; the degradation matrix uses one sequential hue with a status ring
for detected cells; every metric is also present as a table.
