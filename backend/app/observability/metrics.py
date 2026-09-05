"""Prometheus metrics with a no-op fallback when prometheus_client is not installed."""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
    _AVAILABLE = True
except Exception:  # pragma: no cover
    _AVAILABLE = False


class _Metrics:
    def __init__(self):
        if not _AVAILABLE:
            return
        ns = "revenueshield"
        self.webhooks = Counter(f"{ns}_webhooks_total", "Webhook events received", ["event_type", "status"])
        self.cases_opened = Counter(f"{ns}_cases_opened_total", "Recovery cases opened", ["surface", "arm"])
        self.cases_recovered = Counter(f"{ns}_cases_recovered_total", "Recovery cases closed as recovered", ["surface"])
        self.ledger_amount = Counter(f"{ns}_ledger_rupees_total", "Rupees posted to the ledger by entry type", ["entry_type"])
        self.actions = Counter(f"{ns}_actions_total", "Customer-facing actions", ["channel", "status"])
        self.policy_blocks = Counter(f"{ns}_policy_blocks_total", "Actions blocked by a policy rule", ["rule"])
        self.agent_runs = Counter(f"{ns}_agent_runs_total", "Recovery agent runs", ["provider", "status", "degraded"])
        self.agent_tokens = Counter(f"{ns}_agent_tokens_total", "LLM tokens", ["direction"])
        self.jobs = Counter(f"{ns}_jobs_total", "Background jobs processed", ["kind", "status"])
        self.job_duration = Histogram(f"{ns}_job_duration_seconds", "Job duration", ["kind"], buckets=(0.05, 0.2, 0.5, 1, 2, 5, 10, 30, 60))
        self.http_duration = Histogram(f"{ns}_http_request_duration_seconds", "HTTP latency", ["method", "route", "status"], buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
        self.degradation_incidents = Gauge(f"{ns}_degradation_incidents_active", "Active issuer degradation incidents")
        self.approvals_pending = Gauge(f"{ns}_approvals_pending", "Approvals awaiting an operator")

    # --- facade methods (safe no-ops without the client) ---
    def webhook(self, event_type: str, status: str) -> None:
        if _AVAILABLE:
            self.webhooks.labels(event_type or "unknown", status).inc()

    def case_opened(self, surface: Optional[str], arm: Optional[str]) -> None:
        if _AVAILABLE:
            self.cases_opened.labels(surface or "UNKNOWN", arm or "TREATMENT").inc()

    def case_recovered(self, surface: Optional[str]) -> None:
        if _AVAILABLE:
            self.cases_recovered.labels(surface or "UNKNOWN").inc()

    def ledger(self, entry_type: str, amount: float) -> None:
        if _AVAILABLE and amount:
            self.ledger_amount.labels(entry_type).inc(float(amount))

    def action(self, channel: str, status: str) -> None:
        if _AVAILABLE:
            self.actions.labels(channel or "UNKNOWN", status).inc()

    def policy_block(self, rule: Optional[str]) -> None:
        if _AVAILABLE:
            self.policy_blocks.labels(rule or "UNKNOWN").inc()

    def agent_run(self, provider: str, status: str, degraded: bool, input_tokens: int = 0, output_tokens: int = 0) -> None:
        if _AVAILABLE:
            self.agent_runs.labels(provider, status, "true" if degraded else "false").inc()
            if input_tokens:
                self.agent_tokens.labels("input").inc(input_tokens)
            if output_tokens:
                self.agent_tokens.labels("output").inc(output_tokens)

    def job_processed(self, kind: str, status: str, seconds: float) -> None:
        if _AVAILABLE:
            self.jobs.labels(kind, status).inc()
            self.job_duration.labels(kind).observe(seconds)

    def http_request(self, method: str, route: str, status: int, seconds: float) -> None:
        if _AVAILABLE:
            self.http_duration.labels(method, route, str(status)).observe(seconds)

    def set_gauges(self, active_incidents: int, pending_approvals: int) -> None:
        if _AVAILABLE:
            self.degradation_incidents.set(active_incidents)
            self.approvals_pending.set(pending_approvals)

    @staticmethod
    def render() -> tuple[bytes, str]:
        if not _AVAILABLE:
            return b"# prometheus_client not installed\n", "text/plain"
        return generate_latest(), CONTENT_TYPE_LATEST


metrics = _Metrics()
