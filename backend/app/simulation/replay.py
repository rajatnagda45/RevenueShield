"""Batch replay: drive the real pipeline with a virtual clock and measure what came back.

Every event goes through the same adapter and event processor as a production webhook (optionally
over HTTP with a real HMAC signature). Recovery plans advance through the real scheduler (rules or
agent), customers respond through the behaviour model by emitting real `*.captured/paid/charged`
webhooks, settlements are reconciled, and the batch scorecard is computed from the ledger.
Chaos knobs inject duplicate webhooks and an LLM outage window so idempotency and graceful
degradation are measured, not asserted.
"""
from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.approvals import ApprovalService
from app.agent.providers.base import LLMProvider, resolve_provider
from app.agent.schemas import ProviderUnavailable
from app.analytics.scorecard import ScorecardService
from app.audit.chain import AuditChain
from app.compliance.consent import ConsentService
from app.core.config import settings
from app.degradation.monitor import DegradationMonitor
from app.detectors.checkout_abandonment import CheckoutAbandonmentDetector
from app.detectors.mandates import MandateRetrySequencer
from app.detectors.receivables import ReceivablesDetector
from app.integrations.razorpay.adapter import RazorpayAdapter
from app.integrations.razorpay.security import compute_razorpay_signature
from app.ledger.service import RECOVERY_TYPES
from app.ledger.settlement_reconciliation import SettlementReconRow, SettlementReconciliationService
from app.models.agent_run import AgentRun
from app.models.approval import Approval
from app.models.contact_attempt import ContactAttempt
from app.models.diagnosis import Diagnosis
from app.models.ledger_entry import LedgerEntry
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan
from app.services.event_processor import EventProcessor
from app.services.recovery_scheduler import RecoveryScheduler
from app.simulation.customer_model import CustomerBehaviourModel, TouchEvent
from app.simulation.scenarios import Scenario, ScenarioGenerator, SimCase

logger = logging.getLogger(__name__)


@dataclass
class ChaosConfig:
    duplicate_webhook_rate: float = 0.10        # fraction of open events re-sent with the same event id
    llm_outage_hours: Optional[tuple[float, float]] = (48.0, 60.0)  # virtual hours from start during which the LLM is "down"
    malformed_webhooks: int = 3                 # HTTP mode only: bodies that must be rejected without side effects


@dataclass
class ReplayConfig:
    n_cases: int = 200
    days: int = 10
    tick_hours: int = 12
    seed: int = 7
    holdout_percent: float = 10.0
    agent_enabled: bool = True
    provider: str = "auto"
    degradation_episode: bool = True
    batch_id: Optional[str] = None
    start: Optional[datetime] = None
    settlement_lag_days: int = 2
    approval_delay_hours: int = 2
    http_base_url: Optional[str] = None         # when set, events are POSTed to /webhooks/razorpay with a real signature
    chaos: ChaosConfig = field(default_factory=ChaosConfig)


class _OutageProvider:
    """Wraps a planner and refuses inside the configured virtual-time window."""

    def __init__(self, inner: LLMProvider, clock: Callable[[], datetime], start: datetime, window: Optional[tuple[float, float]]):
        self.inner = inner
        self.clock = clock
        self.start = start
        self.window = window
        self.name = inner.name
        self.model = inner.model
        self.outages = 0

    def plan(self, dossier, tools, transcript):
        if self.window:
            h = (self.clock() - self.start).total_seconds() / 3600.0
            if self.window[0] <= h < self.window[1]:
                self.outages += 1
                raise ProviderUnavailable("simulated LLM outage (chaos window)")
        return self.inner.plan(dossier, tools, transcript)


class BatchReplay:
    def __init__(self, db: Session, config: ReplayConfig):
        self.db = db
        self.cfg = config
        self.rng = random.Random(config.seed * 31)
        self.model = CustomerBehaviourModel(seed=config.seed)
        self.now: datetime = config.start or (datetime.now(timezone.utc) - timedelta(days=config.days + 2)).replace(minute=0, second=0, microsecond=0)
        self.gen = ScenarioGenerator(seed=config.seed, batch_id=config.batch_id)
        self.stats: Dict[str, Any] = {
            "events_ingested": 0, "events_duplicate": 0, "duplicates_injected": 0, "duplicate_side_effects": 0, "malformed_rejected": 0,
            "captures_emitted": 0, "opt_outs": 0, "plan_evaluations": 0, "approvals_decided": 0, "sweeps": 0, "degradation_ticks": 0,
            "wall_seconds": 0.0, "http_mode": bool(config.http_base_url),
        }
        self._case_by_key: Dict[str, uuid.UUID] = {}
        self._touch_cursor: Dict[uuid.UUID, int] = {}
        self._outage: Optional[_OutageProvider] = None
        self._http = None

    # ------------------------------------------------------------------ ingestion

    def _ingest(self, payload: Dict[str, Any], event_id: Optional[str] = None) -> Dict[str, Any]:
        event_id = event_id or f"evt_{uuid.uuid4().hex[:16]}"
        if self.cfg.http_base_url:
            import httpx
            if self._http is None:
                self._http = httpx.Client(base_url=self.cfg.http_base_url, timeout=30.0)
            raw = json.dumps(payload).encode()
            res = self._http.post("/webhooks/razorpay", content=raw, headers={"Content-Type": "application/json", "X-Razorpay-Signature": compute_razorpay_signature(raw, settings.RAZORPAY_WEBHOOK_SECRET or ""), "x-razorpay-event-id": event_id})
            body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {"status": "error", "http": res.status_code}
        else:
            normalized = RazorpayAdapter.normalize(payload, event_id_header=event_id)
            result = EventProcessor.process_normalized_event(self.db, normalized)
            body = result.model_dump()
        if body.get("status") == "duplicate":
            self.stats["events_duplicate"] += 1
        else:
            self.stats["events_ingested"] += 1
        return body

    # ------------------------------------------------------------------ run

    def run(self) -> Dict[str, Any]:
        t0 = time.monotonic()
        cfg = self.cfg
        saved = self._apply_settings()
        try:
            spread_days = max(1, min(3, cfg.days // 2))  # all leaks land in the first part of the window so every case gets a full recovery horizon
            scenario = self.gen.generate(n_cases=cfg.n_cases, start=self.now, spread_days=spread_days, degradation_episode=cfg.degradation_episode)
            self._install_agent_provider(scenario)

            # 0. Baseline traffic (healthy captures) so the degradation monitor has a baseline
            for ev in scenario.baseline_events:
                self._ingest(ev)

            # 1. Replay the batch over the virtual clock
            open_events = sorted(scenario.all_open_events(), key=lambda x: x[0])
            end = self.now + timedelta(days=cfg.days)
            tick = timedelta(hours=cfg.tick_hours)
            pending_idx = 0
            active_cases = {c.key: c for c in scenario.cases}
            monitor_step = timedelta(minutes=max(1, settings.DEGRADATION_WINDOW_MINUTES // 3))  # ~5 min: the production worker tick
            while self.now < end:
                # Sub-grid: ingest events in monitor-window slices and run the degradation monitor after each slice,
                # so a 15-minute issuer outage is seen while it happens and later failures in that cell are tagged systemic.
                slice_end = self.now + tick
                cursor = self.now
                while cursor < slice_end:
                    cursor = min(cursor + monitor_step, slice_end)
                    ingested_in_slice = 0
                    while pending_idx < len(open_events) and open_events[pending_idx][0] <= cursor:
                        at, payload, key = open_events[pending_idx]
                        res = self._ingest(payload)
                        ingested_in_slice += 1
                        if res.get("recovery_case_id"):
                            self._case_by_key[key] = uuid.UUID(res["recovery_case_id"])
                        if self.rng.random() < cfg.chaos.duplicate_webhook_rate:
                            # re-send the same event id: must be a no-op
                            before = self._side_effect_fingerprint(res.get("recovery_case_id"))
                            dup = self._ingest(payload, event_id=res.get("event_id"))
                            self.stats["duplicates_injected"] += 1
                            if dup.get("status") != "duplicate" or self._side_effect_fingerprint(res.get("recovery_case_id")) != before:
                                self.stats["duplicate_side_effects"] += 1
                        pending_idx += 1
                    if ingested_in_slice or cursor == slice_end:
                        DegradationMonitor.evaluate(self.db, now=cursor)
                        self.stats["degradation_ticks"] += 1
                self.db.commit()

                # surface sweeps
                CheckoutAbandonmentDetector.sweep(self.db, reference_time=self.now, abandon_after_minutes=30)
                ReceivablesDetector.sweep(self.db, reference_time=self.now)
                MandateRetrySequencer.sweep(self.db, now=self.now, dry_run=True)
                self.stats["sweeps"] += 1
                self.db.commit()

                # recovery plans: advance every treatment case whose plan is due (holdout cases just get their OBSERVE step)
                self._advance_plans()
                self.db.commit()

                # simulated operator on the approval queue
                self._decide_approvals()
                self.db.commit()

                # customers respond
                self._customers_respond(scenario, active_cases)
                self.db.commit()

                self.now += tick

            # 2. Settlements: everything captured settles T+lag
            recon = self._settle()
            self.db.commit()

            # 3. Measure
            scorecard = ScorecardService.compute(self.db, batch_id=scenario.batch_id, seed=cfg.seed)
            chain = AuditChain.verify(self.db)
            agent_stats = self._agent_stats(scenario)
            self.stats["wall_seconds"] = round(time.monotonic() - t0, 2)
            from app.models.degradation_incident import DegradationIncident
            incidents = self.db.scalars(select(DegradationIncident).order_by(DegradationIncident.opened_at.asc())).all()
            report = {
                "batch_id": scenario.batch_id,
                "degradation": {
                    "incidents": len(incidents),
                    "by_status": self._count(i.status for i in incidents),
                    "cells": [f"{i.bank}/{i.payment_method}" for i in incidents],
                    "affected_cases": sum(i.affected_case_count for i in incidents),
                    "systemic_diagnoses": int(scorecard.get("breakdowns", {}).get("by_root_cause", {}).get("SYSTEMIC_ISSUER_DEGRADATION", {}).get("cases", 0)),
                },
                "config": {**{k: v for k, v in asdict(cfg).items() if k not in ("start",)}, "start": scenario.start.isoformat()},
                "scenario": {
                    "cases": len(scenario.cases), "systemic_cases": sum(1 for c in scenario.cases if c.systemic),
                    "surface_mix": self._count(c.surface for c in scenario.cases),
                    "degradation_window": [scenario.degradation_window[0].isoformat(), scenario.degradation_window[1].isoformat()] if scenario.degradation_window else None,
                },
                "replay_stats": self.stats,
                "agent": agent_stats,
                "settlement": {k: v for k, v in recon.items() if k != "matched"},
                "audit_chain": {"ok": chain["ok"], "rows": chain["chained_rows"], "first_break": chain["first_break"]},
                "scorecard": scorecard,
            }
            return report
        finally:
            self._restore_settings(saved)
            self._uninstall_agent_provider()
            if self._http is not None:
                self._http.close()

    # ------------------------------------------------------------------ steps

    def _advance_plans(self) -> None:
        plans = self.db.scalars(
            select(RecoveryPlan).join(RecoveryCase, RecoveryCase.id == RecoveryPlan.recovery_case_id)
            .where(RecoveryCase.batch_id == self.gen.batch_id, RecoveryPlan.status.in_(["ACTIVE", "WAITING"]))
        ).all()
        for plan in plans:
            due = plan.status == "ACTIVE" or (plan.next_evaluation_at is not None and _aware(plan.next_evaluation_at) <= self.now)
            if not due:
                continue
            try:
                RecoveryScheduler.evaluate_and_advance_plan(self.db, plan.id, reference_time=self.now, dry_run=True)
                self.stats["plan_evaluations"] += 1
            except Exception as exc:
                # Surface the failure with the plan id; a silent rollback would hide a real defect behind a good-looking scorecard.
                logger.exception(f"[REPLAY] plan {plan.id} failed: {exc}")
                self.stats["plan_errors"] = self.stats.get("plan_errors", 0) + 1
                raise RuntimeError(f"replay failed while advancing plan {plan.id}: {type(exc).__name__}: {exc}") from exc

    def _decide_approvals(self) -> None:
        cutoff = self.now - timedelta(hours=self.cfg.approval_delay_hours)
        pending = self.db.scalars(select(Approval).join(RecoveryCase, RecoveryCase.id == Approval.recovery_case_id).where(RecoveryCase.batch_id == self.gen.batch_id, Approval.status == "PENDING", Approval.requested_at <= cutoff)).all()
        for a in pending:
            try:
                if self.model.approves():
                    ApprovalService.approve(self.db, a.id, operator="sim-operator", note="approved in replay", dry_run=True, now=self.now)
                else:
                    ApprovalService.reject(self.db, a.id, operator="sim-operator", note="rejected in replay", now=self.now)
                self.stats["approvals_decided"] += 1
            except ValueError:
                continue
        ApprovalService.expire_due(self.db, now=self.now)

    def _customers_respond(self, scenario: Scenario, active_cases: Dict[str, SimCase]) -> None:
        cases = self.db.scalars(select(RecoveryCase).where(RecoveryCase.batch_id == self.gen.batch_id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"]))).all()
        by_id = {c.id: c for c in cases}
        key_by_id = {v: k for k, v in self._case_by_key.items()}
        for case in cases:
            key = key_by_id.get(case.id)
            sim = active_cases.get(key) if key else None
            if sim is None:
                continue
            diag = self.db.scalar(select(Diagnosis).where(Diagnosis.recovery_case_id == case.id).order_by(Diagnosis.created_at.desc()))
            category = diag.category if diag else "UNKNOWN"
            customer = case.customer
            touches_all = self.db.scalars(select(ContactAttempt).where(ContactAttempt.recovery_case_id == case.id, ContactAttempt.outcome == "SENT").order_by(ContactAttempt.occurred_at.asc())).all()
            cursor = self._touch_cursor.get(case.id, 0)
            new_touches = touches_all[cursor:]
            self._touch_cursor[case.id] = len(touches_all)
            touch_events = [TouchEvent(channel=t.channel, personalised=bool((t.attempt_metadata or {}).get("personalised")), language=sim.customer.language if t.channel == "WHATSAPP" else None, payment_plan=bool((case.case_metadata or {}).get("payment_plan"))) for t in new_touches]

            # opt-out after fatigue
            if new_touches and self.model.opts_out(touches_total=len(touches_all)) and customer is not None:
                ConsentService.process_inbound_text(self.db, customer=customer, text="STOP", channel="WHATSAPP", case=case)
                self.stats["opt_outs"] += 1
                continue

            systemic_active = DegradationMonitor.case_is_held(self.db, case)
            if self.model.pays(category=category, segment=sim.customer.segment, hours=self.cfg.tick_hours, touches=touch_events, touches_before=cursor, preferred_language=sim.customer.language, systemic_active=systemic_active):
                at = self.now + timedelta(minutes=self.rng.randint(5, self.cfg.tick_hours * 60 - 5))
                self._ingest(self.gen.recovery_event(sim, at=at))
                self.stats["captures_emitted"] += 1

    def _settle(self) -> Dict[str, Any]:
        rows: List[SettlementReconRow] = []
        entries = self.db.scalars(
            select(LedgerEntry).join(RecoveryCase, RecoveryCase.id == LedgerEntry.recovery_case_id)
            .where(RecoveryCase.batch_id == self.gen.batch_id, LedgerEntry.entry_type.in_(RECOVERY_TYPES))
        ).all()
        for e in entries:
            rows.append(SettlementReconRow(entity_id=e.provider_reference, settlement_id=f"setl_sim_{_aware(e.occurred_at).strftime('%Y%m%d')}", amount=e.amount, settled_at=_aware(e.occurred_at) + timedelta(days=self.cfg.settlement_lag_days), settlement_utr=f"UTR{uuid.uuid4().hex[:10].upper()}"))
        return SettlementReconciliationService.reconcile_rows(self.db, rows)

    # ------------------------------------------------------------------ helpers

    def _side_effect_fingerprint(self, case_id: Optional[str]) -> tuple:
        if not case_id:
            return ()
        cid = uuid.UUID(case_id)
        n_ledger = self.db.scalar(select(__import__("sqlalchemy").func.count(LedgerEntry.id)).where(LedgerEntry.recovery_case_id == cid))
        n_cases = self.db.scalar(select(__import__("sqlalchemy").func.count(RecoveryCase.id)).where(RecoveryCase.batch_id == self.gen.batch_id))
        return (int(n_ledger or 0), int(n_cases or 0))

    def _agent_stats(self, scenario: Scenario) -> Dict[str, Any]:
        runs = self.db.scalars(select(AgentRun).join(RecoveryCase, RecoveryCase.id == AgentRun.recovery_case_id).where(RecoveryCase.batch_id == scenario.batch_id)).all()
        return {
            "enabled": self.cfg.agent_enabled, "provider": self._outage.inner.name if self._outage else None,
            "runs": len(runs), "degraded_to_rules": sum(1 for r in runs if r.degraded_to_rules), "outage_refusals": self._outage.outages if self._outage else 0,
            "by_status": self._count(r.status for r in runs), "by_action": self._count((r.final_plan or {}).get("action") for r in runs),
            "input_tokens": sum(r.input_tokens for r in runs), "output_tokens": sum(r.output_tokens for r in runs), "cost_usd": round(sum(r.cost_usd for r in runs), 4),
            "approvals": self._count(a.status for a in self.db.scalars(select(Approval).join(RecoveryCase, RecoveryCase.id == Approval.recovery_case_id).where(RecoveryCase.batch_id == scenario.batch_id)).all()),
        }

    @staticmethod
    def _count(values) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for v in values:
            out[str(v)] = out.get(str(v), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def _apply_settings(self) -> Dict[str, Any]:
        saved = {k: getattr(settings, k) for k in ("EXPERIMENTS_ENABLED", "HOLDOUT_PERCENT", "AGENT_DRIVES_PLANS", "EXECUTION_MODE", "WHATSAPP_MODE")}
        settings.EXPERIMENTS_ENABLED = True
        settings.HOLDOUT_PERCENT = self.cfg.holdout_percent
        settings.AGENT_DRIVES_PLANS = self.cfg.agent_enabled
        settings.EXECUTION_MODE = "dry_run"
        settings.WHATSAPP_MODE = "DRY_RUN"
        return saved

    @staticmethod
    def _restore_settings(saved: Dict[str, Any]) -> None:
        for k, v in saved.items():
            setattr(settings, k, v)

    def _install_agent_provider(self, scenario: Scenario) -> None:
        from app.agent import runner as runner_mod
        inner = resolve_provider(self.cfg.provider)
        self._outage = _OutageProvider(inner, clock=lambda: self.now, start=scenario.start, window=self.cfg.chaos.llm_outage_hours)
        runner_mod.RecoveryAgentRunner.provider_override = self._outage  # type: ignore[attr-defined]

    @staticmethod
    def _uninstall_agent_provider() -> None:
        from app.agent import runner as runner_mod
        runner_mod.RecoveryAgentRunner.provider_override = None  # type: ignore[attr-defined]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
