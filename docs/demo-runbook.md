# Demo runbook

## A. Offline in two minutes (no keys, no Docker)

```bash
cd backend
python -m venv venv && venv/Scripts/activate      # or source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest tests -q                                    # 406 passed
cd ..
make replay                                        # 1,000 cases, 15% holdout, 10 virtual days, agent on (about 4 minutes)
```

`make replay` writes `reports/demo_batch_*.md` and `.json`. Open the Markdown: treatment vs holdout, incremental
rupees with a confidence interval, zero violations, intact audit chain, duplicate webhooks with zero side
effects, agent runs that degraded to rules during the injected outage.

## B. API + Command Center against the replay database

```bash
cd backend
DATABASE_URL=sqlite:///../reports/replay_demo_1000.db EXPERIMENTS_ENABLED=true uvicorn app.main:app --port 8000
# new terminal
cd frontend-next && cp .env.example .env.local && npm install && npm run dev   # http://localhost:3000
```

Walk: Overview -> Scorecard (pick the batch) -> Cases (filter HOLDOUT, open one: verdicts show every action
blocked by `HOLDOUT_ARM_OBSERVE_ONLY`) -> open a treatment case, press "Run recovery agent", read the trace
-> Approvals -> Degradation (matrix with the HDFC/UPI cell ringed, incident history) -> Audit (verify).

## C. Full stack with Docker

```bash
cp .env.example .env            # optionally add OPENAI_API_KEY (or ANTHROPIC_API_KEY) and Razorpay test keys
make up                         # postgres + api (runs alembic) + worker
make replay-http                # drives /webhooks/razorpay over HTTP with real HMAC signatures
make logs
```

## D. Live Razorpay test mode (optional)

Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `EXECUTION_MODE=razorpay_test`, expose
the API (e.g. ngrok) and register `POST /webhooks/razorpay` in the Razorpay dashboard for payment, order,
invoice, subscription, refund and settlement events. Create a payment in test mode and fail it; watch the case
open, the agent plan, and the payment link appear; pay the link; watch the case close and the ledger update.

## E. LLM planner (GPT-4o or Claude)

Put `OPENAI_API_KEY=sk-...` in `backend/.env` (model `LLM_MODEL_PLANNER_OPENAI`, default `gpt-4o`), or
`ANTHROPIC_API_KEY` for Claude. `LLM_PROVIDER=auto` uses Claude if that key exists, else GPT-4o, else rules.

```bash
cd backend
DATABASE_URL=sqlite:///../reports/replay_demo_1000.db python scripts/smoke_llm_planner.py   # one dry-run plan, prints trace + cost
DATABASE_URL=sqlite:///../reports/replay_demo_openai.db python scripts/replay_batch.py --n 100 --days 7 --seed 7 --holdout 15 --agent --provider openai --batch-id demo_openai_100 --init-db --out ../reports
```

The second command replays 100 cases with GPT-4o planning every evaluation (a few hundred calls, low single-digit
USD). Compare its report with the rules-planner run on the same seed; the `degraded_to_rules` count shows how
often the breaker had to step in. In the Command Center the Replay page has an `openai` option, and a case's
"Run recovery agent" button uses whatever `auto` resolves to.

## Things to show if asked "what broke?"

`POSTMORTEMS.md`: eight incidents, each with the failing output, the root cause, the fix and the prevention.
Highlights: the holdout arm made 1-in-10 legacy tests flaky (by design); two latent v1 production bugs found
by the first real webhook contract test; a silent rollback that hid a real defect behind a clean scorecard.
