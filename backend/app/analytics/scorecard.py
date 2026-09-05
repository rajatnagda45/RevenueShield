"""Batch scorecard: the single artefact that answers the Track 3 bar.

"Show measured money recovered across a batch, with compliant escalation, stopping rules, and an
audit trail." This service computes, for any batch or time window:

* rupees at risk / recovered (captured) / settled, per experiment arm
* incremental lift of treatment over holdout with a z-test p-value and a bootstrap CI
* cost per recovered rupee by channel
* compliance: policy violations (must be 0), outreach on holdout cases (must be 0)
* audit coverage: every executed action has audit rows
* time-to-recovery percentiles and breakdowns by surface, root cause and channel
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.analytics.stats import bootstrap_rate_difference, percentiles, two_proportion_ztest, wilson_interval
from app.experiments.assigner import ExperimentArm
from app.ledger.service import RECOVERY_TYPES, LedgerEntryType
from app.models.audit_log import AuditLog
from app.models.communication import Communication
from app.models.diagnosis import Diagnosis
from app.models.execution import RecoveryExecution
from app.models.intervention import Intervention
from app.models.ledger_entry import LedgerEntry
from app.models.outcome import RecoveryOutcome
from app.models.recovery_action import RecoveryAction
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.models.voice_call import VoiceCall

logger = logging.getLogger(__name__)

SMALL_SAMPLE_THRESHOLD = 30


class ScorecardService:
    """Computes the batch scorecard from the ledger and audit tables."""

    @classmethod
    def compute(
        cls,
        db: Session,
        batch_id: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        surface: Optional[str] = None,
        seed: int = 42,
    ) -> Dict[str, Any]:
        filters = []
        if batch_id:
            filters.append(RecoveryCase.batch_id == batch_id)
        if start:
            filters.append(RecoveryCase.created_at >= start)
        if end:
            filters.append(RecoveryCase.created_at <= end)
        if surface:
            filters.append(RecoveryCase.leak_surface == surface)

        cases: List[RecoveryCase] = list(db.scalars(select(RecoveryCase).where(and_(*filters)) if filters else select(RecoveryCase)).all())
        case_ids = [c.id for c in cases]
        if not case_ids:
            return cls._empty(batch_id, start, end, surface)

        ledger_by_case = cls._ledger_totals(db, case_ids)
        arms = cls._split_arms(cases)

        arm_stats = {arm: cls._arm_stats(arm_cases, ledger_by_case) for arm, arm_cases in arms.items()}
        lift = cls._lift(arm_stats, arms, ledger_by_case, seed)
        compliance = cls._compliance(db, cases, arms)
        audit = cls._audit_coverage(db, case_ids)
        costs = cls._costs(db, case_ids, arm_stats)
        timing = cls._timing(db, case_ids)
        breakdowns = cls._breakdowns(db, cases, ledger_by_case)

        warnings: List[str] = []
        for arm_name, st in arm_stats.items():
            if st["cases"] < SMALL_SAMPLE_THRESHOLD:
                warnings.append(f"{arm_name} arm has only {st['cases']} cases; treat lift estimates as indicative.")
        if arm_stats.get(ExperimentArm.HOLDOUT.value, {}).get("cases", 0) == 0:
            warnings.append("No holdout cases in scope: incremental lift cannot be estimated (only gross recovery shown).")

        return {
            "scope": {
                "batch_id": batch_id,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "leak_surface": surface,
                "cases": len(cases),
            },
            "arms": arm_stats,
            "lift": lift,
            "costs": costs,
            "compliance": compliance,
            "audit": audit,
            "timing": timing,
            "breakdowns": breakdowns,
            "warnings": warnings,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "methodology": {
                "assignment": "sha256(experiment_key:salt:case_id) bucket in [0,10000); bucket < holdout_bps -> HOLDOUT",
                "recovered": "ledger RECOVERED_CAPTURE + RECOVERED_PARTIAL - REFUNDED, from verified gateway webhooks",
                "settled": "ledger SETTLED entries from Razorpay settlement reconciliation",
                "lift_test": "two-proportion pooled z-test on case recovery rate; bootstrap (seeded) CI on rupee-weighted rate difference",
                "primary_metric": "case recovery rate lift (unit-weighted). Rupee-weighted lift is reported but is sensitive to a few large receivables in either arm; read it with its bootstrap CI.",
            },
        }

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _empty(batch_id, start, end, surface) -> Dict[str, Any]:
        return {
            "scope": {"batch_id": batch_id, "start": start.isoformat() if start else None, "end": end.isoformat() if end else None, "leak_surface": surface, "cases": 0},
            "arms": {}, "lift": {}, "costs": {}, "compliance": {"policy_violations": 0, "holdout_outreach_count": 0, "violations_by_rule": {}},
            "audit": {"executed_actions": 0, "audited_actions": 0, "coverage": 1.0}, "timing": {}, "breakdowns": {},
            "warnings": ["No cases in scope."], "generated_at": datetime.utcnow().isoformat() + "Z", "methodology": {},
        }

    @staticmethod
    def _split_arms(cases: List[RecoveryCase]) -> Dict[str, List[RecoveryCase]]:
        arms: Dict[str, List[RecoveryCase]] = {ExperimentArm.TREATMENT.value: [], ExperimentArm.HOLDOUT.value: []}
        for c in cases:
            arm = c.experiment_arm or ExperimentArm.TREATMENT.value
            arms.setdefault(arm, []).append(c)
        return arms

    @staticmethod
    def _ledger_totals(db: Session, case_ids: List[uuid.UUID]) -> Dict[uuid.UUID, Dict[str, Decimal]]:
        rows = db.execute(
            select(LedgerEntry.recovery_case_id, LedgerEntry.entry_type, func.coalesce(func.sum(LedgerEntry.amount), 0))
            .where(LedgerEntry.recovery_case_id.in_(case_ids))
            .group_by(LedgerEntry.recovery_case_id, LedgerEntry.entry_type)
        ).all()
        totals: Dict[uuid.UUID, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
        for cid, etype, amt in rows:
            totals[cid][etype] = Decimal(str(amt or 0))
        return totals

    @classmethod
    def _case_recovered(cls, case: RecoveryCase, ledger: Dict[str, Decimal]) -> Decimal:
        rec = sum((ledger.get(t, Decimal("0.00")) for t in RECOVERY_TYPES), Decimal("0.00"))
        rec -= ledger.get(LedgerEntryType.REFUNDED.value, Decimal("0.00"))
        if rec == 0 and case.recovered_amount:
            rec = Decimal(str(case.recovered_amount))  # fallback for cases created before the ledger existed
        return max(rec, Decimal("0.00"))

    @classmethod
    def _arm_stats(cls, arm_cases: List[RecoveryCase], ledger_by_case) -> Dict[str, Any]:
        n = len(arm_cases)
        at_risk = Decimal("0.00")
        recovered = Decimal("0.00")
        settled = Decimal("0.00")
        recovered_cases = 0
        for c in arm_cases:
            ledger = ledger_by_case.get(c.id, {})
            risk = ledger.get(LedgerEntryType.AT_RISK.value) or Decimal(str(c.amount_at_risk or 0))
            rec = cls._case_recovered(c, ledger)
            at_risk += risk
            recovered += rec
            settled += ledger.get(LedgerEntryType.SETTLED.value, Decimal("0.00"))
            if rec > 0:
                recovered_cases += 1
        case_rate = recovered_cases / n if n else 0.0
        rupee_rate = float(recovered / at_risk) if at_risk > 0 else 0.0
        lo, hi = wilson_interval(recovered_cases, n)
        return {
            "cases": n,
            "recovered_cases": recovered_cases,
            "case_recovery_rate": round(case_rate, 4),
            "case_recovery_rate_ci95": [lo, hi],
            "amount_at_risk": float(at_risk),
            "amount_recovered": float(recovered),
            "amount_settled": float(settled),
            "rupee_recovery_rate": round(rupee_rate, 4),
            "settlement_coverage": round(float(settled / recovered), 4) if recovered > 0 else 0.0,
        }

    @classmethod
    def _lift(cls, arm_stats, arms, ledger_by_case, seed: int) -> Dict[str, Any]:
        t = arm_stats.get(ExperimentArm.TREATMENT.value)
        h = arm_stats.get(ExperimentArm.HOLDOUT.value)
        if not t or not h or h["cases"] == 0 or t["cases"] == 0:
            return {"available": False, "reason": "Both arms need at least one case."}

        abs_lift = t["case_recovery_rate"] - h["case_recovery_rate"]
        rel_lift = (abs_lift / h["case_recovery_rate"]) if h["case_recovery_rate"] > 0 else None
        z, p = two_proportion_ztest(t["recovered_cases"], t["cases"], h["recovered_cases"], h["cases"])

        def arrays(cases):
            rec, risk = [], []
            for c in cases:
                ledger = ledger_by_case.get(c.id, {})
                risk.append(float(ledger.get(LedgerEntryType.AT_RISK.value) or Decimal(str(c.amount_at_risk or 0))))
                rec.append(float(cls._case_recovered(c, ledger)))
            return rec, risk

        t_rec, t_risk = arrays(arms[ExperimentArm.TREATMENT.value])
        h_rec, h_risk = arrays(arms[ExperimentArm.HOLDOUT.value])
        boot = bootstrap_rate_difference(t_rec, t_risk, h_rec, h_risk, seed=seed)
        rupee_rate_diff = t["rupee_recovery_rate"] - h["rupee_recovery_rate"]
        incremental_rupees = rupee_rate_diff * t["amount_at_risk"]

        return {
            "available": True,
            "absolute_lift_case_rate": round(abs_lift, 4),
            "relative_lift_case_rate": round(rel_lift, 4) if rel_lift is not None else None,
            "z_statistic": z,
            "p_value": p,
            "significant_at_5pct": bool(p is not None and p < 0.05),
            "rupee_rate_difference": round(rupee_rate_diff, 4),
            "rupee_rate_difference_ci95": [boot["lower"], boot["upper"]],
            "incremental_rupees_recovered": round(incremental_rupees, 2),
            "incremental_rupees_ci95": [
                round(boot["lower"] * t["amount_at_risk"], 2) if boot["lower"] is not None else None,
                round(boot["upper"] * t["amount_at_risk"], 2) if boot["upper"] is not None else None,
            ],
            "interpretation": (
                f"Treatment recovered {t['case_recovery_rate']:.1%} of cases vs {h['case_recovery_rate']:.1%} in holdout "
                f"(+{abs_lift * 100:.1f} pts). Incremental recovery attributable to the system: Rs {incremental_rupees:,.2f}."
            ),
        }

    @classmethod
    def _compliance(cls, db: Session, cases: List[RecoveryCase], arms) -> Dict[str, Any]:
        holdout_ids = [c.id for c in arms.get(ExperimentArm.HOLDOUT.value, [])]
        case_ids = [c.id for c in cases]
        violations_by_rule: Dict[str, int] = defaultdict(int)

        holdout_outreach = 0
        if holdout_ids:
            holdout_outreach += int(db.scalar(select(func.count(Communication.id)).where(Communication.recovery_case_id.in_(holdout_ids), Communication.status.in_(["SENT", "DELIVERED", "READ"]))) or 0)
            holdout_outreach += int(db.scalar(select(func.count(VoiceCall.id)).where(VoiceCall.recovery_case_id.in_(holdout_ids))) or 0)
            holdout_outreach += int(db.scalar(select(func.count(Intervention.id)).where(Intervention.recovery_case_id.in_(holdout_ids), Intervention.status.in_(["SENT", "SUCCEEDED"]))) or 0)
            holdout_outreach += int(db.scalar(select(func.count(RecoveryExecution.id)).where(RecoveryExecution.recovery_case_id.in_(holdout_ids), RecoveryExecution.status == "SUCCEEDED")) or 0)
        if holdout_outreach:
            violations_by_rule["HOLDOUT_ARM_OBSERVE_ONLY"] = holdout_outreach

        # Executions that succeeded although their action was blocked by policy.
        rows = db.execute(
            select(RecoveryExecution.id, RecoveryAction.policy_result)
            .join(RecoveryAction, RecoveryAction.id == RecoveryExecution.recovery_action_id)
            .where(RecoveryExecution.recovery_case_id.in_(case_ids), RecoveryExecution.status == "SUCCEEDED")
        ).all()
        exec_against_policy = sum(1 for _, pr in rows if isinstance(pr, dict) and pr.get("allowed") is False)
        if exec_against_policy:
            violations_by_rule["EXECUTED_DESPITE_POLICY_BLOCK"] = exec_against_policy

        # Outreach after the case was recovered (stopping rule breach): any SENT communication after closed_at.
        post_recovery = 0
        recovered_cases = [c for c in cases if c.status == "RECOVERED" and c.closed_at]
        if recovered_cases:
            for c in recovered_cases:
                cnt = db.scalar(
                    select(func.count(Communication.id)).where(
                        Communication.recovery_case_id == c.id,
                        Communication.status.in_(["SENT", "DELIVERED"]),
                        Communication.sent_at > c.closed_at,
                    )
                ) or 0
                post_recovery += int(cnt)
        if post_recovery:
            violations_by_rule["OUTREACH_AFTER_RECOVERY"] = post_recovery

        blocked_steps = int(db.scalar(
            select(func.count(RecoveryPlanStep.id))
            .join(RecoveryPlan, RecoveryPlan.id == RecoveryPlanStep.recovery_plan_id)
            .where(RecoveryPlan.recovery_case_id.in_(case_ids), RecoveryPlanStep.status == "BLOCKED")
        ) or 0)

        from app.audit.chain import AuditChain
        from app.compliance.contact_policy import ContactPolicy

        chain = AuditChain.verify(db)
        return {
            "policy_violations": int(sum(violations_by_rule.values())),
            "violations_by_rule": dict(violations_by_rule),
            "holdout_outreach_count": holdout_outreach,
            "policy_blocks_enforced": blocked_steps,
            "stopping_rule_breaches": post_recovery,
            "contact_blocks_by_rule": ContactPolicy.blocks_by_rule(db, case_ids),
            "audit_chain": {"ok": chain["ok"], "chained_rows": chain["chained_rows"], "first_break": chain["first_break"]},
        }

    @classmethod
    def _audit_coverage(cls, db: Session, case_ids: List[uuid.UUID]) -> Dict[str, Any]:
        executed_ids = set()
        for cid in db.scalars(select(RecoveryExecution.recovery_case_id).where(RecoveryExecution.recovery_case_id.in_(case_ids), RecoveryExecution.status == "SUCCEEDED")).all():
            executed_ids.add(cid)
        for cid in db.scalars(select(Communication.recovery_case_id).where(Communication.recovery_case_id.in_(case_ids), Communication.status.in_(["SENT", "DELIVERED", "READ"]))).all():
            executed_ids.add(cid)
        for cid in db.scalars(select(VoiceCall.recovery_case_id).where(VoiceCall.recovery_case_id.in_(case_ids))).all():
            executed_ids.add(cid)
        for cid in db.scalars(select(Intervention.recovery_case_id).where(Intervention.recovery_case_id.in_(case_ids), Intervention.status.in_(["SENT", "SUCCEEDED"]))).all():
            executed_ids.add(cid)

        audited = 0
        if executed_ids:
            audited_ids = set(db.scalars(select(AuditLog.recovery_case_id).where(AuditLog.recovery_case_id.in_(list(executed_ids))).distinct()).all())
            audited = len(executed_ids & audited_ids)
        total_audit_rows = int(db.scalar(select(func.count(AuditLog.id)).where(AuditLog.recovery_case_id.in_(case_ids))) or 0)
        return {
            "executed_actions": len(executed_ids),
            "audited_actions": audited,
            "coverage": round(audited / len(executed_ids), 4) if executed_ids else 1.0,
            "audit_rows": total_audit_rows,
        }

    @classmethod
    def _costs(cls, db: Session, case_ids: List[uuid.UUID], arm_stats) -> Dict[str, Any]:
        rows = db.execute(
            select(LedgerEntry.channel, func.count(LedgerEntry.id), func.coalesce(func.sum(LedgerEntry.amount), 0))
            .where(LedgerEntry.recovery_case_id.in_(case_ids), LedgerEntry.entry_type == LedgerEntryType.COST.value)
            .group_by(LedgerEntry.channel)
        ).all()
        by_channel = {str(ch or "UNKNOWN"): {"actions": int(n), "cost": float(total or 0)} for ch, n, total in rows}
        total_cost = sum(v["cost"] for v in by_channel.values())
        recovered = arm_stats.get(ExperimentArm.TREATMENT.value, {}).get("amount_recovered", 0.0)
        return {
            "total_cost": round(total_cost, 2),
            "by_channel": by_channel,
            "cost_per_recovered_rupee": round(total_cost / recovered, 6) if recovered > 0 else None,
            "roi_multiple": round(recovered / total_cost, 2) if total_cost > 0 else None,
        }

    @classmethod
    def _timing(cls, db: Session, case_ids: List[uuid.UUID]) -> Dict[str, Any]:
        ttr = db.scalars(select(RecoveryOutcome.time_to_recovery_seconds).where(RecoveryOutcome.recovery_case_id.in_(case_ids), RecoveryOutcome.time_to_recovery_seconds.isnot(None))).all()
        hours = [float(s) / 3600.0 for s in ttr if s is not None]
        return {"time_to_recovery_hours": percentiles(hours, (50, 90)), "attributable_recoveries": len(hours)}

    @classmethod
    def _breakdowns(cls, db: Session, cases: List[RecoveryCase], ledger_by_case) -> Dict[str, Any]:
        def bucket(key_fn):
            agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"cases": 0, "recovered_cases": 0, "amount_at_risk": 0.0, "amount_recovered": 0.0})
            for c in cases:
                k = key_fn(c) or "UNKNOWN"
                ledger = ledger_by_case.get(c.id, {})
                rec = float(cls._case_recovered(c, ledger))
                agg[k]["cases"] += 1
                agg[k]["recovered_cases"] += 1 if rec > 0 else 0
                agg[k]["amount_at_risk"] += float(ledger.get(LedgerEntryType.AT_RISK.value) or Decimal(str(c.amount_at_risk or 0)))
                agg[k]["amount_recovered"] += rec
            for v in agg.values():
                v["case_recovery_rate"] = round(v["recovered_cases"] / v["cases"], 4) if v["cases"] else 0.0
                v["amount_at_risk"] = round(v["amount_at_risk"], 2)
                v["amount_recovered"] = round(v["amount_recovered"], 2)
            return dict(agg)

        case_ids = [c.id for c in cases]
        diag_rows = db.execute(
            select(Diagnosis.recovery_case_id, Diagnosis.category).where(Diagnosis.recovery_case_id.in_(case_ids)).order_by(Diagnosis.created_at.asc())
        ).all()
        diag_by_case = {cid: cat for cid, cat in diag_rows}  # last write wins = latest diagnosis

        # Treatment vs holdout inside each surface: the lift a merchant would see per leak type.
        lift_by_surface: Dict[str, Dict[str, Any]] = {}
        surfaces = sorted({(c.leak_surface or c.case_type) for c in cases})
        for surf in surfaces:
            t_cases = [c for c in cases if (c.leak_surface or c.case_type) == surf and (c.experiment_arm or "TREATMENT") == "TREATMENT"]
            h_cases = [c for c in cases if (c.leak_surface or c.case_type) == surf and c.experiment_arm == "HOLDOUT"]
            t_rec = sum(1 for c in t_cases if cls._case_recovered(c, ledger_by_case.get(c.id, {})) > 0)
            h_rec = sum(1 for c in h_cases if cls._case_recovered(c, ledger_by_case.get(c.id, {})) > 0)
            t_rate = t_rec / len(t_cases) if t_cases else None
            h_rate = h_rec / len(h_cases) if h_cases else None
            lift_by_surface[surf] = {
                "treatment_cases": len(t_cases), "treatment_recovered": t_rec, "treatment_rate": round(t_rate, 4) if t_rate is not None else None,
                "holdout_cases": len(h_cases), "holdout_recovered": h_rec, "holdout_rate": round(h_rate, 4) if h_rate is not None else None,
                "absolute_lift": round(t_rate - h_rate, 4) if (t_rate is not None and h_rate is not None) else None,
            }

        return {
            "by_surface": bucket(lambda c: c.leak_surface or c.case_type),
            "by_arm": bucket(lambda c: c.experiment_arm or "TREATMENT"),
            "by_root_cause": bucket(lambda c: diag_by_case.get(c.id, "UNDIAGNOSED")),
            "by_status": bucket(lambda c: c.status),
            "lift_by_surface": lift_by_surface,
        }
