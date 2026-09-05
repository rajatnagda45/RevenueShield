# RevenueShield - common commands. Works with GNU make on macOS/Linux/WSL/Git Bash.
PY ?= backend/venv/Scripts/python.exe
ifeq (,$(wildcard $(PY)))
PY := python
endif

.PHONY: help install test lint replay replay-http demo up down logs worker api

help:
	@echo "make install      - create venv deps (pip install -r backend/requirements.txt)"
	@echo "make test         - run the full test suite"
	@echo "make lint         - ruff correctness checks (undefined names, unused imports)"
	@echo "make replay       - replay 1,000 cases (15% holdout) through the real pipeline into reports/ (no external services needed)"
	@echo "make demo         - docker compose up (postgres + api + worker), then replay over HTTP with real HMAC"
	@echo "make up / down    - docker compose lifecycle"
	@echo "make api          - run the API locally (uvicorn, reload)"
	@echo "make worker       - run the background worker locally"

install:
	cd backend && pip install -r requirements.txt -r requirements-dev.txt

test:
	cd backend && $(abspath $(PY)) -m pytest tests -q

lint:
	cd backend && $(abspath $(PY)) -m ruff check app scripts

replay:
	cd backend && DATABASE_URL=sqlite:///../reports/replay_demo_1000.db $(abspath $(PY)) scripts/replay_batch.py --n 1000 --days 10 --seed 7 --holdout 15 --agent --provider auto --batch-id demo_batch_1000 --init-db --out ../reports

replay-http:
	cd backend && $(abspath $(PY)) scripts/replay_batch.py --n 100 --days 7 --seed 7 --agent --http http://127.0.0.1:8000 --out ../reports

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f backend worker

demo: up
	@echo "Waiting for the API..." && sleep 8 && curl -sf http://127.0.0.1:8000/health/ready && echo && $(MAKE) replay-http

api:
	cd backend && $(abspath $(PY)) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

worker:
	cd backend && $(abspath $(PY)) scripts/run_worker.py
